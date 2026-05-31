#!/usr/bin/env python3
"""
基础代理服务类 - 消除claude和codex的重复代码
提供统一的代理服务实现
"""
import asyncio
import base64
import json
import os
import subprocess
import socket
import sys
import time
import uuid
from datetime import datetime
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import ssl
import certifi
import httpx
from contextlib import asynccontextmanager, contextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from ..utils.usage_parser import (
    extract_usage_from_response,
    normalize_usage_record,
    empty_metrics,
    merge_usage_metrics,
)
from ..utils.platform_helper import create_detached_process
from .realtime_hub import RealTimeRequestHub

class BaseProxyService(ABC):
    """基础代理服务类"""

    # 只对快速暴露的协议/连接坏错做透明重试；长时间等待后断开通常是上游处理超时，
    # 重试只会把用户等待从约 180 秒拉长到约 360 秒。
    UPSTREAM_PROTOCOL_RETRY_MAX_ELAPSED_SECONDS = 30.0
    LOG_BODY_PREVIEW_BYTES = 256 * 1024
    LOG_TRIM_EXTRA_LINES = 20
    LOG_FILE_LOCK_TIMEOUT_SECONDS = 5.0
    
    def __init__(self, service_name: str, port: int, config_manager):
        """
        初始化代理服务

        Args:
            service_name: 服务名称 (claude/codex)
            port: 服务端口
            config_manager: 配置管理器实例
        """
        self.service_name = service_name
        self.port = port
        self.config_manager = config_manager

        # 初始化路径并收紧权限
        self.config_dir = Path.home() / '.clp/run'
        self._ensure_secure_directory(self.config_dir)
        self.pid_file = self.config_dir / f'{service_name}_proxy.pid'
        self.log_file = self.config_dir / f'{service_name}_proxy.log'

        # 数据目录
        self.data_dir = Path.home() / '.clp/data'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.traffic_log = self.data_dir / 'proxy_requests.jsonl'
        old_log = self.data_dir / 'traffic_statistics.jsonl'
        if not self.traffic_log.exists() and old_log.exists():
            try:
                old_log.rename(self.traffic_log)
            except OSError:
                # 如果重命名失败，则保留旧文件并继续使用旧路径
                self.traffic_log = old_log

        # 路由配置文件
        self.routing_config_file = self.data_dir / 'model_router_config.json'
        self.routing_config = self._load_routing_config()
        self.routing_config_signature = self._get_file_signature(self.routing_config_file)

        # 负载均衡配置文件
        self.lb_config_file = self.data_dir / 'lb_config.json'
        self.lb_config = self._load_lb_config()
        self.lb_config_signature = self._get_file_signature(self.lb_config_file)

        # 初始化异步HTTP客户端
        self._client_rebuild_lock = asyncio.Lock()
        self.client = self._create_async_client()

        # 日志写入使用单队列串行追加，避免并发重写 proxy_requests.jsonl
        self._log_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._log_writer_task: Optional[asyncio.Task] = None
        self._traffic_log_line_count: Optional[int] = None

        # 响应日志截断阈值（避免长流占用过多内存）
        self.max_logged_response_bytes = 1024 * 1024  # 1MB

        # 组内 key 轮转状态 (纯内存, 不持久化)
        # {group_name: {'index': int, 'last_time': float}}
        self._rotation_state: Dict[str, Dict[str, Any]] = {}

        # 默认不自动禁用任何 key；各组可通过 fatal_status_codes 字段自定义
        self.DEFAULT_FATAL_STATUS_CODES: set = set()

        # 初始化实时事件中心
        self.realtime_hub = RealTimeRequestHub(service_name)

        # 初始化FastAPI应用（使用 lifespan 管理生命周期）
        @asynccontextmanager
        async def lifespan(app):
            self._log_writer_task = asyncio.create_task(self._log_writer_loop())
            try:
                yield
            finally:
                await self._shutdown_log_writer()
                await self.client.aclose()

        self.app = FastAPI(lifespan=lifespan)
        self._setup_routes()

        # 导入过滤器
        try:
            from ..filter.cached_request_filter import CachedRequestFilter
            self.request_filter = CachedRequestFilter()
        except ImportError:
            # 如果缓存版本不存在，使用原版本
            from ..filter.request_filter import filter_request_data
            self.filter_request_data = filter_request_data
            self.request_filter = None
    
    @staticmethod
    def _ensure_secure_directory(directory: Path):
        """确保目录存在并设置仅用户访问权限"""
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            # 在部分平台/文件系统上可能无法chmod，忽略
            pass
    
    def _create_async_client(self) -> httpx.AsyncClient:
        """创建并配置 httpx AsyncClient"""
        timeout = httpx.Timeout(  # 允许长时间流式响应
            timeout=None,
            connect=30.0,
            read=None,
            write=30.0,
            pool=None,
        )
        limits = httpx.Limits(
            max_connections=200,
            max_keepalive_connections=100,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        return httpx.AsyncClient(timeout=timeout, limits=limits, headers={"Connection": "keep-alive"}, verify=ssl_context)

    async def _close_retired_client_later(self, client: httpx.AsyncClient, delay_seconds: float = 60.0):
        """延迟关闭旧连接池，避免打断正在读取的并发流式请求。"""
        try:
            await asyncio.sleep(delay_seconds)
            await client.aclose()
        except Exception as exc:
            print(f"[上游连接池] 关闭旧连接池失败: {exc}")

    async def _rebuild_async_client(self, reason: str):
        """为后续请求切换到新的 httpx 连接池。"""
        async with self._client_rebuild_lock:
            old_client = self.client
            self.client = self._create_async_client()
            print(f"[上游连接池] 已重建连接池: {reason}")
            asyncio.create_task(self._close_retired_client_later(old_client))

    def _is_retryable_upstream_error(self, exc: Exception) -> bool:
        return isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError))

    def _can_retry_upstream_error(self, exc: Exception, elapsed_seconds: float, attempt: int, max_retries: int) -> Tuple[bool, Optional[str]]:
        if not self._is_retryable_upstream_error(exc):
            return False, "non_retryable_error"
        if attempt >= max_retries:
            return False, "retry_limit_reached"
        if elapsed_seconds > self.UPSTREAM_PROTOCOL_RETRY_MAX_ELAPSED_SECONDS:
            return False, "late_upstream_disconnect"
        return True, None

    async def _send_upstream_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        content: Optional[bytes],
        is_stream: bool,
        max_retries: int = 1,
    ) -> Tuple[httpx.Response, list]:
        """发送上游请求；仅在未向客户端写出任何数据前，对协议级断流做一次安全重试。"""
        attempts = []
        for attempt in range(max_retries + 1):
            client = self.client
            attempt_started = time.monotonic()
            request_out = client.build_request(
                method=method,
                url=url,
                headers=headers,
                content=content if content else None,
            )
            try:
                response = await client.send(request_out, stream=is_stream)
                if attempts:
                    attempts.append({
                        "attempt": attempt + 1,
                        "phase": "send",
                        "result": "success",
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    })
                return response, attempts
            except httpx.RequestError as exc:
                elapsed_seconds = time.monotonic() - attempt_started
                should_retry, skip_reason = self._can_retry_upstream_error(
                    exc, elapsed_seconds, attempt, max_retries
                )
                attempt_info = {
                    "attempt": attempt + 1,
                    "phase": "send",
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": int(elapsed_seconds * 1000),
                    "retryable": should_retry,
                }
                if skip_reason:
                    attempt_info["retry_skipped_reason"] = skip_reason
                attempts.append(attempt_info)

                if not should_retry:
                    setattr(exc, "_clp_upstream_attempts", attempts)
                    raise

                await self._rebuild_async_client(f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(0.25 * (attempt + 1))

        raise RuntimeError("unreachable upstream retry state")

    def _setup_routes(self):
        """设置API路由"""
        @self.app.api_route(
            "/{path:path}",
            methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']
        )
        async def proxy_route(path: str, request: Request):
            return await self.proxy(path, request)

        @self.app.websocket("/ws/realtime")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket实时事件端点"""
            await self.realtime_hub.connect(websocket)
            try:
                # 保持连接活跃，等待客户端消息或断开
                while True:
                    # 接收客户端的ping消息，保持连接
                    try:
                        await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # 发送ping消息保持连接
                        await websocket.send_text('{"type":"ping"}')
            except WebSocketDisconnect:
                pass
            except Exception as e:
                print(f"WebSocket连接异常: {e}")
            finally:
                self.realtime_hub.disconnect(websocket)

    async def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        target_headers: Optional[Dict] = None,
        filtered_body: Optional[bytes] = None,
        original_headers: Optional[Dict] = None,
        original_body: Optional[bytes] = None,
        response_content: Optional[bytes] = None,
        channel: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        response_truncated: bool = False,
        total_response_bytes: Optional[int] = None,
        target_url: Optional[str] = None,
        response_headers: Optional[Dict] = None,
        upstream_error: Optional[Dict[str, Any]] = None,
        upstream_attempts: Optional[list] = None,
    ):
        """记录请求日志到jsonl文件（异步调度）"""
        try:
            log_entry = await asyncio.to_thread(
                self._build_log_entry,
                method, path, status_code, duration_ms, target_headers,
                filtered_body, original_headers, original_body, response_content,
                channel, usage, response_truncated, total_response_bytes,
                target_url, response_headers, upstream_error, upstream_attempts,
            )
            await self._enqueue_log_entry(log_entry)
        except Exception as exc:
            print(f"日志记录失败: {exc}")

    def _build_log_entry(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        target_headers: Optional[Dict],
        filtered_body: Optional[bytes],
        original_headers: Optional[Dict],
        original_body: Optional[bytes],
        response_content: Optional[bytes],
        channel: Optional[str],
        usage: Optional[Dict[str, Any]],
        response_truncated: bool,
        total_response_bytes: Optional[int],
        target_url: Optional[str],
        response_headers: Optional[Dict],
        upstream_error: Optional[Dict[str, Any]],
        upstream_attempts: Optional[list],
    ) -> dict:
        log_entry = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'service': self.service_name,
            'method': method,
            'path': target_url if target_url else path,
            'status_code': status_code,
            'duration_ms': duration_ms,
            'target_headers': target_headers or {}
        }

        if channel:
            log_entry['channel'] = channel

        if original_headers:
            log_entry['original_headers'] = original_headers

        if original_body:
            self._store_log_bytes(log_entry, 'original_body', original_body, self.LOG_BODY_PREVIEW_BYTES)

        if filtered_body:
            if original_body and filtered_body == original_body:
                log_entry['filtered_body_same_as_original'] = True
                log_entry['filtered_body_bytes'] = len(filtered_body)
                log_entry['filtered_body_logged_bytes'] = log_entry.get('original_body_logged_bytes', len(filtered_body))
                if log_entry.get('original_body_truncated'):
                    log_entry['filtered_body_truncated'] = True
            else:
                self._store_log_bytes(log_entry, 'filtered_body', filtered_body, self.LOG_BODY_PREVIEW_BYTES)

        usage_record = usage
        if usage_record is None:
            usage_record = extract_usage_from_response(self.service_name, response_content)
        usage_record = normalize_usage_record(self.service_name, usage_record)
        log_entry['usage'] = usage_record

        if response_content:
            self._store_log_bytes(log_entry, 'response_content', response_content, self.max_logged_response_bytes)

        if response_headers:
            log_entry['response_headers'] = response_headers

        if response_truncated:
            log_entry['response_truncated'] = True

        if total_response_bytes is not None:
            log_entry['response_bytes'] = total_response_bytes

        if upstream_error:
            log_entry['upstream_error'] = upstream_error

        if upstream_attempts:
            log_entry['upstream_attempts'] = upstream_attempts
            log_entry['upstream_retry_count'] = sum(
                1 for attempt in upstream_attempts if attempt.get('retryable')
            )

        return log_entry

    def _store_log_bytes(self, log_entry: dict, field: str, content: bytes, limit: int):
        total_bytes = len(content)
        logged = content[:limit] if limit and total_bytes > limit else content
        log_entry[field] = base64.b64encode(logged).decode('utf-8')
        log_entry[f'{field}_bytes'] = total_bytes
        log_entry[f'{field}_logged_bytes'] = len(logged)
        if len(logged) < total_bytes:
            log_entry[f'{field}_truncated'] = True

    async def _enqueue_log_entry(self, log_entry: dict):
        if self._log_writer_task is None or self._log_writer_task.done():
            await asyncio.to_thread(self._append_log_entry, log_entry)
            return
        await self._log_queue.put(log_entry)

    async def _log_writer_loop(self):
        self._traffic_log_line_count = await asyncio.to_thread(self._count_traffic_log_lines)
        while True:
            log_entry = await self._log_queue.get()
            try:
                if log_entry is None:
                    return
                await asyncio.to_thread(self._append_log_entry, log_entry)
                self._traffic_log_line_count = (self._traffic_log_line_count or 0) + 1
                max_logs = self._get_log_limit()
                if self._traffic_log_line_count > max_logs + self.LOG_TRIM_EXTRA_LINES:
                    self._traffic_log_line_count = await asyncio.to_thread(
                        self._trim_traffic_log_to_limit, max_logs
                    )
            except Exception as exc:
                print(f"日志写入队列处理失败: {exc}")
            finally:
                self._log_queue.task_done()

    async def _shutdown_log_writer(self):
        if self._log_writer_task is None:
            return
        try:
            await self._log_queue.join()
            await self._log_queue.put(None)
            await asyncio.wait_for(self._log_writer_task, timeout=10.0)
        except Exception as exc:
            print(f"关闭日志写入队列失败: {exc}")

    def _save_discarded_logs_usage(self, discarded_logs: list[dict]) -> None:
        """将被丢弃的日志的usage数据保存到历史记录"""
        if not discarded_logs:
            return

        try:
            # 聚合被丢弃日志的usage数据
            aggregated: Dict[str, Dict[str, Dict[str, int]]] = {}
            for entry in discarded_logs:
                usage = entry.get('usage', {})
                metrics = usage.get('metrics', {})
                if not metrics:
                    continue

                service = usage.get('service') or entry.get('service') or 'unknown'
                channel = entry.get('channel') or 'unknown'

                service_bucket = aggregated.setdefault(service, {})
                channel_bucket = service_bucket.setdefault(channel, empty_metrics())
                merge_usage_metrics(channel_bucket, metrics)

            if not aggregated:
                return

            # 加载现有历史记录
            history_file = self.data_dir / 'history_usage.json'
            history_usage: Dict[str, Dict[str, Dict[str, int]]] = {}

            if history_file.exists():
                try:
                    with open(history_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                    # 规范化历史数据
                    for service, channels in (data or {}).items():
                        if not isinstance(channels, dict):
                            continue
                        service_bucket: Dict[str, Dict[str, int]] = {}
                        for channel, metrics in channels.items():
                            normalized = empty_metrics()
                            if isinstance(metrics, dict):
                                merge_usage_metrics(normalized, metrics)
                            service_bucket[channel] = normalized
                        history_usage[service] = service_bucket
                except (json.JSONDecodeError, OSError):
                    pass

            # 合并聚合的usage到历史记录
            for service, channels in aggregated.items():
                service_bucket = history_usage.setdefault(service, {})
                for channel, metrics in channels.items():
                    channel_bucket = service_bucket.setdefault(channel, empty_metrics())
                    merge_usage_metrics(channel_bucket, metrics)

            # 保存更新后的历史记录
            serializable = {
                service: {
                    channel: {key: int(value) for key, value in metrics.items()}
                    for channel, metrics in channels.items()
                }
                for service, channels in history_usage.items()
            }

            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)

        except Exception as exc:
            print(f"保存被丢弃日志的usage失败: {exc}")

    def _get_log_limit(self) -> int:
        system_config_file = self.data_dir / 'system.json'
        max_logs = 50
        try:
            if system_config_file.exists():
                with open(system_config_file, 'r', encoding='utf-8') as f:
                    system_config = json.load(f)
                    max_logs = int(system_config.get('logLimit', 50))
        except (ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
            print(f"读取系统配置失败，使用默认日志限制 {max_logs}: {exc}")
        return max(1, max_logs)

    @contextmanager
    def _traffic_log_lock(self):
        lock_path = self.traffic_log.with_name(f'{self.traffic_log.name}.lock')
        fd = None
        start = time.monotonic()
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                if time.monotonic() - start > self.LOG_FILE_LOCK_TIMEOUT_SECONDS:
                    try:
                        if time.time() - lock_path.stat().st_mtime > 30:
                            try:
                                lock_path.unlink()
                            except FileNotFoundError:
                                pass
                            continue
                    except OSError:
                        pass
                    raise TimeoutError(f"获取日志文件锁超时: {lock_path}")
                time.sleep(0.05)

        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _append_log_entry(self, log_entry: dict):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(log_entry, ensure_ascii=False) + '\n'
        with self._traffic_log_lock():
            with open(self.traffic_log, 'a', encoding='utf-8') as f:
                f.write(line)

    def _count_traffic_log_lines(self) -> int:
        if not self.traffic_log.exists():
            return 0
        try:
            with self._traffic_log_lock():
                with open(self.traffic_log, 'r', encoding='utf-8') as f:
                    return sum(1 for line in f if line.strip())
        except Exception as exc:
            print(f"统计日志条数失败: {exc}")
            return 0

    def _trim_traffic_log_to_limit(self, max_logs: int) -> int:
        try:
            existing_logs: list[dict] = []
            malformed_lines: list[str] = []
            with self._traffic_log_lock():
                if not self.traffic_log.exists():
                    return 0

                # 只有后台裁剪时才读取全量日志；普通请求完成只追加单行。
                with open(self.traffic_log, 'r', encoding='utf-8') as f:
                    for line in f:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            existing_logs.append(json.loads(raw))
                        except json.JSONDecodeError:
                            malformed_lines.append(raw)

                if len(existing_logs) <= max_logs:
                    return len(existing_logs)

                discarded_logs = existing_logs[:-max_logs]
                kept_logs = existing_logs[-max_logs:]
                self._save_discarded_logs_usage(discarded_logs)

                with open(self.traffic_log, 'w', encoding='utf-8') as f:
                    for raw in malformed_lines[-5:]:
                        f.write(raw + '\n')
                    for log_entry in kept_logs:
                        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

                return len(kept_logs) + min(len(malformed_lines), 5)
        except Exception as exc:
            print(f"裁剪日志文件失败: {exc}")
            return self._traffic_log_line_count or 0

    def _maintain_log_limit(self, new_log_entry: dict):
        """兼容旧调用：追加日志并在超过阈值时裁剪。"""
        try:
            self._append_log_entry(new_log_entry)
            max_logs = self._get_log_limit()
            current_count = self._count_traffic_log_lines()
            if current_count > max_logs + self.LOG_TRIM_EXTRA_LINES:
                self._trim_traffic_log_to_limit(max_logs)
        except Exception as exc:
            print(f"维护日志文件限制失败: {exc}")
            try:
                with open(self.traffic_log, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(new_log_entry, ensure_ascii=False) + '\n')
            except Exception as fallback_exc:
                print(f"备用日志写入也失败: {fallback_exc}")

    def _get_file_signature(self, file_path: Path) -> Tuple[int, int]:
        """获取文件签名，用于检测内容变化"""
        try:
            stat_result = file_path.stat()
            return stat_result.st_mtime_ns, stat_result.st_size
        except FileNotFoundError:
            return (0, 0)
        except OSError as exc:
            print(f"读取文件签名失败({file_path}): {exc}")
            return (0, 0)

    def _ensure_routing_config_current(self):
        """检查路由配置是否有更新，如有则重新加载"""
        current_signature = self._get_file_signature(self.routing_config_file)
        if current_signature != self.routing_config_signature:
            self.routing_config = self._load_routing_config()
            self.routing_config_signature = current_signature

    def _load_routing_config(self) -> dict:
        """加载路由配置"""
        try:
            if self.routing_config_file.exists():
                with open(self.routing_config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"加载路由配置失败: {e}")
        
        # 返回默认配置
        return {
            'mode': 'default',
            'modelMappings': {
                'claude': [],
                'codex': []
            },
            'configMappings': {
                'claude': [],
                'codex': []
            }
        }

    def _default_lb_config(self) -> dict:
        """构建负载均衡默认配置"""
        return {
            'mode': 'active-first',
            'services': {
                'claude': {
                    'failureThreshold': 3,
                    'autoResetMinutes': 10,
                    'currentFailures': {},
                    'excludedConfigs': [],
                    'excludedTimestamps': {},
                    'manualDisabledUntil': {}
                },
                'codex': {
                    'failureThreshold': 3,
                    'autoResetMinutes': 10,
                    'currentFailures': {},
                    'excludedConfigs': [],
                    'excludedTimestamps': {},
                    'manualDisabledUntil': {}
                }
            }
        }

    def _ensure_lb_service_section(self, config: dict, service: str):
        """确保指定服务的负载均衡配置结构完整"""
        services = config.setdefault('services', {})
        service_section = services.setdefault(service, {})
        threshold = service_section.get('failureThreshold', 3)
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            threshold = 3
        if threshold <= 0:
            threshold = 3
        service_section['failureThreshold'] = threshold

        failures = service_section.get('currentFailures', {})
        if not isinstance(failures, dict):
            failures = {}
        normalized_failures = {}
        for key, value in failures.items():
            name = str(key)
            try:
                count = int(value)
            except (TypeError, ValueError):
                count = 0
            normalized_failures[name] = max(count, 0)
        service_section['currentFailures'] = normalized_failures

        excluded = service_section.get('excludedConfigs', [])
        if not isinstance(excluded, list):
            excluded = []
        service_section['excludedConfigs'] = [str(item) for item in excluded if isinstance(item, str)]

        auto_reset = service_section.get('autoResetMinutes', 10)
        try:
            auto_reset = int(auto_reset)
        except (TypeError, ValueError):
            auto_reset = 10
        if auto_reset < 0:
            auto_reset = 0
        service_section['autoResetMinutes'] = auto_reset

        timestamps = service_section.get('excludedTimestamps', {})
        if not isinstance(timestamps, dict):
            timestamps = {}
        normalized_timestamps = {}
        for key, value in timestamps.items():
            name = str(key)
            try:
                ts_value = float(value)
            except (TypeError, ValueError):
                continue
            if ts_value > 0:
                normalized_timestamps[name] = ts_value
        service_section['excludedTimestamps'] = normalized_timestamps

        manual_disabled = service_section.get('manualDisabledUntil', {})
        if not isinstance(manual_disabled, dict):
            manual_disabled = {}
        normalized_manual = {}
        for key, value in manual_disabled.items():
            name = str(key)
            try:
                date_str = str(value).strip()
            except Exception:
                continue
            if date_str:
                normalized_manual[name] = date_str
        service_section['manualDisabledUntil'] = normalized_manual

    def _apply_lb_auto_reset(self, service_section: dict) -> bool:
        """根据配置自动重置已跳过的目标"""
        interval_minutes = service_section.get('autoResetMinutes', 0)
        try:
            interval_minutes = int(interval_minutes)
        except (TypeError, ValueError):
            interval_minutes = 0

        if interval_minutes <= 0:
            return False

        excluded = service_section.get('excludedConfigs', [])
        if not excluded:
            return False

        timestamps = service_section.setdefault('excludedTimestamps', {})
        failures = service_section.setdefault('currentFailures', {})
        now = time.time()
        changed = False
        expired = []

        for name in list(excluded):
            ts_value = timestamps.get(name)
            if not isinstance(ts_value, (int, float)):
                expired.append(name)
                continue
            if now - float(ts_value) >= interval_minutes * 60:
                expired.append(name)

        for name in expired:
            if name in excluded:
                excluded.remove(name)
            timestamps.pop(name, None)
            if name in failures and failures[name] != 0:
                failures[name] = 0
            changed = True

        return changed

    def _cleanup_manual_disabled(self, service_section: dict) -> bool:
        """清理已过期的手动禁用项"""
        manual_disabled = service_section.setdefault('manualDisabledUntil', {})
        if not isinstance(manual_disabled, dict):
            service_section['manualDisabledUntil'] = {}
            return True

        today = datetime.now().date().isoformat()
        changed = False

        for name in list(manual_disabled.keys()):
            value = manual_disabled.get(name)
            if not isinstance(value, str):
                manual_disabled.pop(name, None)
                changed = True
                continue
            date_str = value.strip()
            if not date_str or date_str != today:
                manual_disabled.pop(name, None)
                changed = True

        return changed

    def _load_lb_config(self) -> dict:
        """加载负载均衡配置"""
        try:
            if self.lb_config_file.exists():
                with open(self.lb_config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = self._default_lb_config()
        except Exception as exc:
            print(f"加载负载均衡配置失败: {exc}")
            data = self._default_lb_config()

        if 'mode' not in data:
            data['mode'] = 'active-first'

        self._ensure_lb_service_section(data, 'claude')
        self._ensure_lb_service_section(data, 'codex')
        return data

    def _ensure_lb_config_current(self):
        """检查负载均衡配置是否有更新"""
        current_signature = self._get_file_signature(self.lb_config_file)
        if current_signature != self.lb_config_signature:
            self.lb_config = self._load_lb_config()
            self.lb_config_signature = current_signature

    def _persist_lb_config(self):
        """持久化负载均衡配置"""
        try:
            with open(self.lb_config_file, 'w', encoding='utf-8') as f:
                json.dump(self.lb_config, f, ensure_ascii=False, indent=2)
            self.lb_config_signature = self._get_file_signature(self.lb_config_file)
        except OSError as exc:
            print(f"保存负载均衡配置失败: {exc}")

    def reload_lb_config(self):
        """重新加载负载均衡配置"""
        self.lb_config = self._load_lb_config()
        self.lb_config_signature = self._get_file_signature(self.lb_config_file)

    def _apply_model_routing(self, body: bytes) -> Tuple[bytes, Optional[str]]:
        """应用模型路由规则，返回修改后的body和要使用的配置名"""
        routing_mode = self.routing_config.get('mode', 'default')
        
        if routing_mode == 'default':
            return body, None
        
        try:
            # 解析请求体
            if not body:
                return body, None
                
            body_str = body.decode('utf-8')
            body_json = json.loads(body_str)
            
            # 获取模型名称
            model = body_json.get('model')
            if not model:
                return body, None
            
            if routing_mode == 'model-mapping':
                return self._apply_model_mapping(body_json, model, body)
            elif routing_mode == 'config-mapping':
                return self._apply_config_mapping(body_json, model, body)
                
        except Exception as e:
            print(f"应用模型路由失败: {e}")
            
        return body, None

    # modality 优先级：图片 > 音频 > 文档（命中第一个即用）
    _MODALITY_PRIORITY = ('image', 'audio', 'document')

    def _detect_request_modality(self, body_json: dict) -> set:
        """扫描请求体的消息内容，返回包含的非文本模态集合。

        兼容三种主流请求体形态：
        - Anthropic Messages API：顶层 `messages[]`，图片 block `type: image`
        - OpenAI Responses API（Codex）：顶层 `input[]`，图片 block `type: input_image`
        - OpenAI Chat Completions：顶层 `messages[]`，图片 block `type: image_url`

        识别规则：
        - image / input_image / image_url -> image
        - document                        -> document
        - input_audio / audio             -> audio
        - tool_result 内嵌的 image 也计入  -> image
        """
        modalities = set()

        # 收集所有可能的"消息容器"
        containers = []
        for key in ('messages', 'input'):
            v = body_json.get(key)
            if isinstance(v, list):
                containers.append(v)

        image_types = ('image', 'input_image', 'image_url')

        for msgs in containers:
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                content = msg.get('content')
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get('type', '')
                    if btype in image_types:
                        modalities.add('image')
                    elif btype == 'document':
                        modalities.add('document')
                    elif btype in ('input_audio', 'audio'):
                        modalities.add('audio')
                    elif btype == 'tool_result':
                        inner = block.get('content')
                        if isinstance(inner, list):
                            for ib in inner:
                                if isinstance(ib, dict) and ib.get('type') in image_types:
                                    modalities.add('image')
        return modalities

    def _select_target_by_modality(self, default_target: str, modality_targets, request_modalities: set):
        """按模态优先级选择 target，返回 (chosen_target, matched_modality_or_None)。

        若 modality_targets 缺失/格式不合法/无命中，回退到 default_target。
        """
        if not isinstance(modality_targets, dict) or not request_modalities:
            return default_target, None
        for mod in self._MODALITY_PRIORITY:
            if mod not in request_modalities:
                continue
            candidate = modality_targets.get(mod)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip(), mod
        return default_target, None

    def _apply_model_mapping(self, body_json: dict, model: str, original_body: bytes) -> Tuple[bytes, Optional[str]]:
        """应用模型→模型映射和配置→模型映射，支持按模态选择目标模型。"""
        mappings = self.routing_config.get('modelMappings', {}).get(self.service_name, [])
        request_modalities = self._detect_request_modality(body_json)

        for mapping in mappings:
            source = mapping.get('source', '').strip()
            target = mapping.get('target', '').strip()
            source_type = mapping.get('source_type', 'model').strip()
            modality_targets = mapping.get('modalityTargets')

            if not source or not target:
                continue

            matched = False
            if source_type == 'config':
                current_config = self._get_current_active_config()
                if current_config == source:
                    matched = True
            elif source_type == 'model':
                if model == source:
                    matched = True

            if not matched:
                continue

            chosen_target, matched_modality = self._select_target_by_modality(
                target, modality_targets, request_modalities
            )

            body_json['model'] = chosen_target
            modified_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')

            label = '配置映射' if source_type == 'config' else '模型映射'
            if matched_modality:
                print(f"{label}[{matched_modality}]: {source} -> {chosen_target}")
            else:
                print(f"{label}: {source} -> {chosen_target}")
            return modified_body, None

        return original_body, None

    def _apply_config_mapping(self, body_json: dict, model: str, original_body: bytes) -> Tuple[bytes, Optional[str]]:
        """应用模型→配置映射"""
        mappings = self.routing_config.get('configMappings', {}).get(self.service_name, [])
        
        for mapping in mappings:
            mapped_model = mapping.get('model', '').strip()
            target_config = mapping.get('config', '').strip()
            
            if mapped_model and target_config and model == mapped_model:
                # 检查目标配置是否存在
                if target_config in self.config_manager.configs:
                    print(f"配置映射: {model} -> {target_config}")
                    return original_body, target_config
                else:
                    print(f"配置映射失败: 配置 {target_config} 不存在")
        
        return original_body, None

    def _get_current_active_config(self) -> Optional[str]:
        """获取当前激活的组名"""
        return self.config_manager.active_group

    def _select_config_by_loadbalance(self, configs: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """根据负载均衡策略选择配置名"""
        self._ensure_lb_config_current()
        mode = self.lb_config.get('mode', 'active-first')
        if mode == 'weight-based':
            selected = self._select_weighted_config(configs)
            if selected:
                return selected
        return self.config_manager.active_config

    def _select_weighted_config(self, configs: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """按权重选择配置"""
        if not configs:
            return None

        self._ensure_lb_service_section(self.lb_config, self.service_name)
        service_section = self.lb_config['services'][self.service_name]
        changed = False
        if self._apply_lb_auto_reset(service_section):
            changed = True
        if self._cleanup_manual_disabled(service_section):
            changed = True
        if changed:
            self._persist_lb_config()

        threshold = service_section.get('failureThreshold', 3)
        failures = service_section.get('currentFailures', {})
        excluded = set(service_section.get('excludedConfigs', []))
        manual_disabled = service_section.get('manualDisabledUntil', {})
        today_key = datetime.now().date().isoformat()

        sorted_configs = sorted(
            configs.items(),
            key=lambda item: (-float(item[1].get('weight', 0) or 0), item[0])
        )

        for name, _ in sorted_configs:
            if failures.get(name, 0) >= threshold:
                continue
            if name in excluded:
                continue
            if isinstance(manual_disabled, dict) and manual_disabled.get(name) == today_key:
                continue
            return name

        active_config = self.config_manager.active_config
        if active_config in configs:
            return active_config
        return sorted_configs[0][0] if sorted_configs else None

    def reload_routing_config(self):
        """重新加载路由配置"""
        self.routing_config = self._load_routing_config()
        self.routing_config_signature = self._get_file_signature(self.routing_config_file)

    def _record_lb_result(self, config_name: Optional[str], status_code: int):
        """记录请求结果以更新负载均衡状态"""
        if not config_name:
            return

        self._ensure_lb_config_current()
        if self.lb_config.get('mode', 'active-first') != 'weight-based':
            return

        self._ensure_lb_service_section(self.lb_config, self.service_name)
        service_section = self.lb_config['services'][self.service_name]
        threshold = service_section.get('failureThreshold', 3)
        failures = service_section.setdefault('currentFailures', {})
        excluded = service_section.setdefault('excludedConfigs', [])
        timestamps = service_section.setdefault('excludedTimestamps', {})
        manual_disabled = service_section.setdefault('manualDisabledUntil', {})

        changed = False
        if self._apply_lb_auto_reset(service_section):
            changed = True
        if self._cleanup_manual_disabled(service_section):
            changed = True
        is_success = status_code is not None and 200 <= int(status_code) < 300

        if is_success:
            if failures.get(config_name, 0) != 0:
                failures[config_name] = 0
                changed = True
            if config_name in excluded:
                excluded.remove(config_name)
                changed = True
            if timestamps.pop(config_name, None) is not None:
                changed = True
        else:
            new_count = failures.get(config_name, 0) + 1
            if failures.get(config_name) != new_count:
                failures[config_name] = new_count
                changed = True
            if new_count >= threshold and config_name not in excluded:
                excluded.append(config_name)
                timestamps[config_name] = time.time()
                changed = True
            elif new_count >= threshold:
                # 已经处于排除列表中，刷新排除时间戳
                timestamps[config_name] = time.time()
            else:
                if config_name in timestamps:
                    timestamps.pop(config_name, None)
                    changed = True

        if changed:
            self._persist_lb_config()

    def _get_fatal_status_codes(self, group_name: Optional[str]) -> set:
        """获取组配置的 fatal_status_codes，未配置则返回空集（不自动禁用）"""
        if not group_name:
            return self.DEFAULT_FATAL_STATUS_CODES
        groups = self.config_manager.groups
        group_data = groups.get(group_name, {})
        codes = group_data.get('fatal_status_codes')
        if codes is not None:
            return set(codes)
        return self.DEFAULT_FATAL_STATUS_CODES

    def _select_key_from_group(self, group_name: str, group_data: Dict[str, Any], exclude_keys: set = None) -> Optional[Dict[str, Any]]:
        """
        从组中选择一个 key，基于空闲超时轮转策略:
        - 距上次请求未超过 idle_timeout → 继续使用当前 key (保住缓存)
        - 超过 idle_timeout → 轮转到下一个 key
        """
        keys = [k for k in group_data.get('keys', [])
                if not k.get('disabled', False) and k.get('name') not in (exclude_keys or set())]
        if not keys:
            return None
        if len(keys) == 1:
            return keys[0]

        now = time.time()
        idle_timeout = self.config_manager.rotation_config.get('idle_timeout', 300)
        state = self._rotation_state.get(group_name)

        if state is None:
            # 首次请求
            self._rotation_state[group_name] = {'index': 0, 'last_time': now}
            return keys[0]

        if now - state['last_time'] > idle_timeout:
            # 空闲超时，轮转到下一个
            new_index = (state['index'] + 1) % len(keys)
            self._rotation_state[group_name] = {'index': new_index, 'last_time': now}
            return keys[new_index]
        else:
            # 未超时，继续使用当前 key
            state['last_time'] = now
            index = state['index'] % len(keys)
            return keys[index]

    def build_target_param(self, path: str, request: Request, body: bytes, exclude_keys: set = None) -> Tuple[str, Dict, bytes, Optional[str], Optional[str]]:
        """
        构建请求参数 (v2 组格式)

        Returns:
            (target_url, headers, body, key_name_for_logging, group_name)
        """
        # 使用最新的路由配置
        self._ensure_routing_config_current()

        # 应用模型路由规则 (config_override 现在是组名)
        modified_body, config_override = self._apply_model_routing(body)

        # 确定要使用的组
        groups = self.config_manager.groups

        if config_override and config_override in groups:
            group_name = config_override
        else:
            group_name = self.config_manager.active_group

        group_data = groups.get(group_name) if group_name else None

        if not group_data and group_name:
            # 缓存可能过期，刷新重试
            groups = self.config_manager.groups
            group_data = groups.get(group_name)

        if not group_data:
            # 回退到第一个可用组
            if groups:
                group_name = next(iter(groups))
                group_data = groups[group_name]
            else:
                raise ValueError(f"未找到任何可用配置组")

        # 从组中选择一个 key (轮转)
        key_data = self._select_key_from_group(group_name, group_data, exclude_keys)
        if not key_data:
            raise ValueError(f"组 {group_name} 中没有可用的 key")

        key_name = key_data.get('name', group_name)

        # 构建目标URL
        base_url = group_data['base_url'].rstrip('/')
        normalized_path = path.lstrip('/')
        target_url = f"{base_url}/{normalized_path}" if normalized_path else base_url

        raw_query = request.url.query
        if raw_query:
            target_url = f"{target_url}?{raw_query}"

        # 处理headers
        excluded_headers = {'x-api-key', 'authorization', 'host', 'content-length'}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded_headers}
        headers['host'] = urlsplit(target_url).netloc
        headers.setdefault('connection', 'keep-alive')

        if key_data.get('api_key'):
            headers['x-api-key'] = key_data['api_key']
        if key_data.get('auth_token'):
            clean_token = ''.join(key_data['auth_token'].split())
            headers['authorization'] = f'Bearer {clean_token}'

        # OAuth 认证
        if group_data.get('auth_type') == 'oauth':
            headers['originator'] = 'codex_cli_rs'
            if key_data.get('account_id'):
                headers['chatgpt-account-id'] = key_data['account_id']

        return target_url, headers, modified_body, key_name, group_name

    @abstractmethod
    def test_endpoint(self, model: str, base_url: str, auth_token: str = None, api_key: str = None, extra_params: dict = None) -> dict:
        """
        测试API端点连通性

        Args:
            model: 模型名称
            base_url: 目标API地址
            auth_token: 认证令牌（可选）
            api_key: API密钥（可选）
            extra_params: 扩展参数（可选）

        Returns:
            dict: 包含测试结果的字典
        """
        pass

    def apply_request_filter(self, data: bytes) -> bytes:
        """应用请求过滤器"""
        if self.request_filter:
            # 使用缓存版本的过滤器
            return self.request_filter.apply_filters(data)
        else:
            # 使用原版本的过滤器
            return self.filter_request_data(data)
    
    async def proxy(self, path: str, request: Request):
        """处理代理请求"""
        start_time = time.time()
        request_id = str(uuid.uuid4())

        original_headers = {k: v for k, v in request.headers.items()}
        original_body = await request.body()

        active_config_name: Optional[str] = None
        active_group_name: Optional[str] = None
        target_headers: Optional[Dict[str, str]] = None
        filtered_body: Optional[bytes] = None
        target_url: Optional[str] = None

        try:
            target_url, target_headers, target_body, active_config_name, active_group_name = self.build_target_param(path, request, original_body)

            # 发送请求开始事件
            await self.realtime_hub.request_started(
                request_id=request_id,
                method=request.method,
                path=path,
                channel=active_config_name or "unknown",
                headers=target_headers,
                target_url=target_url
            )

        except ValueError as exc:
            return JSONResponse({
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(exc)}
            }, status_code=400)

        # 应用请求过滤器，放到线程池避免阻塞事件循环
        filtered_body = await asyncio.to_thread(self.apply_request_filter, target_body)

        # 检测是否需要流式传输
        headers_lower = {k.lower(): v for k, v in original_headers.items()}
        x_stainless_helper_method = headers_lower.get('x-stainless-helper-method', '').lower()
        content_type = headers_lower.get('content-type', '').lower()
        accept = headers_lower.get('accept', '').lower()
        is_stream = (
            'text/event-stream' in accept or
            'text/event-stream' in content_type or
            'stream' in content_type or
            'application/x-ndjson' in content_type or
            "stream" in x_stainless_helper_method
        )
        upstream_attempts_log = []

        try:
            # Key 错误感知重试: 遇到组配置的 fatal_status_codes 时禁用当前 key 并尝试下一个
            excluded_keys: set = set()
            response = None
            MAX_CONSECUTIVE_FAILURES = 3  # 连续同状态码失败上限，超过视为全局性错误

            # 获取当前组的 fatal_status_codes（未配置则为空集，不触发自动禁用）
            fatal_codes = self._get_fatal_status_codes(active_group_name)
            consecutive_same_code = 0
            last_fatal_code = None

            while True:
                response, send_attempts = await self._send_upstream_request(
                    method=request.method,
                    url=target_url,
                    headers=target_headers,
                    content=filtered_body,
                    is_stream=is_stream,
                )
                upstream_attempts_log.extend(send_attempts)
                status_code = response.status_code

                if not fatal_codes or status_code not in fatal_codes:
                    break  # 正常响应或非 key 级别的错误，不重试

                # 读取错误响应体用于日志
                error_body = None
                if is_stream:
                    error_body = b''
                    async for chunk in response.aiter_bytes():
                        error_body += chunk
                    await response.aclose()
                else:
                    error_body = response.content

                # 检测是否为非 key 级别的错误（如 HTML 响应 = Cloudflare/WAF 拦截）
                error_text = ''
                try:
                    error_text = error_body.decode('utf-8', errors='ignore')[:200] if error_body else ''
                except Exception:
                    pass
                is_non_key_error = error_text.lstrip().startswith('<')  # HTML 响应不是 API 错误

                # 连续相同状态码计数
                if status_code == last_fatal_code:
                    consecutive_same_code += 1
                else:
                    consecutive_same_code = 1
                    last_fatal_code = status_code

                # 如果是非 key 级别错误，或连续失败超限，停止重试且不禁用 key
                if is_non_key_error or consecutive_same_code >= MAX_CONSECUTIVE_FAILURES:
                    if is_non_key_error:
                        print(f"[KEY重试] 检测到非API错误(HTML响应)，停止重试，不禁用 key")
                    else:
                        print(f"[KEY重试] 连续 {consecutive_same_code} 个 key 返回 HTTP {status_code}，判定为全局性错误，停止重试")
                    break

                # 禁用当前 key
                excluded_keys.add(active_config_name)
                reason = f"HTTP {status_code}"
                if error_text:
                    reason = f"HTTP {status_code}: {error_text}"
                await asyncio.to_thread(
                    self.config_manager.disable_key, active_group_name, active_config_name, reason
                )
                await asyncio.to_thread(self._record_lb_result, active_config_name, status_code)

                # 尝试选下一个 key
                try:
                    target_url, target_headers, target_body, active_config_name, active_group_name = \
                        self.build_target_param(path, request, original_body, exclude_keys=excluded_keys)
                    filtered_body = await asyncio.to_thread(self.apply_request_filter, target_body)
                    print(f"[KEY重试] 切换到 key={active_config_name}，已排除: {excluded_keys}")
                except ValueError:
                    # 没有更多可用 key，返回最后一次的错误响应
                    duration_ms = int((time.time() - start_time) * 1000)
                    await self.realtime_hub.request_completed(
                        request_id=request_id, status_code=status_code,
                        duration_ms=duration_ms, success=False
                    )
                    await self.log_request(
                        method=request.method, path=path, status_code=status_code,
                        duration_ms=duration_ms, target_headers=target_headers,
                        filtered_body=filtered_body, original_headers=original_headers,
                        original_body=original_body, response_content=error_body,
                        channel=active_config_name, target_url=target_url,
                        upstream_attempts=upstream_attempts_log,
                    )
                    return JSONResponse(
                        {"type": "error", "error": {"type": "api_error", "message": f"组内所有 key 均不可用 (最后错误: HTTP {status_code})"}},
                        status_code=status_code
                    )

            status_code = response.status_code
            lb_result_recorded = False

            if not (200 <= status_code < 300):
                await asyncio.to_thread(self._record_lb_result, active_config_name, status_code)
                lb_result_recorded = True

            # 构造返回头，标记被剔除的头信息
            # httpx 的 aiter_bytes() 会自动解压 gzip/deflate/br 响应，
            # 因此必须剔除这些编码相关的头，否则客户端会尝试二次解压导致 ZlibError
            excluded_response_headers = {'content-encoding', 'content-length', 'transfer-encoding'}
            response_headers = {}  # 用于实际返回
            response_headers_for_log = {}  # 用于日志记录（包含标记）

            for k, v in response.headers.items():
                k_lower = k.lower()
                if k_lower in excluded_response_headers:
                    # 用于日志：保留但标记已剔除
                    response_headers_for_log[f"{k}[已剔除]"] = v
                else:
                    # 用于返回和日志
                    response_headers[k] = v
                    response_headers_for_log[k] = v

            # 拦截上游返回 200 但 body 为空的异常情况
            # 上游偶尔会返回 status=200 + content-length=0，客户端无法解析空 body 会报错
            upstream_content_length = response.headers.get('content-length')
            if status_code == 200 and upstream_content_length == '0':
                if is_stream:
                    await response.aclose()

                duration_ms = int((time.time() - start_time) * 1000)
                await self.realtime_hub.request_completed(
                    request_id=request_id,
                    status_code=502,
                    duration_ms=duration_ms,
                    success=False
                )
                await self.log_request(
                    method=request.method,
                    path=path,
                    status_code=502,
                    duration_ms=duration_ms,
                    target_headers=target_headers,
                    filtered_body=filtered_body,
                    original_headers=original_headers,
                    original_body=original_body,
                    channel=active_config_name,
                    target_url=target_url,
                    response_headers=response_headers_for_log,
                    upstream_attempts=upstream_attempts_log,
                )
                await asyncio.to_thread(self._record_lb_result, active_config_name, 502)
                return JSONResponse(
                    {"type": "error", "error": {"type": "api_error", "message": "上游返回空响应 (status=200, content-length=0)"}},
                    status_code=502
                )

            collected = bytearray()
            total_response_bytes = 0
            response_truncated = False
            first_chunk = True
            stream_error_info = None
            stream_preface_retry_used = False

            async def iterator():
                nonlocal response, status_code, response_truncated, total_response_bytes
                nonlocal first_chunk, lb_result_recorded, stream_error_info, stream_preface_retry_used
                try:
                    while True:
                        stream_attempt_started = time.monotonic()
                        try:
                            async for chunk in response.aiter_bytes():
                                if not chunk:
                                    continue

                                current_duration = int((time.time() - start_time) * 1000)

                                # 首次接收数据时标记为流式状态
                                if first_chunk:
                                    await self.realtime_hub.request_streaming(request_id, current_duration)
                                    first_chunk = False

                                # 尝试解码为文本发送实时更新
                                try:
                                    chunk_text = chunk.decode('utf-8', errors='ignore')
                                    if chunk_text.strip():  # 只发送非空chunk
                                        await self.realtime_hub.response_chunk(
                                            request_id, chunk_text, current_duration
                                        )
                                except Exception:
                                    pass  # 忽略解码失败

                                total_response_bytes += len(chunk)
                                if len(collected) < self.max_logged_response_bytes:
                                    remaining = self.max_logged_response_bytes - len(collected)
                                    collected.extend(chunk[:remaining])
                                    if len(chunk) > remaining:
                                        response_truncated = True
                                else:
                                    response_truncated = True
                                yield chunk
                            break
                        except Exception as stream_exc:
                            elapsed_seconds = time.monotonic() - stream_attempt_started
                            should_retry, skip_reason = self._can_retry_upstream_error(
                                stream_exc,
                                elapsed_seconds,
                                1 if stream_preface_retry_used else 0,
                                1,
                            )
                            retryable = (
                                is_stream and
                                first_chunk and
                                not stream_preface_retry_used and
                                200 <= status_code < 300 and
                                should_retry
                            )
                            attempt_info = {
                                "attempt": len(upstream_attempts_log) + 1,
                                "phase": "before_first_chunk",
                                "error_class": type(stream_exc).__name__,
                                "message": str(stream_exc),
                                "elapsed_ms": int(elapsed_seconds * 1000),
                                "retryable": retryable,
                            }
                            if skip_reason:
                                attempt_info["retry_skipped_reason"] = skip_reason
                            upstream_attempts_log.append(attempt_info)

                            if retryable:
                                stream_preface_retry_used = True
                                try:
                                    await response.aclose()
                                except Exception:
                                    pass
                                await self._rebuild_async_client(f"{type(stream_exc).__name__}: {stream_exc}")
                                try:
                                    response, retry_attempts = await self._send_upstream_request(
                                        method=request.method,
                                        url=target_url,
                                        headers=target_headers,
                                        content=filtered_body,
                                        is_stream=is_stream,
                                        max_retries=0,
                                    )
                                    upstream_attempts_log.extend(retry_attempts)
                                    upstream_attempts_log.append({
                                        "attempt": len(upstream_attempts_log) + 1,
                                        "phase": "before_first_chunk_retry",
                                        "result": "success",
                                    })
                                    status_code = response.status_code
                                    if 200 <= status_code < 300:
                                        continue
                                    stream_exc = httpx.HTTPStatusError(
                                        f"retry returned HTTP {status_code}",
                                        request=response.request,
                                        response=response,
                                    )
                                except httpx.RequestError as retry_exc:
                                    stream_exc = retry_exc

                            # 流式传输中断（连接断开、解压错误等）
                            stream_error_info = {
                                "phase": "after_first_chunk" if not first_chunk else "before_first_chunk",
                                "error_class": type(stream_exc).__name__,
                                "message": str(stream_exc),
                                "elapsed_ms": int(elapsed_seconds * 1000),
                                "retryable": False,
                            }
                            if skip_reason:
                                stream_error_info["retry_skipped_reason"] = skip_reason
                            # 注入 Anthropic 格式的 SSE error 事件，让客户端能正确识别错误
                            if is_stream:
                                error_data = json.dumps({
                                    "type": "error",
                                    "error": {"type": "api_error", "message": f"流式响应中断: {stream_exc}"}
                                })
                                yield f"event: error\ndata: {error_data}\n\n".encode('utf-8')
                            break
                finally:
                    final_duration = int((time.time() - start_time) * 1000)
                    final_status_code = 502 if stream_error_info else status_code

                    # 发送请求完成事件
                    try:
                        await self.realtime_hub.request_completed(
                            request_id=request_id,
                            status_code=final_status_code,
                            duration_ms=final_duration,
                            success=stream_error_info is None and 200 <= status_code < 400
                        )
                    except Exception:
                        pass

                    try:
                        await response.aclose()
                    except Exception:
                        pass

                    # 原有日志记录逻辑
                    try:
                        response_content = bytes(collected) if collected else None
                        await self.log_request(
                            method=request.method,
                            path=path,
                            status_code=final_status_code,
                            duration_ms=final_duration,
                            target_headers=target_headers,
                            filtered_body=filtered_body,
                            original_headers=original_headers,
                            original_body=original_body,
                            response_content=response_content,
                            channel=active_config_name,
                            response_truncated=response_truncated,
                            total_response_bytes=total_response_bytes,
                            target_url=target_url,
                            response_headers=response_headers_for_log,
                            upstream_error=stream_error_info,
                            upstream_attempts=upstream_attempts_log,
                        )
                    except Exception as log_exc:
                        print(f"流式响应日志记录异常: {log_exc}")

                    if not lb_result_recorded:
                        try:
                            await asyncio.to_thread(self._record_lb_result, active_config_name, final_status_code)
                            lb_result_recorded = True
                        except Exception:
                            pass

            return StreamingResponse(
                iterator(),
                status_code=status_code,
                headers=response_headers
            )
        except httpx.RequestError as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            send_failure_attempts = getattr(exc, "_clp_upstream_attempts", [])
            upstream_attempts = upstream_attempts_log + send_failure_attempts
            last_attempt = upstream_attempts[-1] if upstream_attempts else {}
            upstream_error = {
                "phase": "send",
                "error_class": type(exc).__name__,
                "message": str(exc),
                "elapsed_ms": last_attempt.get("elapsed_ms"),
                "retryable": bool(last_attempt.get("retryable")),
            }
            if last_attempt.get("retry_skipped_reason"):
                upstream_error["retry_skipped_reason"] = last_attempt.get("retry_skipped_reason")

            if isinstance(exc, httpx.ConnectTimeout):
                error_msg = "连接超时"
            elif isinstance(exc, httpx.ReadTimeout):
                error_msg = "响应读取超时"
            elif isinstance(exc, httpx.ConnectError):
                error_msg = "连接错误"
            elif isinstance(exc, httpx.HTTPStatusError):
                error_msg = "上游返回错误状态"
            else:
                error_msg = "请求失败"

            response_data = {
                "type": "error",
                "error": {"type": "api_error", "message": f"{error_msg}: {exc}"}
            }
            status_code = 500

            # 发送错误事件
            await self.realtime_hub.request_completed(
                request_id=request_id,
                status_code=status_code,
                duration_ms=duration_ms,
                success=False
            )

            await self.log_request(
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
                target_headers=target_headers,
                filtered_body=filtered_body,
                original_headers=original_headers,
                original_body=original_body,
                channel=active_config_name,
                target_url=target_url,
                upstream_error=upstream_error,
                upstream_attempts=upstream_attempts,
            )

            await asyncio.to_thread(self._record_lb_result, active_config_name, status_code)

            return JSONResponse(response_data, status_code=status_code)

    def run_app(self):
        """启动代理服务"""
        import os
        # 切换到项目根目录
        project_root = Path(__file__).parent.parent.parent
        
        # 在daemon环境中，需要明确指定环境和重定向
        env = os.environ.copy()
        
        try:
            with open(self.log_file, 'a') as log_file:
                uvicorn_cmd = [
                    sys.executable, '-m', 'uvicorn',
                    f'src.{self.service_name}.proxy:app',
                    '--host', '0.0.0.0',
                    '--port', str(self.port),
                    '--http', 'h11',
                    '--timeout-keep-alive', '60',
                    '--limit-concurrency', '500',
                ]
                subprocess.run(
                    uvicorn_cmd,
                    cwd=str(project_root),
                    env=env,
                    stdout=log_file,
                    stderr=log_file,
                    stdin=subprocess.DEVNULL
                )
                print(f"启动{self.service_name}代理成功 在端口 {self.port}")
        except Exception as e:
            print(f"启动{self.service_name}代理失败: {e}")


class BaseServiceController(ABC):
    """基础服务控制器类"""
    
    def __init__(self, service_name: str, port: int, config_manager, proxy_module_path: str):
        """
        初始化服务控制器
        
        Args:
            service_name: 服务名称
            port: 服务端口
            config_manager: 配置管理器实例
            proxy_module_path: 代理模块路径 (如 'src.claude.proxy')
        """
        self.service_name = service_name
        self.port = port
        self.config_manager = config_manager
        self.proxy_module_path = proxy_module_path
        
        # 初始化路径
        self.config_dir = Path.home() / '.clp/run'
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.config_dir / f'{service_name}_proxy.pid'
        self.log_file = self.config_dir / f'{service_name}_proxy.log'
    
    @staticmethod
    def _ensure_secure_directory(directory: Path):
        """确保运行目录存在且权限安全"""
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            # 在部分平台可能不支持 chmod，忽略即可
            pass

    def _write_pid_file(self, pid: int):
        """以受限权限写入 PID 文件"""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self.pid_file, flags, 0o600)
        with os.fdopen(fd, 'w') as pid_handle:
            pid_handle.write(str(pid))

    @staticmethod
    def _is_port_open(port: int, host: str = '127.0.0.1') -> bool:
        """检查端口是否可用"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                return sock.connect_ex((host, port)) == 0
            except OSError:
                return False

    def _wait_for_service_ready(self, port: int, timeout: float = 10.0, interval: float = 0.2) -> bool:
        """轮询等待服务就绪。若进程不在运行，立即失败；若在运行则等待端口就绪。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running():
                return False
            if self._is_port_open(port):
                return True
            time.sleep(interval)
        return self.is_running() and self._is_port_open(port)
    
    def get_pid(self) -> Optional[int]:
        """获取服务进程PID"""
        if self.pid_file.exists():
            try:
                return int(self.pid_file.read_text().strip())
            except:
                return None
        return None
    
    def is_running(self) -> bool:
        """检查服务是否在运行"""
        import psutil
        pid = self.get_pid()
        if pid:
            try:
                process = psutil.Process(pid)
                return process.is_running()
            except psutil.NoSuchProcess:
                return False
        return False
    
    def start(self, port: Optional[int] = None) -> bool:
        """启动服务"""
        if self.is_running():
            print(f"{self.service_name}服务已经在运行")
            return False
        
        config_file_path = None
        ensure_file_fn = getattr(self.config_manager, 'ensure_config_file', None)
        if callable(ensure_file_fn):
            config_file_path = ensure_file_fn()
        elif hasattr(self.config_manager, 'config_file'):
            config_file_path = getattr(self.config_manager, 'config_file')

        # 检查配置
        configs = self.config_manager.configs
        if not configs:
            if config_file_path:
                print(f"警告: {self.service_name}配置为空，将以占位模式启动。请编辑 {config_file_path} 补充配置后重启。")
            else:
                print(f"警告: 未检测到{self.service_name}配置文件，将以占位模式启动。")
        
        project_root = Path(__file__).parent.parent.parent
        env = os.environ.copy()
        original_port = self.port
        target_port = port if port is not None else self.port
        
        uvicorn_cmd = [
            sys.executable, '-m', 'uvicorn',
            f'{self.proxy_module_path}:app',
            '--host', '0.0.0.0',
            '--port', str(target_port),
            '--http', 'h11',
            '--timeout-keep-alive', '60',
            '--limit-concurrency', '500',
        ]
        with open(self.log_file, 'a') as log_handle:
            # 在独立进程组中运行，避免控制台信号终止子进程
            process = create_detached_process(
                uvicorn_cmd,
                log_handle,
                cwd=str(project_root),
                env=env,
            )

        # 保存PID
        self._write_pid_file(process.pid)

        if self._wait_for_service_ready(target_port):
            self.port = target_port
            print(f"{self.service_name}服务启动成功 (端口: {self.port})")
            return True

        # 启动失败，恢复端口并清理 PID 文件
        self.port = original_port
        if self.pid_file.exists():
            self.pid_file.unlink()
        print(f"{self.service_name}服务启动失败")
        return False
    
    def stop(self) -> bool:
        """停止服务"""
        import psutil
        
        if not self.is_running():
            print(f"{self.service_name}服务未运行")
            return False
        
        pid = self.get_pid()
        if pid:
            try:
                process = psutil.Process(pid)
                process.terminate()
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            
            # 清理PID文件
            if self.pid_file.exists():
                self.pid_file.unlink()
            
            print(f"{self.service_name}服务已停止")
            return True
        
        return False
    
    def restart(self, port: Optional[int] = None) -> bool:
        """重启服务"""
        self.stop()
        time.sleep(1)
        return self.start(port=port)
    
    def status(self) -> Dict[str, Any]:
        """返回服务状态信息"""
        running = self.is_running()
        pid = self.get_pid() if running else None
        active_config = self.config_manager.active_config
        return {
            'service': self.service_name,
            'running': running,
            'pid': pid,
            'active_config': active_config,
            'port': self.port,
        }
