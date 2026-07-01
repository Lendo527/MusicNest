"""播放状态机 — PlaylistManager for per-device playback management.

移植自 TypeScript: songloft-plugin-miot/src/player/manager.ts
适配 Python asyncio 异步模型.
"""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ===== Enums =====


class PlayState(Enum):
    """播放状态"""
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class PlayMode(Enum):
    """播放模式"""
    ORDER = "order"    # 顺序播放，到末尾停止
    LOOP = "loop"      # 列表循环
    SINGLE = "single"  # 单曲循环
    RANDOM = "random"  # 随机不重复


# ===== PlaylistManager =====


class PlaylistManager:
    """单设备播放管理器.

    管理播放状态机、播放模式切换、定时切歌、语音打断恢复.
    """

    def __init__(self, miot_client: "MinaHTTPClient", device_id: str = ""):
        """初始化播放管理器.

        Args:
            miot_client: MinaHTTPClient 实例，用于设备通信.
            device_id: 设备ID，可通过 set_device_id 后续设置.
        """
        self._miot = miot_client
        self._device_id = device_id

        # 播放状态
        self._state: PlayState = PlayState.IDLE
        self._mode: PlayMode = PlayMode.ORDER

        # 队列
        self._songs: list[dict] = []          # [{title, url, duration}]
        self._current_index: int = 0
        self._playlist_id: int = 0

        # 定时器
        self._auto_next_task: Optional[asyncio.Task] = None  # 自动切歌 asyncio Task
        self._play_start_time: float = 0.0                    # 当前歌曲开始时间戳(秒)

        # 随机模式
        self._random_played: set[int] = set()

        # 语音挂起
        self._voice_suspended_at: float = 0.0

    # ===== 属性 =====

    @property
    def state(self) -> PlayState:
        return self._state

    @property
    def mode(self) -> PlayMode:
        return self._mode

    @property
    def device_id(self) -> str:
        return self._device_id

    def set_device_id(self, device_id: str) -> None:
        """设置/更新设备ID."""
        self._device_id = device_id

    # ===== 公开方法 =====

    async def play(self, playlist_id: int, start_index: int = 0,
                   mode: PlayMode = PlayMode.ORDER, songs: Optional[list[dict]] = None) -> bool:
        """播放歌单.

        Args:
            playlist_id: 歌单ID.
            start_index: 起始歌曲索引.
            mode: 播放模式.
            songs: 预加载的歌曲列表; 若为 None 则需先通过 set_queue 设置.

        Returns:
            是否成功.
        """
        self._stop_auto_next()
        self._state = PlayState.IDLE
        self._play_start_time = 0.0

        # 使用传入的歌曲列表或已有的队列
        if songs is not None:
            self._songs = list(songs)
        elif not self._songs:
            logger.warning("PlaylistManager: No songs in queue")
            return False

        if not self._songs:
            logger.warning(f"PlaylistManager: Playlist is empty id={playlist_id}")
            return False

        self._playlist_id = playlist_id
        self._current_index = start_index if 0 <= start_index < len(self._songs) else 0
        self._mode = mode
        self._random_played = set()

        ok = await self._play_current()
        if not ok:
            logger.error("PlaylistManager: Failed to play current song")
            return False

        logger.info(
            f"PlaylistManager: Playlist started id={playlist_id} "
            f"index={self._current_index} mode={self._mode.value} total={len(self._songs)}"
        )
        return True

    async def stop(self) -> None:
        """停止播放."""
        self._stop_auto_next()
        self._clear_voice_suspend()
        self._state = PlayState.STOPPED
        self._play_start_time = 0.0

        if self._device_id:
            await self._miot.player_stop(self._device_id)

        logger.info("PlaylistManager: Playback stopped")

    async def pause(self) -> None:
        """暂停播放."""
        self._stop_auto_next()
        self._clear_voice_suspend()
        self._state = PlayState.PAUSED

        if self._device_id:
            await self._miot.player_pause(self._device_id)

        logger.info("PlaylistManager: Playback paused")

    async def resume(self) -> bool:
        """恢复播放 (使用 play 命令, 不重发 URL)."""
        if self._state not in (PlayState.PLAYING, PlayState.PAUSED) or not self._songs:
            return False

        self._stop_auto_next()

        ok = await self._miot.player_play(self._device_id)
        if not ok:
            logger.warning("PlaylistManager: resume play failed")
            return False

        self._state = PlayState.PLAYING

        # 重置切歌定时器
        song = self._current_song()
        if song and song.get("duration", 0) > 0 and self._play_start_time > 0:
            elapsed = time.monotonic() - self._play_start_time
            remaining = song["duration"] - elapsed
            if remaining > 0:
                self._start_auto_next(remaining)
                logger.info(f"PlaylistManager: Timer reset after resume: remaining={remaining:.1f}s")

        return True

    async def next_track(self) -> bool:
        """下一首."""
        self._stop_auto_next()
        if not self._songs:
            logger.warning("PlaylistManager: No playlist loaded for next")
            return False

        next_idx = self._get_next_index()
        if next_idx < 0:
            logger.info("PlaylistManager: No next song, stopping")
            await self.stop()
            return False

        self._current_index = next_idx
        return await self._play_current()

    async def prev_track(self) -> bool:
        """上一首."""
        self._stop_auto_next()
        if not self._songs:
            logger.warning("PlaylistManager: No playlist loaded for prev")
            return False

        prev_idx = self._get_prev_index()
        if prev_idx < 0:
            logger.info("PlaylistManager: No previous song")
            return False

        self._current_index = prev_idx
        return await self._play_current()

    async def set_volume(self, volume: int) -> bool:
        """设置音量.

        Args:
            volume: 0-100.

        Returns:
            是否成功.
        """
        if not self._device_id:
            return False
        return await self._miot.set_volume(self._device_id, volume)

    async def set_play_mode(self, mode: PlayMode) -> None:
        """设置播放模式."""
        self._mode = mode
        if mode == PlayMode.RANDOM:
            self._random_played = set()
        logger.info(f"PlaylistManager: Play mode set to {mode.value}")

    # ===== 语音交互挂起与智能恢复 =====

    def suspend_for_voice_interaction(self) -> None:
        """挂起播放：停止切歌定时器但保持 playing/paused 状态.

        用于语音交互打断时，防止定时器在 AI 响应期间触发切歌.
        同时保持状态以便后续 resume() 恢复.
        """
        self._stop_auto_next()
        if self._voice_suspended_at == 0.0:
            self._voice_suspended_at = time.monotonic()

    async def smart_resume(self, miot_client: "MinaHTTPClient", device_id: str,
                           timeout: int = 30) -> None:
        """智能恢复播放.

        等待 TTS 播报结束 → 重新推送当前歌曲 URL.

        逻辑:
        1. 等待 3 秒让 TTS 开始
        2. 轮询设备状态每 2 秒
        3. 检测 status != 1 (playing) → 设备空闲 → replayCurrent()
        4. 超时但设备仍在播放 → 仅重置切歌定时器，不重发URL

        Args:
            miot_client: MinaHTTPClient 实例.
            device_id: 设备ID.
            timeout: 超时秒数 (默认 30).
        """
        if self._state != PlayState.PLAYING:
            return

        # 等待 3 秒让 TTS 开始播报
        await asyncio.sleep(3)

        if self._state != PlayState.PLAYING:
            return

        timeout_sec = max(5, min(120, timeout))
        max_wait = timeout_sec
        poll_interval = 2.0
        start_time = time.monotonic()
        device_became_idle = False
        last_device_position: float = 0.0

        while time.monotonic() - start_time < max_wait:
            if self._state != PlayState.PLAYING:
                return

            raw = await miot_client.get_player_status(device_id)
            device_status = self._parse_device_status(raw)

            if device_status["status"] != 1:  # not playing
                device_became_idle = True
                break

            last_device_position = device_status["position"]
            await asyncio.sleep(poll_interval)

        if self._state != PlayState.PLAYING:
            return

        if not device_became_idle:
            # 超时退出：设备一直在播放，说明已自动恢复
            # 仅重置切歌定时器，不重发 URL（避免从头播放）
            logger.info("PlaylistManager: Device auto-resumed, resetting timer only")
            self._reset_auto_next_timer(last_device_position)
            return

        # 设备空闲（TTS 结束），重新推送当前歌曲 URL
        ok = await self._play_current()
        if ok:
            logger.info("PlaylistManager: Playback restored via replay after voice interaction")
        else:
            logger.warning("PlaylistManager: Failed to restore playback after voice interaction")

    # ===== 播放队列管理 =====

    def set_queue(self, songs: list[dict]) -> None:
        """设置播放队列.

        Args:
            songs: [{title, url, duration}] 格式的歌曲列表.
        """
        self._songs = list(songs)
        self._current_index = 0
        self._random_played = set()

    def current_song(self) -> Optional[dict]:
        """获取当前歌曲信息."""
        return self._current_song()

    def get_status(self) -> dict:
        """获取播放状态摘要."""
        song = self._current_song()
        duration = song.get("duration", 0) if song else 0
        return {
            "state": self._state.value,
            "play_mode": self._mode.value,
            "playlist_id": self._playlist_id,
            "current_index": self._current_index,
            "current_song": {
                "title": song.get("title", "") if song else "",
                "url": song.get("url", "") if song else "",
            } if song else None,
            "position": self._get_position(),
            "duration": duration,
            "is_playing": self._state == PlayState.PLAYING,
        }

    def is_playing(self) -> bool:
        """是否正在播放."""
        return self._state == PlayState.PLAYING

    def has_playlist(self) -> bool:
        """是否有播放列表."""
        return len(self._songs) > 0

    def cleanup(self) -> None:
        """清理资源."""
        self._stop_auto_next()

    # ===== 内部方法: 播放控制 =====

    async def _play_current(self) -> bool:
        """播放当前索引的歌曲."""
        if self._current_index < 0 or self._current_index >= len(self._songs):
            logger.error(f"PlaylistManager: Invalid current index: {self._current_index}")
            return False

        self._stop_auto_next()

        song = self._songs[self._current_index]
        song_url = song.get("url", "")

        if not song_url:
            logger.error(f"PlaylistManager: No URL for song: {song.get('title', 'unknown')}")
            return False

        if not self._device_id:
            logger.error("PlaylistManager: No device_id set")
            return False

        logger.info(
            f"PlaylistManager: Playing song index={self._current_index} "
            f"title={song.get('title', '')} duration={song.get('duration', 0)}"
        )

        ok = await self._miot.play_url(self._device_id, song_url)
        if not ok:
            logger.error("PlaylistManager: Failed to play URL on device")
            return False

        self._clear_voice_suspend()
        prev_state = self._state
        self._state = PlayState.PLAYING
        self._play_start_time = time.monotonic()

        logger.debug(
            "PlaylistManager: 状态切换 %s → PLAYING, play_start_time=%.2f",
            prev_state.value, self._play_start_time
        )

        # 注册自动切歌定时器
        duration = song.get("duration", 0)
        if duration > 0:
            self._start_auto_next(duration)
        else:
            logger.warning(f"PlaylistManager: Song duration invalid: {duration}")

        return True

    # ===== 内部方法: 自动切歌 =====

    def _start_auto_next(self, delay_seconds: float) -> None:
        """启动自动切歌定时器."""
        self._stop_auto_next()
        logger.info(f"PlaylistManager: Auto-next timer scheduled in {delay_seconds:.1f}s")
        logger.debug("PlaylistManager: 定时器设置: delay=%.1fs", delay_seconds)
        self._auto_next_task = asyncio.create_task(self._auto_next_after(delay_seconds))

    def _stop_auto_next(self) -> None:
        """停止自动切歌定时器."""
        if self._auto_next_task is not None and not self._auto_next_task.done():
            logger.debug("PlaylistManager: 定时器取消")
            self._auto_next_task.cancel()
            self._auto_next_task = None

    async def _auto_next_after(self, delay_seconds: float) -> None:
        """定时触发切歌."""
        try:
            await asyncio.sleep(delay_seconds)
            await self._on_song_finished()
        except asyncio.CancelledError:
            pass  # 正常取消
        except Exception as e:
            logger.error(f"PlaylistManager: _auto_next_after error: {e}")

    async def _on_song_finished(self) -> None:
        """歌曲播放结束回调."""
        if self._state != PlayState.PLAYING:
            logger.info("PlaylistManager: Not playing, skip auto-next")
            return

        next_idx = self._get_next_index()
        if next_idx < 0:
            logger.info("PlaylistManager: No next song, playback complete")
            self._state = PlayState.STOPPED
            self._play_start_time = 0.0
            return

        self._current_index = next_idx
        ok = await self._play_current()
        if not ok:
            logger.error("PlaylistManager: Auto-next failed, stopping")
            self._state = PlayState.STOPPED
            self._play_start_time = 0.0

    # ===== 内部方法: 索引计算 =====

    def _get_next_index(self) -> int:
        """获取下一首索引（根据播放模式）.

        Returns:
            下一首索引，-1 表示没有下一首.
        """
        length = len(self._songs)
        if length == 0:
            return -1

        if self._mode == PlayMode.ORDER:
            # 顺序播放：到末尾停止
            if self._current_index < length - 1:
                return self._current_index + 1
            return -1

        elif self._mode == PlayMode.LOOP:
            # 列表循环
            return (self._current_index + 1) % length

        elif self._mode == PlayMode.SINGLE:
            # 单曲循环
            return self._current_index

        elif self._mode == PlayMode.RANDOM:
            # 随机不重复
            self._random_played.add(self._current_index)

            if len(self._random_played) >= length:
                self._random_played = set()

            import random
            unplayed = [i for i in range(length) if i not in self._random_played]
            if not unplayed:
                return random.randint(0, length - 1)
            return random.choice(unplayed)

        return -1

    def _get_prev_index(self) -> int:
        """获取上一首索引.

        Returns:
            上一首索引，-1 表示没有上一首.
        """
        length = len(self._songs)
        if length == 0:
            return -1

        if self._mode == PlayMode.ORDER:
            # 顺序播放：到第一首停止
            if self._current_index > 0:
                return self._current_index - 1
            return -1

        elif self._mode == PlayMode.LOOP:
            # 列表循环：第一首回到最后一首
            if self._current_index > 0:
                return self._current_index - 1
            return length - 1

        elif self._mode == PlayMode.SINGLE:
            # 单曲循环：重复当前
            return self._current_index

        elif self._mode == PlayMode.RANDOM:
            # 随机：简单返回前一索引
            if self._current_index > 0:
                return self._current_index - 1
            return length - 1

        # 默认
        if self._current_index > 0:
            return self._current_index - 1
        return -1

    # ===== 内部方法: 辅助 =====

    def _current_song(self) -> Optional[dict]:
        """获取当前歌曲."""
        if 0 <= self._current_index < len(self._songs):
            return self._songs[self._current_index]
        return None

    def _get_position(self) -> float:
        """获取当前播放位置（秒）."""
        if self._state != PlayState.PLAYING or self._play_start_time == 0.0:
            return 0.0
        elapsed = time.monotonic() - self._play_start_time
        song = self._current_song()
        if song and song.get("duration", 0) > 0 and elapsed > song["duration"]:
            return float(song["duration"])
        return elapsed

    def _reset_auto_next_timer(self, device_position_sec: float = -1.0) -> None:
        """仅重置切歌定时器（不发送任何设备命令）.

        用于设备已自动恢复播放的场景，避免多余的 play 命令导致歌曲从头播放.

        Args:
            device_position_sec: 设备实际播放位置（秒），优先使用;
                                 未提供时回退到挂钟时间.
        """
        self._stop_auto_next()
        self._clear_voice_suspend()

        song = self._current_song()
        if not song or song.get("duration", 0) <= 0:
            return

        remaining: float
        if device_position_sec >= 0:
            remaining = song["duration"] - device_position_sec
            self._play_start_time = time.monotonic() - device_position_sec
        elif self._play_start_time > 0:
            elapsed = time.monotonic() - self._play_start_time
            remaining = song["duration"] - elapsed
        else:
            return

        if remaining > 0:
            self._start_auto_next(remaining)
            logger.info(f"PlaylistManager: Timer reset: remaining={remaining:.1f}s")

    def _clear_voice_suspend(self) -> None:
        """清除语音挂起标记."""
        self._voice_suspended_at = 0.0

    @staticmethod
    def _parse_device_status(raw: Any) -> dict:
        """从 UBus player_get_play_status 响应解析设备状态.

        响应格式: { data: { info: '{"status":1,"play_song_detail":{"position":12000,...}}' } }

        Returns:
            {"status": int, "position": float}  status=-1 表示解析失败.
        """
        status = -1
        position = 0.0

        logger.debug(
            "PlaylistManager: _parse_device_status raw=%s",
            str(raw)[:300] if raw else "None"
        )

        info = None
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, dict):
                info = data.get("info")
            # 也兼容 info 直接在顶层 (部分响应格式)
            if info is None:
                info = raw.get("info")

        if isinstance(info, str):
            try:
                parsed = json.loads(info)
                if isinstance(parsed, dict):
                    if "status" in parsed and isinstance(parsed["status"], (int, float)):
                        status = int(parsed["status"])
                    detail = parsed.get("play_song_detail")
                    if isinstance(detail, dict) and "position" in detail:
                        position = float(detail["position"]) / 1000.0  # ms → s
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        logger.debug(
            "PlaylistManager: _parse_device_status result status=%d position=%.1f",
            status, position
        )
        return {"status": status, "position": position}
