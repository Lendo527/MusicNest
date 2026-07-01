"""对话监控器（轨道1） - 定时轮询小爱对话记录，检测新消息。

优化点：
  - 默认轮询间隔 0.5s → 0.2s（快速捕获语音指令）
  - 时间窗口 2s → 30s（不丢消息，避免时钟漂移导致漏拦）
  - _poll_loop 异常不再静默，logger.warning 输出
  - 多条新消息全部触发回调（不只取最新一条）
  - _initialized 字段真正生效：首次拉取跳过历史消息
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from app.miot.client import MinaHTTPClient

logger = logging.getLogger(__name__)

# 回调类型: async func(device_id, message) -> None
MessageCallback = Callable[[str, dict], Awaitable[None]]

# 默认轮询间隔（秒）— 0.2s，比原版 0.5s 更快捕获
DEFAULT_POLL_INTERVAL = 0.2

# 时间窗口（秒）— 放宽到 30s，避免时钟漂移导致漏拦
MESSAGE_TTL_SEC = 30.0


class ConversationMonitor:
    """小爱音箱对话监控器（轨道1：语义层）

    定时轮询设备对话记录，去重后触发回调。
    职责：理解"用户说了什么"。
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

    def get_last_query(self, device_id: str, within_sec: float = 5.0) -> Optional[str]:
        """获取设备最近 within_sec 秒内的对话 query（供轨道2反查使用）"""
        cutoff_ms = int((time.time() - within_sec) * 1000)
        for m in reversed(self._message_buffer):
            if m.get("device_id") == device_id and m.get("timestamp_ms", 0) >= cutoff_ms:
                return m.get("query", "")
        return None

    def mark_query_handled(self, device_id: str, query: str) -> None:
        """标记某个 query 已被处理（供轨道2避免重复触发）"""
        # 简单实现：写入一个标记字段，下次 get_last_query 跳过它
        # 这里通过在 _message_buffer 中加 handled 标记实现
        for m in reversed(self._message_buffer):
            if (m.get("device_id") == device_id
                and m.get("query", "") == query
                and not m.get("handled", False)):
                m["handled"] = True
                break

    def is_query_handled(self, device_id: str, query: str, within_sec: float = 5.0) -> bool:
        """检查某个 query 是否已被轨道1处理过"""
        cutoff_ms = int((time.time() - within_sec) * 1000)
        for m in reversed(self._message_buffer):
            if (m.get("device_id") == device_id
                and m.get("timestamp_ms", 0) >= cutoff_ms
                and m.get("query", "") == query):
                return m.get("handled", False)
        return False

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
        """轮询循环（异常不静默）"""
        while self._enabled:
            try:
                await self._poll_all()
            except Exception as e:
                logger.warning(f"[Monitor] _poll_all 异常: {e}", exc_info=True)
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

        逻辑：
          1. 首次拉取：只更新 _last_timestamps，不触发回调（避免启动时回放历史）
          2. 后续拉取：时间戳去重 + 缓冲区记录 + 30s 时间窗口过滤 + 多条全部触发
        """
        info = self._device_info.get(device_id, {})
        hardware = info.get("hardware", "")

        # 获取对话记录
        messages = await self._client.get_latest_ask(device_id, hardware, limit=5)

        if not messages:
            return

        # 首次拉取：只更新 _last_timestamps，不触发回调
        if not self._initialized.get(device_id, False):
            max_ts = max((m.get("timestamp_ms", 0) for m in messages), default=0)
            self._last_timestamps[device_id] = max_ts
            self._initialized[device_id] = True
            logger.info(
                "[Monitor] 设备 %s 首次拉取完成，跳过 %d 条历史消息，last_ts=%d",
                device_id[:12], len(messages), max_ts
            )
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
                "handled": False,
            })

            # 取最大时间戳防止乱序消息遗漏
            if ts > max_ts:
                max_ts = ts

            # 每条消息独立判断时间窗口（30s，避免时钟漂移导致漏拦）
            now_ms = int(time.time() * 1000)
            if now_ms - ts > MESSAGE_TTL_SEC * 1000:
                logger.debug(
                    "[Monitor] 消息超时跳过回调: device=%s ts=%d age=%.2fs query=%r",
                    device_id[:12], ts, (now_ms - ts) / 1000, msg.get("query", "")[:60]
                )
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

        # 触发回调：多条消息全部触发（按时间顺序）
        for msg in new_messages:
            logger.info(
                "[Monitor] 触发回调: device=%s query=%r",
                device_id[:12], msg.get("query", "")[:80]
            )
            for name, cb in self._callbacks:
                try:
                    await cb(device_id, msg)
                except Exception as e:
                    logger.error(
                        "[Monitor] 回调 %s 执行异常: %s", name, e, exc_info=True
                    )
