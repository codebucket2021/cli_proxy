#!/usr/bin/env python3
"""
实时请求事件管理中心
负责管理 WebSocket 连接和广播实时请求事件
"""
import asyncio
import json
import uuid
from typing import Dict, List, Set, Optional
from fastapi import WebSocket
from dataclasses import dataclass, asdict
from datetime import datetime
import logging
import traceback

@dataclass
class RealTimeRequest:
    """实时请求状态数据类"""
    request_id: str
    service: str
    channel: str
    method: str
    path: str
    start_time: str  # ISO格式
    status: str  # PENDING/STREAMING/COMPLETED/FAILED
    duration_ms: int = 0
    status_code: Optional[int] = None
    request_headers: Optional[Dict] = None
    response_chunks: Optional[List[str]] = None
    response_chunks_total_len: int = 0
    response_truncated: bool = False
    target_url: Optional[str] = None

    def __post_init__(self):
        if self.response_chunks is None:
            self.response_chunks = []
        if self.request_headers is None:
            self.request_headers = {}

class RealTimeRequestHub:
    """实时请求事件管理中心"""

    def __init__(self, service_name: str, max_requests: int = 100):
        self.service_name = service_name
        self.max_requests = max_requests
        self.connections: Set[WebSocket] = set()
        self.active_requests: Dict[str, RealTimeRequest] = {}
        self.logger = logging.getLogger(f"realtime.{service_name}")

        # 设置日志级别
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    async def connect(self, websocket: WebSocket):
        """新连接接入"""
        try:
            await websocket.accept()
            self.connections.add(websocket)
            self.logger.info(f"WebSocket connected, total: {len(self.connections)}")

            # 发送当前活跃请求快照
            await self._send_snapshot(websocket)
        except Exception as e:
            self.logger.error(f"WebSocket 连接失败: {e}")
            raise

    def disconnect(self, websocket: WebSocket):
        """连接断开"""
        self.connections.discard(websocket)
        self.logger.info(f"WebSocket disconnected, total: {len(self.connections)}")

    async def _send_snapshot(self, websocket: WebSocket):
        """发送当前活跃请求快照"""
        if not self.active_requests:
            return

        try:
            for request in list(self.active_requests.values()):
                await websocket.send_text(json.dumps({
                    "type": "snapshot",
                    **asdict(request)
                }, ensure_ascii=False))
        except Exception as e:
            self.logger.error(f"发送快照失败: {e}")

    async def broadcast_event(self, event_type: str, request_id: str, **data):
        """广播事件到所有连接"""
        if not self.connections:
            return

        event_data = {
            "type": event_type,
            "request_id": request_id,
            "service": self.service_name,
            "timestamp": datetime.now().isoformat(),
            **data
        }

        message = json.dumps(event_data, ensure_ascii=False)
        disconnected = set()

        # 遍历连接集合的快照，避免广播期间另一个请求 add/remove 连接
        # 导致 "Set changed size during iteration" 崩溃
        for i, connection in enumerate(list(self.connections)):
            try:
                await connection.send_text(message)
            except Exception as e:
                self.logger.warning(f"发送消息失败: {e}")
                disconnected.add(connection)

        # 清理断开的连接
        if disconnected:
            self.connections -= disconnected
            self.logger.info(f"清理了 {len(disconnected)} 个断开的连接")


    async def request_started(self, request_id: str, method: str, path: str,
                            channel: str, headers: Dict, target_url: str = None):
        """记录请求开始"""
        try:

            request = RealTimeRequest(
                request_id=request_id,
                service=self.service_name,
                channel=channel,
                method=method,
                path=path,
                start_time=datetime.now().isoformat(),
                status="PENDING",
                request_headers=self._sanitize_headers(headers),
                target_url=target_url
            )

            self.active_requests[request_id] = request
            self._cleanup_old_requests()

            # 避免 request_id 参数冲突，从 asdict 结果中排除它
            request_data = asdict(request)
            request_data.pop('request_id', None)  # 移除 request_id 避免冲突
            await self.broadcast_event("started", request_id, **request_data)
            self.logger.debug(f"请求开始: {request_id} - {method} {path}")
        except Exception as e:
            self.logger.error(f"记录请求开始失败: {e}\n{traceback.format_exc()}")

    async def request_streaming(self, request_id: str, duration_ms: int):
        """标记请求进入流式状态"""
        try:
            if request_id in self.active_requests:
                self.active_requests[request_id].status = "STREAMING"
                self.active_requests[request_id].duration_ms = duration_ms

                await self.broadcast_event("progress", request_id,
                                         status="STREAMING", duration_ms=duration_ms)
                self.logger.debug(f"请求流式状态: {request_id} - {duration_ms}ms")
        except Exception as e:
            self.logger.error(f"更新流式状态失败: {e}")

    async def response_chunk(self, request_id: str, chunk: str, duration_ms: int):
        """添加响应数据块"""
        try:
            if request_id not in self.active_requests:
                return

            request = self.active_requests[request_id]

            # 限制单个响应的总长度，避免内存爆炸。
            # 用累加长度而非每次 sum(所有 chunk)：后者是 O(n)、整条流累积成 O(n²)，
            # 且本方法在转发关键路径上(base_proxy 先 await 本方法、再 yield 给客户端)，
            # 长响应到后期会越拖越慢，导致客户端 idle 超时、收不到完整响应而重试。
            if request.response_chunks_total_len < 2 * 1024 * 1024:  # 2MB限制
                request.response_chunks.append(chunk)
                request.response_chunks_total_len += len(chunk)
            else:
                if not request.response_truncated:
                    request.response_truncated = True
                    request.response_chunks.append("[...响应过长，已截断...]")

            request.duration_ms = duration_ms

            # 只发送非空的有意义的chunk
            if chunk.strip():
                await self.broadcast_event("progress", request_id,
                                         response_delta=chunk,
                                         duration_ms=duration_ms,
                                         response_truncated=request.response_truncated)
        except Exception as e:
            self.logger.error(f"处理响应块失败: {e}")

    async def request_completed(self, request_id: str, status_code: int,
                              duration_ms: int, success: bool = True):
        """标记请求完成"""
        try:
            if request_id not in self.active_requests:
                return

            request = self.active_requests[request_id]
            request.status = "COMPLETED" if success else "FAILED"
            request.status_code = status_code
            request.duration_ms = duration_ms

            await self.broadcast_event("completed" if success else "failed",
                                     request_id,
                                     status=request.status,
                                     status_code=status_code,
                                     duration_ms=duration_ms)

            self.logger.debug(f"请求完成: {request_id} - {status_code} - {duration_ms}ms")

            # 延迟清理已完成的请求，让前端有时间显示
            asyncio.create_task(self._delayed_cleanup(request_id, 30))  # 30秒后清理
        except Exception as e:
            self.logger.error(f"标记请求完成失败: {e}")

    async def _delayed_cleanup(self, request_id: str, delay_seconds: int):
        """延迟清理请求"""
        try:
            await asyncio.sleep(delay_seconds)
            if request_id in self.active_requests:
                self.active_requests.pop(request_id, None)
                self.logger.debug(f"清理请求: {request_id}")
        except Exception as e:
            self.logger.error(f"延迟清理失败: {e}")

    def _cleanup_old_requests(self):
        """清理过多的历史请求"""
        try:
            if len(self.active_requests) > self.max_requests:
                # 保留最新的请求
                sorted_requests = sorted(
                    self.active_requests.items(),
                    key=lambda x: x[1].start_time,
                    reverse=True
                )

                old_count = len(self.active_requests)
                self.active_requests = dict(sorted_requests[:self.max_requests])
                cleaned_count = old_count - len(self.active_requests)

                if cleaned_count > 0:
                    self.logger.info(f"清理了 {cleaned_count} 个旧请求")
        except Exception as e:
            self.logger.error(f"清理旧请求失败: {e}")

    def _sanitize_headers(self, headers: Dict) -> Dict:
        """清理敏感的请求头"""
        if not headers:
            return {}

        try:
            sensitive_headers = {'authorization', 'x-api-key', 'cookie'}
            return {
                k: v if k.lower() not in sensitive_headers else "[已隐藏]"
                for k, v in headers.items()
            }
        except Exception as e:
            self.logger.error(f"清理请求头失败: {e}")
            return {}

    def get_connection_count(self) -> int:
        """获取当前连接数"""
        return len(self.connections)

    def get_active_request_count(self) -> int:
        """获取活跃请求数"""
        return len(self.active_requests)