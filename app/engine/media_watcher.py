"""媒体状态监控器（轨道2） - 高频轮询设备播放状态，检测原生播放。

设计动机：
    轨道1（对话轮询）的延迟在 0.7~2.5s，加上小米服务器写入对话记录的延迟，
    小爱原生播放可能已经响 1~2 秒。本模块通过 200ms 高频轮询
    player_get_play_status，发现 status: 0→1 跳变且非 musicnest 自己触发
    时，立即 stop_all_media 并反查对话记录触发拦截。

工作流程：
    1. 每 200ms 调用 get_player_status
    2. 检测 status: 0→1 跳变
    3. 判断是否 musicnest 自己触发（_last_own_play_at 时间戳 + 3s 窗口）
       - 是 → 不干预
       - 否 → 触发"原生播放"拦截
    4. 拦截动作：
       a. 立即 stop_all_media（不等任何东西）
       b. 反查 monitor.get_last_query 拿最近 5 秒内的 query
       c. 若有匹配"播放XXX"的指令 → 调用注册的回调
       d. 若无 → 视为误判，不干预（避免打断用户主动唤醒的音乐）

与轨道1 协同：
    - 轨道1 处理过的 query 会 mark_query_handled
    - 轨道2 检测到原生播放时先检查 is_query_handled，避免重复触发
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Awaitable, Callable, Optional

from app.miot.client import MinaHTTPClient

logger = logging.getLogger(__name__)

# 默认轮询间隔（秒）— 200ms 平衡了响应速度和服务器压力
DEFAULT_WATCH_INTERVAL = 0.2

# "自己触发的播放"判定窗口（秒）— 30 秒
# 覆盖转码5秒 + 音箱请求URL延迟14秒 + 播放启动缓冲10秒，避免MediaWatcher误判自己的播放
OWN_PLAY_WINDOW_SEC = 30.0

# 反查对话记录的窗口（秒）— 扩大到 30 秒，覆盖转码+播放启动的全流程
RECENT_QUERY_WINDOW_SEC = 30.0

# 连续失败多少次后暂停 watcher
MAX_CONSECUTIVE_FAILURES = 20

# 连续失败暂停后的冷却时间（秒）— 冷却结束后重置计数器重试
FAILURE_COOLDOWN_SEC = 60.0

# 拦截冷却时间（秒）— 防止短时间内重复触发
INTERCEPT_COOLDOWN_SEC = 5.0


class DevicePlayState(Enum):
    """设备播放状态机"""
    IDLE = "idle"             # status=0 或解析失败
    OWN_PLAYING = "own"       # status=1 且 musicnest 自己触发
    NATIVE_PLAYING = "native" # status=1 且非自己触发（小爱原生播放）


# 拦截回调类型: async func(device_id, query) -> None
# query 是反查到的最近对话 query（可能为 None 表示没查到）
InterceptCallback = Callable[[str, Optional[str]], Awaitable[None]]


class MediaWatcher:
    """播放状态监控器（轨道2：反应层）

    高频轮询设备播放状态，检测小爱原生播放并立即拦截。
    职责：快速反应，作为轨道1 的兜底。
    """

    def __init__(
        self,
        client: MinaHTTPClient,
        monitor: "ConversationMonitor",
        poll_interval: float = DEFAULT_WATCH_INTERVAL,
    ):
        self._client = client
        self._monitor = monitor  # 用于反查最近对话
        self._poll_interval = max(0.1, min(5.0, poll_interval))

        self._enabled = False
        self._task: Optional[asyncio.Task] = None

        # 每设备状态
        self._device_states: dict[str, DevicePlayState] = {}
        self._device_selected: dict[str, bool] = {}  # device_id -> 是否勾选
        self._last_status: dict[str, int] = {}  # device_id -> 上次 status 值

        # 拦截回调列表
        self._intercept_callbacks: list[tuple[str, InterceptCallback]] = []

        # 拦截冷却（防止重复触发）
        self._last_intercept_at: dict[str, float] = {}

        # 连续失败计数（用于自动暂停）
        self._consecutive_failures: dict[str, int] = {}

        # 连续失败暂停时间戳（用于冷却后重试）
        self._paused_at: dict[str, float] = {}

        # 首轮预热标志（per-device：首次读取只记录状态，不触发拦截）
        self._device_initialized: dict[str, bool] = {}

    @property
    def is_running(self) -> bool:
        return self._enabled and self._task is not None

    def register_intercept_callback(self, name: str, cb: InterceptCallback) -> None:
        """注册拦截回调（去重名称）"""
        self._intercept_callbacks = [(n, c) for n, c in self._intercept_callbacks if n != name]
        self._intercept_callbacks.append((name, cb))

    def unregister_intercept_callback(self, name: str) -> None:
        self._intercept_callbacks = [(n, c) for n, c in self._intercept_callbacks if n != name]

    async def start(self, devices: list[dict]) -> None:
        """启动 watcher

        Args:
            devices: 设备列表 [{"deviceID": "...", "name": "..."}, ...]
        """
        if self._enabled:
            return

        from app.config import config as app_config
        selections: dict = app_config.get("device_selections", {})

        for d in devices:
            did = d.get("deviceID", "")
            if not did:
                continue
            self._device_states[did] = DevicePlayState.IDLE
            self._last_status[did] = 0
            self._device_selected[did] = bool(selections.get(did, False))
            self._consecutive_failures[did] = 0
            self._paused_at[did] = 0.0
            self._device_initialized[did] = False

        enabled_count = sum(1 for v in self._device_selected.values() if v)
        if enabled_count == 0:
            logger.warning("[MediaWatcher] 无勾选设备，watcher 仍启动但不会干预任何设备")
        else:
            logger.info(f"[MediaWatcher] {enabled_count} 个设备待监控，轮询间隔 {self._poll_interval}s")

        self._enabled = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        self._enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "poll_interval": self._poll_interval,
            "devices": [
                {
                    "device_id": did,
                    "state": state.value,
                    "last_status": self._last_status.get(did, 0),
                    "failures": self._consecutive_failures.get(did, 0),
                }
                for did, state in self._device_states.items()
            ],
        }

    async def _watch_loop(self) -> None:
        """主循环"""
        from app.miot.token_refresh import is_token_invalid
        _token_invalid_logged = False
        backoff = self._poll_interval
        while self._enabled:
            # token 失效时暂停轮询，避免疯狂 401（每30秒检查一次是否已重新登录）
            if is_token_invalid():
                if not _token_invalid_logged:
                    logger.warning("[MediaWatcher] token 已失效，暂停轮询等待重新登录")
                    _token_invalid_logged = True
                await asyncio.sleep(30)
                continue
            _token_invalid_logged = False
            try:
                await self._watch_all_devices()
                backoff = self._poll_interval
            except Exception as e:
                logger.warning(f"[MediaWatcher] _watch_all_devices 异常: {e}", exc_info=True)
                backoff = min(backoff * 2, 5.0)
            await asyncio.sleep(backoff)

    async def _watch_all_devices(self) -> None:
        """轮询所有勾选设备的状态"""
        selected_ids = [
            did for did, selected in self._device_selected.items() if selected
        ]
        if not selected_ids:
            return
        await asyncio.gather(
            *[self._watch_device(did) for did in selected_ids],
            return_exceptions=True
        )

    async def _watch_device(self, device_id: str) -> None:
        """监控单个设备的状态变化"""
        # 失败次数过多，暂停该设备监控一段时间
        if self._consecutive_failures.get(device_id, 0) >= MAX_CONSECUTIVE_FAILURES:
            # 冷却结束后重置计数器重试
            paused_at = self._paused_at.get(device_id, 0)
            if paused_at > 0 and time.time() - paused_at >= FAILURE_COOLDOWN_SEC:
                logger.info(
                    "[MediaWatcher] 设备 %s 冷却完成，重置失败计数器重试",
                    device_id[:12]
                )
                self._consecutive_failures[device_id] = 0
                self._paused_at[device_id] = 0.0
            else:
                return

        try:
            raw = await self._client.get_player_status(device_id)
        except Exception as e:
            self._consecutive_failures[device_id] = self._consecutive_failures.get(device_id, 0) + 1
            if self._consecutive_failures[device_id] == MAX_CONSECUTIVE_FAILURES:
                self._paused_at[device_id] = time.time()
                logger.error(
                    "[MediaWatcher] 设备 %s 连续失败 %d 次，暂停监控",
                    device_id[:12], MAX_CONSECUTIVE_FAILURES
                )
            return

        # 解析 status
        current_status = self._parse_status(raw)
        if current_status is None:
            self._consecutive_failures[device_id] = self._consecutive_failures.get(device_id, 0) + 1
            return

        # 失败计数清零
        self._consecutive_failures[device_id] = 0

        prev_status = self._last_status.get(device_id, 0)
        self._last_status[device_id] = current_status

        # 首轮预热：只记录状态，不触发事件（避免对正在播放的设备触发误拦截）
        if not self._device_initialized.get(device_id, False):
            self._device_initialized[device_id] = True
            return

        # 检测 status: 0→1 跳变
        if prev_status != 1 and current_status == 1:
            await self._on_playback_started(device_id)

    async def _on_playback_started(self, device_id: str) -> None:
        """检测到设备开始播放（status: 0→1）"""
        # 拦截冷却检查
        now = time.time()
        last_intercept = self._last_intercept_at.get(device_id, 0)
        if now - last_intercept < INTERCEPT_COOLDOWN_SEC:
            logger.debug(
                "[MediaWatcher] 设备 %s 拦截冷却中（%.1fs < %.1fs），跳过",
                device_id[:12], now - last_intercept, INTERCEPT_COOLDOWN_SEC
            )
            return

        # 判断是否 musicnest 自己触发
        if self._client.is_own_play_recent(device_id, OWN_PLAY_WINDOW_SEC):
            # 自己触发的播放，不干预
            self._device_states[device_id] = DevicePlayState.OWN_PLAYING
            self._last_intercept_at[device_id] = now
            logger.debug(
                "[MediaWatcher] 设备 %s 自己触发的播放，不干预",
                device_id[:12]
            )
            return

        # 检测到原生播放！先反查对话记录，确认需要拦截才 stop（避免误杀自己的播放）
        self._device_states[device_id] = DevicePlayState.NATIVE_PLAYING
        logger.warning(
            "[MediaWatcher] ⚠️ 检测到设备 %s 疑似原生播放，反查对话确认...",
            device_id[:12]
        )

        # 1. 反查最近未处理的对话记录（先查，不立即 stop）
        recent_query = self._monitor.get_last_unhandled_query(
            device_id, within_sec=RECENT_QUERY_WINDOW_SEC
        )

        if not recent_query:
            # 没有最近的对话记录，可能是用户主动唤醒的，不干预也不 stop
            logger.info(
                "[MediaWatcher] 设备 %s 原生播放但无最近对话，视为用户主动唤醒，不干预",
                device_id[:12]
            )
            self._last_intercept_at[device_id] = now
            return

        # 2. 检查是否已被轨道1 处理过
        if self._monitor.is_query_handled(device_id, recent_query, within_sec=RECENT_QUERY_WINDOW_SEC):
            logger.info(
                "[MediaWatcher] 设备 %s 原生播放，但 query %r 已被轨道1处理，跳过",
                device_id[:12], recent_query[:40]
            )
            self._last_intercept_at[device_id] = now
            return

        # 3. 确认需要拦截：先 stop 所有媒体通道，确保完成后再触发回调
        logger.info(
            "[MediaWatcher] 设备 %s 确认拦截: query=%r",
            device_id[:12], recent_query[:80]
        )
        await self._client.stop_all_media(device_id)

        # 4. 触发拦截回调
        self._last_intercept_at[device_id] = now
        for name, cb in self._intercept_callbacks:
            try:
                await cb(device_id, recent_query)
            except Exception as e:
                logger.error(
                    "[MediaWatcher] 拦截回调 %s 执行异常: %s", name, e, exc_info=True
                )

        # 拦截后标记 query 为已处理，避免轨道 1 重复触发
        if self._monitor:
            self._monitor.mark_query_handled(device_id, recent_query)

    @staticmethod
    def _parse_status(raw) -> Optional[int]:
        """从 player_get_play_status 响应中解析 status 字段

        响应格式: { data: { info: '{"status":1,"play_song_detail":{...}}' } }
        """
        if not isinstance(raw, dict):
            return None

        data = raw.get("data")
        info = None
        if isinstance(data, dict):
            info = data.get("info")
        if info is None:
            info = raw.get("info")

        if not isinstance(info, str):
            return None

        try:
            import json
            parsed = json.loads(info)
            if isinstance(parsed, dict) and "status" in parsed:
                status = parsed["status"]
                if isinstance(status, (int, float)):
                    return int(status)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        return None
