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
    CLIENT_DISCONNECT_POLL_SECONDS = 3.0
    DEFAULT_MAX_CONSECUTIVE_FATAL_FAILURES = 3
    DEFAULT_MAX_CONSECUTIVE_QUOTA_FAILURES = 30
    DEFAULT_MAX_KEY_RETRIES_PER_REQUEST = 50
    DEFAULT_QUOTA_STATUS_CODES = {429}
    DEFAULT_QUOTA_ERROR_TYPES = {'usage_limit_reached'}
    # 流式转发保活：上游静默超过该秒数就给客户端注入一个 SSE 注释帧。anyrouter 长
    # thinking 时 body 会静默数十秒~数分钟，期间 clp 一个字节都不发，cc(尤其 2.1.185+
    # 字节级 idle 看门狗)会判连接卡死、提前放弃并重发→旧请求后台仍跑完 cc 不收→"错位"。
    # 注入保活让 cc↔clp 永不 idle。取值需远小于 cc 最短 idle 阈值(约 180s)。
    SSE_KEEPALIVE_SECONDS = 5.0
    
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

    @staticmethod
    def _body_requests_stream(body) -> bool:
        """检测请求体是否声明 "stream": true。cc 2.1.185 起 accept 头变成 application/json，
        流式标志只留在 body 里；只看请求头会把流式请求误判为非流式，导致
        client.send(stream=False) 死等整个响应跑完(可达 80~170s)才返回、期间一字节都不
        发给客户端，客户端约 60s 超时重发→错位。故必须据 body 判定。"""
        if not body:
            return False
        try:
            if b'"stream"' not in body:
                return False
            import re as _re
            return _re.search(rb'"stream"\s*:\s*true', body) is not None
        except Exception:
            return False

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
            # 同步构建（不再走 asyncio.to_thread）：高并发重试风暴下线程池会被占满，
            # 大响应的 base64 编码在 to_thread 里永久排队，导致这些请求日志丢失
            # （现象：jsonl 只剩小 body 的 429，200 大响应全部缺失）。
            log_entry = self._build_log_entry(
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
        # 自愈：writer task 不存在或已退出（异常死亡/被取消）时重启它，
        # 避免某次异常后日志永久停写（曾出现 task 静默退出后 jsonl 不再更新）
        if self._log_writer_task is None or self._log_writer_task.done():
            if self._log_writer_task is not None and not self._log_writer_task.cancelled():
                dead_exc = self._log_writer_task.exception()
                if dead_exc is not None:
                    print(f"日志写入任务已退出({type(dead_exc).__name__}: {dead_exc})，重启")
            try:
                self._log_writer_task = asyncio.create_task(self._log_writer_loop())
            except RuntimeError:
                # 无运行中的事件循环等极端情况，降级同步写入，至少不丢日志
                await asyncio.to_thread(self._append_log_entry, log_entry)
                return
        try:
            self._log_queue.put_nowait(log_entry)
        except asyncio.QueueFull:
            # 队列积压（writer 卡住/过慢）时降级同步写，避免阻塞请求收尾
            await asyncio.to_thread(self._append_log_entry, log_entry)

    async def _log_writer_loop(self):
        # 写日志在本后台串行 task 内直接同步执行，不再走 asyncio.to_thread：
        # 高并发重试风暴下线程池会被请求路径的 to_thread(日志构建/lb 统计等)占满，
        # 导致写日志的 to_thread 永久排队、jsonl 停写。后台单 task 串行直写文件开销很小。
        try:
            self._traffic_log_line_count = self._count_traffic_log_lines()
        except Exception as exc:
            print(f"统计流量日志行数失败，从 0 计: {exc}")
            self._traffic_log_line_count = 0
        while True:
            log_entry = await self._log_queue.get()
            try:
                if log_entry is None:
                    return
                self._append_log_entry(log_entry)
                self._traffic_log_line_count = (self._traffic_log_line_count or 0) + 1
                max_logs = self._get_log_limit()
                if self._traffic_log_line_count > max_logs + self.LOG_TRIM_EXTRA_LINES:
                    self._traffic_log_line_count = self._trim_traffic_log_to_limit(max_logs)
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

    def _apply_thinking_override(self, body: bytes, group_data: Dict[str, Any]) -> bytes:
        """按组覆写请求体的 thinking 参数。

        背景: anyrouter 等逆向中转拒绝 opus 的 "thinking: disabled" 请求并把拒绝包装成
        429 Service Unavailable (实测 adaptive 成功率 3/3、disabled 成功率 0/109)。cc 的
        subagent、websearch/webfetch 辅助请求为了快会关思考(disabled)，因而全被挡。开启
        本覆写后把这类请求的 thinking 规范化为模型支持的 on-mode，骗过中转校验。

        仅当组配置 "force_thinking" 为真值时生效，按模型选择:
          - opus / sonnet -> {"type": "adaptive"}                    (4.x 唯一 on-mode)
          - haiku         -> {"type": "enabled", budget_tokens:1024} (haiku 不支持 adaptive)
        已是 adaptive 的不动；非 Anthropic 模型 / 解析失败时原样返回。
        """
        if not body or not isinstance(group_data, dict):
            return body
        if not group_data.get('force_thinking'):
            return body
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body
        if not isinstance(data, dict):
            return body

        model = str(data.get('model') or '').lower()
        if not model:
            return body

        th = data.get('thinking')
        if isinstance(th, dict) and th.get('type') == 'adaptive':
            return body  # 已是 adaptive，无需改

        if 'haiku' in model:
            # haiku 只支持固定预算思考，budget 必须 >=1024 且 < max_tokens
            max_tokens = data.get('max_tokens')
            if isinstance(max_tokens, int) and max_tokens <= 1024:
                return body  # 放不下最小思考预算，不动
            data['thinking'] = {'type': 'enabled', 'budget_tokens': 1024}
        elif 'opus' in model or 'sonnet' in model or 'claude' in model:
            data['thinking'] = {'type': 'adaptive'}
        else:
            return body  # 非 Anthropic 模型不碰

        try:
            return json.dumps(data, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            return body

    @staticmethod
    def _strip_beta_flags(headers: Dict[str, str], strip_list) -> None:
        """从 anthropic-beta 头里移除指定 beta 标志(就地修改 headers)。

        anyrouter 后端较旧，在 web_search 服务端工具场景下遇到较新的 beta(实测
        thinking-token-count-2026-05-13)会返回 429;cc 2.1.154 不带该 beta 时同样的
        web_search 请求 200。剥掉上游不认识的新 beta，新版 cc 也能用 WebSearch。
        仅当组配置 "strip_beta" 非空时生效。
        """
        if not strip_list:
            return
        strip_set = {str(s).strip() for s in strip_list if str(s).strip()}
        if not strip_set:
            return
        for key in list(headers.keys()):
            if key.lower() == 'anthropic-beta':
                flags = [f.strip() for f in headers[key].split(',')]
                headers[key] = ','.join(f for f in flags if f and f not in strip_set)
                break

    def _relax_web_tool_choice(self, body: bytes, group_data: Dict[str, Any]) -> bytes:
        """移除带服务端 web 工具请求的强制 tool_choice。

        实测同一上游(anyrouter): cc 2.1.185 的 WebSearch 子请求带
        tool_choice={"type":"tool","name":"web_search"} 强制调用服务端工具 -> 429;
        cc 2.1.154 不带 tool_choice(auto)的同形请求 -> 200。anyrouter 拒绝"强制调用
        服务端 web 工具"。移除强制 tool_choice(退回 auto)让新版 cc 也能用 WebSearch。
        仅当组配置 web_tool_choice_fix 为真、且请求确实带 web_search/web_fetch 工具时生效。
        """
        if not body or not isinstance(group_data, dict):
            return body
        if not group_data.get('web_tool_choice_fix'):
            return body
        if b'web_search' not in body and b'web_fetch' not in body:
            return body
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body
        if not isinstance(data, dict):
            return body
        tc = data.get('tool_choice')
        if not (isinstance(tc, dict) and tc.get('type') == 'tool'):
            return body
        tools = data.get('tools')
        if not isinstance(tools, list):
            return body
        has_server_web = any(
            isinstance(t, dict) and str(t.get('type', '')).startswith(('web_search', 'web_fetch'))
            for t in tools
        )
        if not has_server_web:
            return body
        data.pop('tool_choice', None)
        try:
            return json.dumps(data, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            return body

    # anyrouter 2026-07 起按"客户端工具名列表"做请求指纹校验，识别的是 Claude Code
    # 的真实工具名(改名即 429)，schema 内容不校验(空 schema + 假描述可过)，因此
    # 用极简假体(~1.3KB)冒充即可。3 个名字不够、7 个够，留 8 个作余量。
    _PAD_TOOL_NAMES = ('Agent', 'AskUserQuestion', 'Bash', 'Edit',
                       'Glob', 'Grep', 'Read', 'Write')
    _PAD_TOOL_MIN_COUNT = 8

    def _pad_client_tools(self, body: bytes, group_data: Dict[str, Any]) -> bytes:
        """给工具表过小的请求补足 Claude Code 客户端工具名，骗过中转指纹校验。

        背景: anyrouter 2026-07-21 起收紧准入，要求请求"像 CC 主会话"——thinking 开启
        (由 force_thinking 负责) 且带足量【真名】客户端工具 schema，缺一即
        429 Service Unavailable。cc 的辅助请求(WebFetch 摘要/标题生成/WebSearch 子
        请求)工具表为空或只有服务端 web_search，因而全被拒。实测(直发对照):
          - 失败体 + adaptive + 7 个真名工具 -> 200；3 个 -> 429；改名的 7 个 -> 429
          - schema 换成空对象 + "不可用"描述 -> 仍 200 (校验只认名字)
          - 消息填充 90KB / 换模型 / 剥 beta 均无效 (非体积、非模型、非 beta 问题)

        仅当组配置 "tools_pad" 为真值时生效。工具数 >= _PAD_TOOL_MIN_COUNT 的请求
        (主会话/subagent)不动。填充工具描述明确"不可调用"；若原请求本无任何工具
        且未指定 tool_choice，额外设 tool_choice:{"type":"none"} 保证模型绝不误调
        (实测 anyrouter 接受 none)。非 Anthropic 模型 / 解析失败时原样返回。
        """
        if not body or not isinstance(group_data, dict):
            return body
        if not group_data.get('tools_pad'):
            return body
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body
        if not isinstance(data, dict):
            return body
        if 'claude' not in str(data.get('model') or '').lower():
            return body

        tools = data.get('tools')
        if not isinstance(tools, list):
            tools = []
        if len(tools) >= self._PAD_TOOL_MIN_COUNT:
            return body

        existing_names = {t.get('name') for t in tools if isinstance(t, dict)}
        pad = [
            {
                'name': name,
                'description': 'Not available in this session. Never invoke this tool.',
                'input_schema': {'type': 'object', 'properties': {}, 'required': []},
            }
            for name in self._PAD_TOOL_NAMES if name not in existing_names
        ]
        if not pad:
            return body

        had_no_tools = not tools
        data['tools'] = tools + pad
        if had_no_tools and 'tool_choice' not in data:
            data['tool_choice'] = {'type': 'none'}
        try:
            return json.dumps(data, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            return body

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

    @staticmethod
    def _coerce_positive_int(value: Any, default: int, minimum: int = 1) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number >= minimum else default

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {'1', 'true', 'yes', 'on'}:
                return True
            if normalized in {'0', 'false', 'no', 'off'}:
                return False
        return default

    @staticmethod
    def _coerce_status_codes(value: Any, default: set) -> set:
        if value is None:
            return set(default)
        if isinstance(value, (str, int)):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return set(default)

        codes = set()
        for item in value:
            try:
                codes.add(int(item))
            except (TypeError, ValueError):
                continue
        return codes or set(default)

    @staticmethod
    def _coerce_string_set(value: Any, default: set) -> set:
        if value is None:
            return set(default)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return set(default)

        items = {str(item).strip().lower() for item in value if str(item).strip()}
        return items if items else set(default)

    @staticmethod
    def _coerce_timestamp(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            timestamp = int(float(value))
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    def _get_key_retry_policy(self, group_name: Optional[str]) -> Dict[str, Any]:
        group_data: Dict[str, Any] = {}
        if group_name:
            group_data = self.config_manager.groups.get(group_name, {})

        return {
            'max_consecutive_fatal_failures': self._coerce_positive_int(
                group_data.get('max_consecutive_fatal_failures'),
                self.DEFAULT_MAX_CONSECUTIVE_FATAL_FAILURES,
            ),
            'max_consecutive_quota_failures': self._coerce_positive_int(
                group_data.get('max_consecutive_quota_failures'),
                self.DEFAULT_MAX_CONSECUTIVE_QUOTA_FAILURES,
            ),
            'max_key_retries_per_request': self._coerce_positive_int(
                group_data.get('max_key_retries_per_request'),
                self.DEFAULT_MAX_KEY_RETRIES_PER_REQUEST,
            ),
            'quota_status_codes': self._coerce_status_codes(
                group_data.get('quota_status_codes'),
                self.DEFAULT_QUOTA_STATUS_CODES,
            ),
            'quota_error_types': self._coerce_string_set(
                group_data.get('quota_error_types'),
                self.DEFAULT_QUOTA_ERROR_TYPES,
            ),
            'disable_quota_until_reset': self._coerce_bool(
                group_data.get('disable_quota_until_reset'),
                True,
            ),
        }

    def _parse_error_body(self, error_body: Optional[bytes]) -> Dict[str, Any]:
        text = ''
        if error_body:
            try:
                text = error_body.decode('utf-8', errors='ignore')
            except Exception:
                text = ''

        error_type = ''
        resets_at = None
        if text:
            try:
                payload = json.loads(text)
                error = payload.get('error') if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    error_type = str(error.get('type') or '').strip().lower()
                    resets_at = self._coerce_timestamp(error.get('resets_at'))
                    if resets_at is None:
                        resets_in_seconds = self._coerce_timestamp(error.get('resets_in_seconds'))
                        if resets_in_seconds is not None:
                            resets_at = int(time.time()) + resets_in_seconds
            except Exception:
                pass

        return {
            'text': text[:200],
            'error_type': error_type,
            'resets_at': resets_at,
        }

    @staticmethod
    def _is_quota_error(status_code: int, error_info: Dict[str, Any], retry_policy: Dict[str, Any]) -> bool:
        if status_code not in retry_policy.get('quota_status_codes', set()):
            return False
        quota_error_types = retry_policy.get('quota_error_types', set())
        error_type = str(error_info.get('error_type') or '').strip().lower()
        if not quota_error_types:
            return True
        return bool(error_type and error_type in quota_error_types)

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

        # 按组覆写 thinking 参数（针对 anyrouter 等只接受带 thinking 的中转）
        modified_body = self._apply_thinking_override(modified_body, group_data)
        # 按组移除强制 tool_choice（anyrouter 拒绝强制调用服务端 web 工具）
        modified_body = self._relax_web_tool_choice(modified_body, group_data)
        # 按组补足客户端工具名（anyrouter 按工具名指纹拒绝工具表过小的请求）
        modified_body = self._pad_client_tools(modified_body, group_data)

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

        # 按组剥离上游不兼容的 anthropic-beta 标志(如 anyrouter 在 web_search 下拒绝新 beta)
        strip_beta = group_data.get('strip_beta')
        if strip_beta:
            self._strip_beta_flags(headers, strip_beta)

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
            "stream" in x_stainless_helper_method or
            # cc 2.1.185 起 accept 头为 application/json、流式标志 "stream":true 只在 body：
            # 仅看请求头会误判为非流式，client.send(stream=False) 死等整个响应、期间不给
            # 客户端发字节 → 客户端约 60s 超时重发→错位。必须据 body 补判。
            self._body_requests_stream(filtered_body)
        )
        upstream_attempts_log = []

        try:
            # Key 错误感知重试: 遇到组配置的 fatal_status_codes 时禁用当前 key 并尝试下一个
            excluded_keys: set = set()
            response = None

            # 获取当前组的 fatal_status_codes（未配置则为空集，不触发自动禁用）
            fatal_codes = self._get_fatal_status_codes(active_group_name)
            retry_policy = self._get_key_retry_policy(active_group_name)
            fatal_response_count = 0
            consecutive_same_code = 0
            last_fatal_key = None

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
                error_info = self._parse_error_body(error_body)
                error_text = error_info.get('text', '')
                is_non_key_error = error_text.lstrip().startswith('<')  # HTML 响应不是 API 错误
                is_quota_error = self._is_quota_error(status_code, error_info, retry_policy)
                fatal_response_count += 1

                # 连续相同 key 故障类型计数；quota 429 和普通 429 使用不同阈值。
                current_fatal_key = (status_code, 'quota' if is_quota_error else 'fatal')
                if current_fatal_key == last_fatal_key:
                    consecutive_same_code += 1
                else:
                    consecutive_same_code = 1
                    last_fatal_key = current_fatal_key

                max_consecutive_failures = (
                    retry_policy['max_consecutive_quota_failures']
                    if is_quota_error
                    else retry_policy['max_consecutive_fatal_failures']
                )

                # 如果是非 key 级别错误、连续失败超限、或单请求重试超限，停止重试且不禁用当前 key
                if (
                    is_non_key_error
                    or consecutive_same_code >= max_consecutive_failures
                    or fatal_response_count >= retry_policy['max_key_retries_per_request']
                ):
                    if is_non_key_error:
                        print(f"[KEY重试] 检测到非API错误(HTML响应)，停止重试，不禁用 key")
                    elif fatal_response_count >= retry_policy['max_key_retries_per_request']:
                        print(
                            f"[KEY重试] 单请求已尝试 {fatal_response_count} 个 fatal key，"
                            f"达到 max_key_retries_per_request={retry_policy['max_key_retries_per_request']}，停止重试"
                        )
                    else:
                        failure_kind = "quota" if is_quota_error else "fatal"
                        print(
                            f"[KEY重试] 连续 {consecutive_same_code} 个 key 返回 HTTP {status_code} ({failure_kind})，"
                            f"达到阈值 {max_consecutive_failures}，停止重试"
                        )
                    break

                # 禁用当前 key
                excluded_keys.add(active_config_name)
                reason = f"HTTP {status_code}"
                if error_text:
                    reason = f"HTTP {status_code}: {error_text}"
                disabled_until = None
                if is_quota_error and retry_policy['disable_quota_until_reset']:
                    disabled_until = error_info.get('resets_at')
                await asyncio.to_thread(
                    self.config_manager.disable_key,
                    active_group_name,
                    active_config_name,
                    reason,
                    disabled_until,
                )
                await asyncio.to_thread(self._record_lb_result, active_config_name, status_code)

                # 尝试选下一个 key
                try:
                    target_url, target_headers, target_body, active_config_name, active_group_name = \
                        self.build_target_param(path, request, original_body, exclude_keys=excluded_keys)
                    filtered_body = await asyncio.to_thread(self.apply_request_filter, target_body)
                    fatal_codes = self._get_fatal_status_codes(active_group_name)
                    retry_policy = self._get_key_retry_policy(active_group_name)
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
            client_disconnected = False

            async def iterator():
                nonlocal response, status_code, response_truncated, total_response_bytes
                nonlocal first_chunk, lb_result_recorded, stream_error_info, stream_preface_retry_used
                nonlocal client_disconnected

                async def _watch_client_disconnect():
                    # 独立轮询客户端是否断开：覆盖 anyrouter 出首字后长时间静默、
                    # 转发循环卡在 aiter_bytes 等待、无法在循环体内自检的情况。
                    # 断开则关闭上游响应，令 aiter_bytes 抛错退出转发。
                    nonlocal client_disconnected
                    try:
                        while not client_disconnected:
                            await asyncio.sleep(self.CLIENT_DISCONNECT_POLL_SECONDS)
                            if await request.is_disconnected():
                                client_disconnected = True
                                try:
                                    await response.aclose()
                                except Exception:
                                    pass
                                return
                    except asyncio.CancelledError:
                        return

                disconnect_watcher = asyncio.ensure_future(_watch_client_disconnect())
                try:
                    while True:
                        stream_attempt_started = time.monotonic()
                        try:
                            # 手动驱动上游字节迭代：流式下给每次读取设保活超时。anyrouter
                            # 长 thinking 时 body 静默数十秒~数分钟，期间 clp 若一字节不发，
                            # cc(尤其 2.1.185+ 字节级 idle 看门狗)会判卡死、提前放弃并重发，
                            # 旧请求仍在后台跑完→"错位"。静默超阈值即向 cc 注入 SSE 注释保活帧；
                            # 用 asyncio.wait 持有读取任务、超时不取消它，避免打断 httpx 流。
                            byte_iter = response.aiter_bytes().__aiter__()
                            pending_read = None
                            while True:
                                if pending_read is None:
                                    pending_read = asyncio.ensure_future(byte_iter.__anext__())
                                if is_stream:
                                    done, _ = await asyncio.wait(
                                        {pending_read}, timeout=self.SSE_KEEPALIVE_SECONDS
                                    )
                                    if not done:
                                        if client_disconnected:
                                            pending_read.cancel()
                                            return
                                        # 上游仍静默：发 SSE 注释行保活(冒号开头，客户端按
                                        # 规范忽略内容，但收到字节即重置 idle 计时)。
                                        yield b": keepalive\n\n"
                                        continue
                                else:
                                    await asyncio.wait({pending_read})
                                try:
                                    chunk = pending_read.result()
                                except StopAsyncIteration:
                                    break
                                finally:
                                    pending_read = None

                                # watcher 已探测到客户端断开 → 立即停止转发
                                if client_disconnected:
                                    return
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
                            if client_disconnected:
                                # 上游响应被 watcher 主动关闭（客户端已断开），
                                # 不作为上游错误重试，直接结束（finally 记 499）
                                return
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
                    disconnect_watcher.cancel()
                    final_duration = int((time.time() - start_time) * 1000)
                    if client_disconnected:
                        # 客户端主动断开（如 cc 流式 watchdog abort）：记 499，非成功
                        final_status_code = 499
                    else:
                        final_status_code = 502 if stream_error_info else status_code

                    # 发送请求完成事件
                    try:
                        await self.realtime_hub.request_completed(
                            request_id=request_id,
                            status_code=final_status_code,
                            duration_ms=final_duration,
                            success=(not client_disconnected) and stream_error_info is None and 200 <= status_code < 400
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
