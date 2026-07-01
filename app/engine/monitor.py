"""对话监控器 - 定时轮询小爱对话记录，检测新消息"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from app.miot.client import MinaHTTPClient

logger = logging.getLogger(__name__)

# 回调类型: async func(device_id, message) -> None
MessageCallback = Callable[[str, dict], Awaitable[None]]

# 默认轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 0.5


class ConversationMonitor:
    """小爱音箱对话监控器

    定时轮询设备对话记录，去重后触发回调。
    轮询间隔可配置，默认 0.5s（比原版 1s 更快捕获语音指令）。
    """

    def __init__(self, client: MinaHTTPClient, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self._client = client
        self._poll_interval = max(0.1, min(30.0, poll_interval))  # 限制范围 0.1s~30s
        self._enabled = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[tuple[str, MessageCallback]] = []
        self._last_timestamps: dict[str, int] = {}  # device_id -> last_timestamp_ms
        self._initialized: dict[str, bool] = {}  # device_id -> 是否已完成首次拉取
        self._message_buffer: deque = deque(maxlen=200)  # 环形缓冲区
        self._device_info: dict[str, dict] = {}  # device_id -> {hardware, name}

    @property
    def is_running(self) -> bool:
        return self._enabled and self._task is not None

    def register_callback(self, name: str, cb: MessageCallback) -> None:
        """注册回调（去重名称）"""
        self._callbacks = [(n, c) for n, c in self._callbacks if n != name]
        self._callbacks.append((name, cb))

    def unregister_callback(self, name: str) -> None:
        self._callbacks = [(n, c) for n, c in self._callbacks if n != name]

    async def start(self, devices: Optional[list[dict]] = None) -> None:
        """启动监控

        Args:
            devices: 设备列表 [{"deviceID": "...", "hardware": "...", "name": "..."}, ...]
        """
        if self._enabled:
            return

        if devices:
            from app.config import config as app_config
            selections: dict = app_config.get("device_selections", {})
            selected_count = 0
            for d in devices:
                did = d.get("deviceID", "")
                self._last_timestamps.setdefault(did, 0)
                self._initialized[did] = False  # 标记该设备待首次拉取
                self._device_info[did] = {
                    "hardware": d.get("hardware", ""),
                    "name": d.get("name", ""),
                }
                if selections.get(did, False):
                    selected_count += 1
            if selected_count == 0:
                logger.warning(f"[Monitor] 共 {len(devices)} 个设备，但无一勾选，请前往设备管理页勾选需要监控的设备")
            else:
                logger.info(f"[Monitor] {selected_count}/{len(devices)} 个设备已勾选，仅监控已勾选设备")

        self._enabled = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """停止监控"""
        self._enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_messages(self, limit: int = 50, since_ts: int = 0) -> list[dict]:
        """获取消息记录"""
        result = list(self._message_buffer)
        if since_ts > 0:
            result = [m for m in result if m.get("timestamp_ms", 0) > since_ts]
        if limit > 0 and len(result) > limit:
            result = result[-limit:]
        return result

    def get_status(self) -> dict:
        """获取监控状态"""
        return {
            "is_running": self.is_running,
            "device_count": len(self._last_timestamps),
            "message_count": len(self._message_buffer),
            "devices": [
                {
                    "device_id": did,
                    "name": self._device_info.get(did, {}).get("name", ""),
                    "last_ts": ts,
                }
                for did, ts in self._last_timestamps.items()
            ],
        }

    async def _poll_loop(self) -> None:
        """轮询循环"""
        while self._enabled:
            try:
                await self._poll_all()
            except Exception:
                pass
            await asyncio.sleep(self._poll_interval)

    async def _poll_all(self) -> None:
        """轮询所有已勾选的设备（跳过未勾选的）"""
        from app.config import config as app_config
        selections: dict = app_config.get("device_selections", {})
        for device_id in list(self._last_timestamps.keys()):
            # 仅轮询已勾选的设备
            if not selections.get(device_id, False):
                continue
            await self._poll_device(device_id)

    async def _poll_device(self, device_id: str) -> None:
        """轮询单个设备的对话记录

        统一逻辑：每条记录先比时间戳去重，通过后缓冲并更新 last_ts，
        再判断是否在 2 秒窗口内，超时跳过不触发回调。
        仅处理最新一条新消息触发回调。
        """
        info = self._device_info.get(device_id, {})
        hardware = info.get("hardware", "")

        # 获取对话记录
        messages = await self._client.get_latest_ask(device_id, hardware, limit=5)

        if not messages:
            return

        last_ts = self._last_timestamps.get(device_id, 0)
        new_messages = []
        max_ts = last_ts

        for msg in messages:
            ts = msg.get("timestamp_ms", 0)

            # 跳过已处理过的记录（基于时间戳去重）
            if ts <= last_ts:
                continue

            # 新记录：存入仪表盘缓冲区
            self._message_buffer.append({
                "device_id": device_id,
                "device_name": info.get("name", ""),
                "timestamp_ms": ts,
                "query": msg.get("query", ""),
                "answer": msg.get("answer", ""),
            })

            # 取最大时间戳防止乱序消息遗漏
            if ts > max_ts:
                max_ts = ts

            # 每条消息独立判断时间窗口（避免累积耗时导致偏差）
            now_ms = int(time.time() * 1000)
            # 跳过过期指令（早于当前时间2秒以上），不触发回调
            if now_ms - ts > 2000:
                continue

            new_messages.append(msg)

        # 更新最后时间戳为最大值
        if max_ts > last_ts:
            self._last_timestamps[device_id] = max_ts

        if not new_messages:
            return

        logger.info(
            "[Monitor] 发现新消息: device=%s count=%d",
            device_id[:12], len(new_messages)
        )

        # 触发回调：仅处理最新一条
        latest_msg = new_messages[-1]
        logger.info(
            "[Monitor] 触发回调: device=%s query=%r",
            device_id[:12], latest_msg.get("query", "")[:80]
        )
        for name, cb in self._callbacks:
            try:
                await cb(device_id, latest_msg)
            except Exception as e:
                logger.error(
                    "[Monitor] 回调 %s 执行异常: %s", name, e, exc_info=True
                )
