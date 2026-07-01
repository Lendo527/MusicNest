"""musicnest - FastAPI 应用入口"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import jinja2
from jinja2 import Environment, FileSystemLoader

templates_dir = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)


def _render_template(name: str, request: Request) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    html = template.render({"request": request})
    return HTMLResponse(html)


from app.config import config
from app.version import __version__
from app.miot.auth import MiAuth, _generate_device_id
from app.miot.client import MinaHTTPClient
from app.miot.hardware import needs_music_api, needs_mp3
from app.engine.monitor import ConversationMonitor
from app.engine.media_watcher import MediaWatcher
from app.engine.voice import VoiceEngine, VoiceCommand, _default_commands
from app.search.kuwo import search_by_keyword, search as kuwo_search
from app.search.netease import (
    search as netease_search,
    verify_cookie as netease_verify_cookie,
    get_playlist_tracks as netease_get_playlist_tracks,
)
from app.music.scanner import MusicScanner
from app.download.tracker import (
    init_db,
    add_task,
    get_tasks,
    get_task_by_id,
    get_download_stats,
    update_task_status,
    delete_task,
    clear_finished_tasks,
    is_synced,
    get_synced_ids,
    record_sync,
)
from app.download.worker import download_worker, playlist_sync_worker, RUNNING as _DOWNLOAD_WORKER_RUNNING, stop_worker

logging.getLogger().setLevel(logging.INFO)
# 压制 httpx 的 HTTP Request 日志
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("musicnest")

# ===== 调试日志 =====
_DEBUG_LOG_PATH = "/data/debug.log"
_debug_fh: Optional[logging.FileHandler] = None

def _setup_debug_logging():
    """设置调试日志文件，输出所有 DEBUG 级别日志

    幂等：重复调用不会重复添加 handler，避免日志重复输出。
    """
    global _debug_fh
    root = logging.getLogger()

    # 如果已存在 handler，先移除（避免重复添加 + 支持重新配置）
    if _debug_fh is not None:
        try:
            root.removeHandler(_debug_fh)
            _debug_fh.close()
        except Exception:
            pass
        _debug_fh = None

    if not config.get("debug_logging", True):
        logger.info("[DebugLog] 调试日志已关闭")
        return

    try:
        fh = logging.FileHandler(_DEBUG_LOG_PATH, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        # 给 root logger 添加文件 handler（捕获所有模块的日志）
        root.addHandler(fh)
        _debug_fh = fh
        # 设置所有 musicnest 相关 logger 到 DEBUG 级别
        for name in ["musicnest", "musicnest.voice", "musicnest.monitor",
                      "musicnest.player", "musicnest.miot", "musicnest.kuwo",
                      "musicnest.kuwo.search", "musicnest.kuwo.format",
                      "musicnest.auth", "musicnest.config", "musicnest.netease",
                      "musicnest.token_refresh", "musicnest.download"]:
            logging.getLogger(name).setLevel(logging.DEBUG)
        # 确保第三方库的 ERROR 级别日志也能被捕获（web 端报错的关键来源）
        # 设置 propagate=True 使 uvicorn/fastapi 日志传播到 root logger（写入 debug.log）
        for name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            lg.propagate = True
        logger.info("[DebugLog] 调试日志已启用: %s", _DEBUG_LOG_PATH)
    except Exception as e:
        logger.warning("[DebugLog] 调试日志设置失败: %s", e)

_setup_debug_logging()

netease_cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")
if netease_cookie:
    logger.info("[Netease] Cookie 状态: 已配置 (%d 字符)", len(netease_cookie))
else:
    logger.info("[Netease] Cookie 状态: 未配置，下载受限")

# ===== 全局服务实例 =====
miauth = MiAuth()
miot_client: MinaHTTPClient | None = None
monitor: ConversationMonitor | None = None
media_watcher: MediaWatcher | None = None
scanner = MusicScanner(config.get("music_path", "/music"))
voice_engine = VoiceEngine()

app = FastAPI(title="MusicNest", version=__version__)

# 静态文件（默认封面等）
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ===== 全局异常处理（确保未捕获异常也输出到 debug.log） =====

@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """捕获所有未处理的异常，记录到 debug.log 并返回 500"""
    logger.error(f"[Unhandled] {request.method} {request.url.path} 异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 1, "msg": f"服务器内部错误: {exc}"},
    )


@app.middleware("http")
async def _log_http_errors(request: Request, call_next):
    """记录所有 5xx 响应到 debug.log，便于排查 web 端报错"""
    try:
        response = await call_next(request)
    except Exception as exc:
        # call_next 抛出的异常（已被 exception_handler 捕获的不会到这里，这里兜底）
        logger.error(f"[HTTP] {request.method} {request.url.path} 中间件异常: {exc}", exc_info=True)
        raise
    if response.status_code >= 500:
        logger.error(f"[HTTP] {request.method} {request.url.path} → {response.status_code}")
    return response


# ===== 播放状态（全局） =====

class PlayMode(str, Enum):
    SINGLE = "single"            # 单曲播放（播完停）
    SINGLE_LOOP = "single_loop"  # 单曲循环
    LIST = "list"                # 列表播放（播完停）
    LIST_LOOP = "list_loop"      # 列表循环
    SHUFFLE = "shuffle"          # 随机播放


# 播放模式图标映射
PLAY_MODE_ICONS = {
    PlayMode.SINGLE: "bi-music-note",
    PlayMode.SINGLE_LOOP: "bi-arrow-repeat",
    PlayMode.LIST: "bi-list",
    PlayMode.LIST_LOOP: "bi-arrow-repeat",
    PlayMode.SHUFFLE: "bi-shuffle",
}

# 播放模式切换顺序
_PLAY_MODE_ORDER = [
    PlayMode.SINGLE,
    PlayMode.SINGLE_LOOP,
    PlayMode.LIST,
    PlayMode.LIST_LOOP,
    PlayMode.SHUFFLE,
]


class PlayState:
    def __init__(self):
        self.current_index: Optional[int] = None   # 当前歌曲索引
        self.playlist: list = []                    # 完整歌单 [{title, artist, filepath, ...}]
        self.mode: PlayMode = PlayMode.SINGLE       # 播放模式
        self.is_playing: bool = False               # 是否正在播放
        self.device_id: Optional[str] = None         # 播放设备ID
        self.volume: int = 50                       # 当前音量 0-100
        self.duration: int = 0                      # 当前歌曲时长（秒）
        self._play_start_time: float = 0.0           # 当前曲开始时间戳（monotonic秒），用于本地进度估算
        self._pause_elapsed: float = 0.0             # 暂停时已播放的秒数，恢复时用于回退 _play_start_time

    def current_song(self) -> Optional[dict]:
        if self.current_index is not None and 0 <= self.current_index < len(self.playlist):
            return self.playlist[self.current_index]
        return None

    def _get_next_index(self) -> Optional[int]:
        length = len(self.playlist)
        if length == 0 or self.current_index is None:
            return None
        if self.mode == PlayMode.SINGLE:
            return None  # 单曲播完停
        elif self.mode == PlayMode.SINGLE_LOOP:
            return self.current_index  # 单曲循环
        elif self.mode == PlayMode.LIST:
            nxt = self.current_index + 1
            return nxt if nxt < length else None
        elif self.mode == PlayMode.LIST_LOOP:
            return (self.current_index + 1) % length
        elif self.mode == PlayMode.SHUFFLE:
            if length == 1:
                return 0
            import random as _random
            # 排除当前歌曲，避免"下一首"返回正在播放的曲
            candidates = [i for i in range(length) if i != self.current_index]
            return _random.choice(candidates)
        return None

    def _get_prev_index(self) -> Optional[int]:
        length = len(self.playlist)
        if length == 0 or self.current_index is None:
            return None
        prev = self.current_index - 1
        if prev < 0:
            # 根据模式决定是否回绕
            if self.mode in (PlayMode.LIST_LOOP, PlayMode.SHUFFLE):
                return length - 1
            return None
        return prev

    def get_state_dict(self) -> dict:
        song = self.current_song()
        return {
            "current_index": self.current_index,
            "song": song,
            "playlist_length": len(self.playlist),
            "mode": self.mode.value,
            "mode_icon": PLAY_MODE_ICONS.get(self.mode, "bi-music-note"),
            "is_playing": self.is_playing,
            "volume": self.volume,
            "duration": self.duration,
        }

    def stop_playing(self) -> None:
        """停止播放，重置所有运行时状态"""
        self.is_playing = False
        self._play_start_time = 0.0
        self._pause_elapsed = 0.0


play_state = PlayState()


from collections import OrderedDict

# ===== 在线音频代理 =====
_online_urls: OrderedDict[str, str] = OrderedDict()  # hash -> kuwo_url（LRU，上限1000）

KUWO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Referer": "http://www.kuwo.cn/",
}


# ===== 睡眠定时 =====

_sleep_timer_task: Optional[asyncio.Task] = None
_sleep_timer_remaining: int = 0  # 剩余秒数，用于前端轮询


async def _sleep_timer(minutes: int):
    """倒计时后停止播放"""
    global _sleep_timer_remaining
    total_seconds = minutes * 60
    for remaining in range(total_seconds, 0, -1):
        _sleep_timer_remaining = remaining
        await asyncio.sleep(1)
    _sleep_timer_remaining = 0
    try:
        client = await _check_miot()
        if client and play_state.device_id:
            await client.stop_all_media(play_state.device_id)
            play_state.stop_playing()
            logger.info(f"[Timer] 睡眠定时结束，已停止所有媒体播放")
    except Exception as e:
        logger.error(f"[Timer] 停止播放失败: {e}")


# ===== 闹钟 =====

_alarm_tasks: dict[str, asyncio.Task] = {}


async def _alarm_loop(alarm_id: str, hour: int, minute: int, days: list[int], song_index: Optional[int] = None):
    """闹钟循环，每天指定时间触发"""
    logger.info(f"[Alarm] 闹钟已启动: id={alarm_id} time={hour:02d}:{minute:02d} days={days} song_index={song_index}")
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        if wait_seconds < 1:
            wait_seconds = 60  # 避免瞬时重复触发
        await asyncio.sleep(wait_seconds)

        # 检查星期匹配（0=Mon ... 6=Sun）
        if days and target.weekday() not in days:
            continue

        try:
            client = await _check_miot()
            if not client or not play_state.device_id:
                # 尝试获取设备
                play_state.device_id = await _get_target_device()
                if not play_state.device_id:
                    logger.warning(f"[Alarm] 闹钟 id={alarm_id} 无可用播放设备，跳过")
                    continue

            # 先停掉音箱所有媒体通道，确保闹钟能完全接管播放
            await client.stop_all_media(play_state.device_id)

            if song_index is not None:
                await _play_on_device(play_state.device_id, song_index)
                play_state.is_playing = True
                logger.info(f"[Alarm] 闹钟触发: id={alarm_id} 播放歌曲 index={song_index}")
            else:
                # 播放第一首可用歌曲
                songs = scanner.get_songs(limit=1)
                if songs:
                    play_state.playlist = scanner.get_songs(limit=500)
                    play_state.current_index = 0
                    await _play_on_device(play_state.device_id, 0)
                    play_state.is_playing = True
                    logger.info(f"[Alarm] 闹钟触发: id={alarm_id} 播放默认歌单")
                else:
                    logger.warning(f"[Alarm] 闹钟 id={alarm_id} 曲库为空，无法播放")
        except Exception as e:
            logger.error(f"[Alarm] 闹钟触发失败: {e}")


def _restore_alarms():
    """从配置恢复闹钟任务"""
    alarms = config.get("alarms", [])
    for alarm in alarms:
        if not alarm.get("enabled", True):
            continue
        aid = alarm.get("id")
        if not aid:
            logger.warning(f"[Alarm] 跳过无 id 的闹钟配置: {alarm}")
            continue
        hour = alarm.get("hour", 8)
        minute = alarm.get("minute", 0)
        days = alarm.get("days", [])
        song_index = alarm.get("song_index")
        if aid in _alarm_tasks:
            _alarm_tasks[aid].cancel()
        task = asyncio.create_task(_alarm_loop(aid, hour, minute, days, song_index))
        _alarm_tasks[aid] = task
        logger.info(f"[Alarm] 恢复闹钟: id={aid} {hour:02d}:{minute:02d}")


# ===== 语音指令管理 =====

def _init_voice_engine() -> None:
    """从配置恢复语音指令"""
    saved = config.get("voice_commands", [])
    if saved and isinstance(saved, list):
        commands = []
        for item in saved:
            try:
                cmd_type = item.get("type", "play_song")
                cmd = VoiceCommand(
                    type=cmd_type,
                    keywords=item.get("keywords", []),
                    param=item.get("param"),
                    enabled=item.get("enabled", True),
                )
                # 兼容旧配置：set_play_mode/set_volume 缺 param 时补默认值
                if not cmd.param:
                    _default_params = {
                        "set_play_mode": "random",
                        "set_volume": "absolute",
                    }
                    if cmd_type in _default_params:
                        cmd.param = _default_params[cmd_type]
                commands.append(cmd)
            except Exception as e:
                logger.warning(f"[VoiceCmd] 跳过格式错误的指令配置: {item}, err={e}")
                continue
        if commands:
            # 确保 play_song 包含 "播放" 关键词
            for cmd in commands:
                if cmd.type == "play_song" and "播放" not in cmd.keywords:
                    cmd.keywords.append("播放")
                    logger.info("[VoiceCmd] 已补全 play_song 关键词: 添加\"播放\"")
            voice_engine.set_commands(commands)
        else:
            voice_engine.set_commands(_default_commands())
    else:
        voice_engine.set_commands(_default_commands())

    voice_engine.enabled = config.get("voice_engine_enabled", True)
    logger.info(f"[VoiceEngine] 初始化完成: {len(voice_engine.commands)} 条指令, enabled={voice_engine.enabled}")


def _serialize_commands() -> list[dict]:
    """把当前指令序列化到配置"""
    return [
        {
            "type": cmd.type,
            "keywords": cmd.keywords,
            "param": cmd.param,
            "enabled": cmd.enabled,
        }
        for cmd in voice_engine.commands
    ]


async def _on_voice_message(device_id: str, msg: dict) -> None:
    """语音消息回调：VoiceEngine 匹配 → 执行播放控制 + 睡眠定时/闹钟"""
    # 检查设备是否已勾选
    selections: dict = config.get("device_selections", {})
    if not selections.get(device_id, False):
        logger.info(f"[VoiceCmd] 设备未勾选，跳过: device={device_id[:12]}...")
        return
    query = msg.get("query", "")
    if not query:
        return

    # 去重：如果该 query 已被处理过（轨道2 先拦截 → 轨道1 后到，或反之），跳过
    # 时间窗口 5 秒，避免用户连续说两次相同指令被漏处理
    if monitor and monitor.is_query_handled(device_id, query, within_sec=5.0):
        logger.debug(f"[VoiceCmd] query 已被处理，跳过: device={device_id[:12]}... query={query[:40]!r}")
        return

    # === 睡眠定时 / 闹钟 指令（正则匹配，优先级高于通用语音引擎） ===
    timer_minutes = _parse_timer_minutes(query)
    if timer_minutes is not None:
        # "X分钟后停止播放" / "X分钟停止" / "定时X分钟"
        global _sleep_timer_task
        if _sleep_timer_task:
            _sleep_timer_task.cancel()
        _sleep_timer_task = asyncio.create_task(_sleep_timer(timer_minutes))
        logger.info(f"[VoiceCmd] 睡眠定时: {timer_minutes} 分钟")
        return

    if re.search(r'取消(?:睡眠)?定时|关掉定时|停掉定时', query):
        if _sleep_timer_task:
            _sleep_timer_task.cancel()
            _sleep_timer_task = None
        global _sleep_timer_remaining
        _sleep_timer_remaining = 0
        logger.info(f"[VoiceCmd] 取消睡眠定时")
        return

    # 闹钟: "每天早上X点播放" / "每天早上X点X分播放" / "X点播放XXX"
    alarm_match = _parse_alarm_from_query(query)
    if alarm_match:
        hour_val, minute_val, song_hint = alarm_match
        alarm_id = str(uuid.uuid4())[:8]
        days = []  # 默认每天
        # 尝试匹配歌曲
        song_index = None
        if song_hint:
            results = scanner.search(song_hint)
            if results:
                # 在完整歌单中查找匹配的索引
                all_songs = scanner.get_songs(limit=500)
                for i, s in enumerate(all_songs):
                    if s.get("filepath") == results[0].get("filepath"):
                        song_index = i
                        break

        task = asyncio.create_task(_alarm_loop(alarm_id, hour_val, minute_val, days, song_index))
        _alarm_tasks[alarm_id] = task

        alarms = config.get("alarms", [])
        alarms.append({
            "id": alarm_id, "hour": hour_val, "minute": minute_val,
            "days": days, "song_index": song_index, "enabled": True,
            "song_hint": song_hint or "",
        })
        config.set("alarms", alarms)
        logger.info(f"[VoiceCmd] 创建闹钟: {hour_val:02d}:{minute_val:02d}{' 歌曲:' + song_hint if song_hint else ''}")
        return

    # === 通用语音引擎 ===
    result = voice_engine.handle_message(query)
    if not result:
        # 未匹配任何指令：用户与小爱聊天（如问天气）打断了音乐
        # 触发 smart_resume：等待 TTS 结束后自动恢复播放
        if play_state.is_playing and play_state.current_song() and miot_client:
            asyncio.create_task(_smart_resume_playback(device_id))
            logger.info(f"[SmartResume] 检测到播放被打断，启动智能恢复任务: query={query[:40]!r}")
        return
    logger.info(f"[VoiceCmd] 命中: type={result.command.type} keyword={result.keyword} arg={result.argument}")

    result_text = ""

    try:
        if result.command.type == "play_song":
            # 立即 stop_all_media（fire-and-forget，不等返回）
            # 这是"抢先劫持"的核心：尽快打断小爱原生播放
            if miot_client and device_id:
                asyncio.create_task(miot_client.stop_all_media(device_id))

            song_name = result.argument or query
            local_results = scanner.search(song_name)

            if local_results:
                # 本地命中：play_url + TTS
                all_songs = scanner.get_songs(limit=5000)
                target = local_results[0]
                target_path = target.get("filepath", "")
                real_index = next(
                    (i for i, s in enumerate(all_songs) if s.get("filepath") == target_path), None
                )
                if real_index is None:
                    real_index = 0
                    play_state.playlist = list(local_results)
                else:
                    play_state.playlist = list(all_songs)
                play_state.current_index = real_index
                play_state.device_id = device_id

                # 元数据查询改 fire-and-forget（不阻塞播放）
                asyncio.create_task(_enrich_playlist_metadata(song_name, real_index, all_songs))

                ok = await _play_on_device(device_id, real_index)
                song_title = target.get("title", song_name)
                song_artist = target.get("artist", "") or target.get("display_artist", "")
                if ok:
                    play_state.is_playing = True
                    tts_text = f"为您播放 {song_artist}唱的 {song_title}" if song_artist else f"为您播放 {song_title}"
                else:
                    tts_text = "播放失败"
                if config.get("tts_enabled", True) and miot_client:
                    try:
                        await miot_client.text_to_speech(device_id, tts_text)
                        logger.info(f"[VoiceTTS] 播报: {tts_text}")
                    except Exception as e:
                        logger.warning(f"[VoiceTTS] TTS 播报失败: {e}")
                # play_song 分支自处理 TTS，跳过末尾统一 TTS
                if monitor:
                    monitor.mark_query_handled(device_id, query)
                return
            else:
                # 本地未命中：TTS "正在搜索" + 在线搜索
                if config.get("tts_enabled", True) and miot_client:
                    try:
                        await miot_client.text_to_speech(device_id, f"正在联网搜索 {song_name}")
                    except Exception:
                        pass

                kw_result = await search_by_keyword(song_name)
                if kw_result.get("code") == 0 and kw_result.get("data"):
                    song_data = kw_result["data"]
                    song = {
                        "title": song_data.get("title", song_name),
                        "artist": song_data.get("artist", ""),
                        "filepath": song_data.get("url", ""),
                        "album": song_data.get("album", ""),
                        "cover_url": song_data.get("cover_url", ""),
                        "duration": song_data.get("duration", 0),
                    }
                    play_state.playlist = [song]
                    play_state.current_index = 0
                    play_state.device_id = device_id
                    play_state.duration = int(song_data.get("duration", 0))
                    if miot_client:
                        play_url_raw = song["filepath"]
                        if not play_url_raw or not play_url_raw.startswith("http"):
                            if config.get("tts_enabled", True):
                                try:
                                    await miot_client.text_to_speech(device_id, f"没有找到歌曲 {song_name}")
                                except Exception:
                                    pass
                            if monitor:
                                monitor.mark_query_handled(device_id, query)
                            return

                        server_host = config.get("server_host", "http://localhost:58092")
                        url_hash = hashlib.md5(play_url_raw.encode()).hexdigest()[:16]
                        _online_urls[url_hash] = play_url_raw
                        _online_urls.move_to_end(url_hash)
                        if len(_online_urls) > 1000:
                            _online_urls.popitem(last=False)
                        proxied_url = f"{server_host}/api/music/proxy/{url_hash}"
                        logger.info(f"[VoiceCmd] 在线歌曲代理: {play_url_raw[:60]}... -> /api/music/proxy/{url_hash}")

                        hardware = await _get_device_hardware(device_id)
                        if needs_music_api(hardware):
                            ok = await miot_client.play_music_url(device_id, proxied_url)
                        else:
                            ok = await miot_client.play_url(device_id, proxied_url)
                        if ok:
                            play_state.is_playing = True
                            play_state._play_start_time = time.monotonic()
                            tts_text = f"找到 {song['artist']}唱的 {song['title']}，开始播放"
                        else:
                            tts_text = "播放失败"
                        if config.get("tts_enabled", True):
                            try:
                                await miot_client.text_to_speech(device_id, tts_text)
                                logger.info(f"[VoiceTTS] 播报: {tts_text}")
                            except Exception as e:
                                logger.warning(f"[VoiceTTS] TTS 播报失败: {e}")
                    else:
                        if config.get("tts_enabled", True) and miot_client:
                            try:
                                await miot_client.text_to_speech(device_id, "未登录小米账号")
                            except Exception:
                                pass
                else:
                    if config.get("tts_enabled", True) and miot_client:
                        try:
                            await miot_client.text_to_speech(device_id, f"没有找到歌曲 {song_name}")
                        except Exception:
                            pass

                # play_song 分支自处理 TTS，跳过末尾统一 TTS
                if monitor:
                    monitor.mark_query_handled(device_id, query)
                return

        elif result.command.type == "next":
            if miot_client and device_id:
                await miot_client.stop_all_media(device_id)
            next_idx = play_state._get_next_index()
            if next_idx is not None and play_state.device_id:
                play_state.current_index = next_idx
                ok = await _play_on_device(play_state.device_id, next_idx)
                if ok:
                    play_state.is_playing = True
                    song = play_state.current_song()
                    song_title = song.get("title", "") if song else ""
                    result_text = f"已切换到《{song_title}》"
                else:
                    result_text = "切换失败"
            elif play_state.playlist:
                result_text = "没有下一首了"
            else:
                result_text = "播放列表为空"

        elif result.command.type == "previous":
            if miot_client and device_id:
                await miot_client.stop_all_media(device_id)
            prev_idx = play_state._get_prev_index()
            if prev_idx is not None and play_state.device_id:
                play_state.current_index = prev_idx
                ok = await _play_on_device(play_state.device_id, prev_idx)
                if ok:
                    play_state.is_playing = True
                    song = play_state.current_song()
                    song_title = song.get("title", "") if song else ""
                    result_text = f"已切换到《{song_title}》"
                else:
                    result_text = "切换失败"
            elif play_state.current_index is not None and play_state.device_id:
                # 已是第一首，重播当前
                ok = await _play_on_device(play_state.device_id, play_state.current_index)
                if ok:
                    play_state.is_playing = True
                    result_text = "已是第一首，重新播放"
                else:
                    result_text = "重播失败"
            else:
                result_text = "没有正在播放的歌曲"

        elif result.command.type == "stop":
            if miot_client:
                await miot_client.stop_all_media(device_id)
            play_state.stop_playing()
            result_text = "已停止播放"

        elif result.command.type == "play_playlist":
            if miot_client and device_id:
                await miot_client.stop_all_media(device_id)
            songs = scanner.get_songs(limit=500)
            if songs:
                play_state.playlist = songs
                play_state.current_index = 0
                play_state.device_id = device_id
                ok = await _play_on_device(device_id, 0)
                if ok:
                    play_state.is_playing = True
                    result_text = "正在播放歌单"
                else:
                    result_text = "播放失败"
            else:
                result_text = "歌单为空，请先扫描音乐库"

        elif result.command.type == "set_play_mode":
            param = result.command.param
            mode_map = {
                "random": PlayMode.SHUFFLE,
                "single": PlayMode.SINGLE_LOOP,
                "loop": PlayMode.LIST_LOOP,
                "order": PlayMode.LIST,
            }
            mode_names = {"random": "随机", "single": "单曲循环", "loop": "列表循环", "order": "顺序"}
            if param in mode_map:
                play_state.mode = mode_map[param]
                logger.info(f"[VoiceCmd] 播放模式切换为: {play_state.mode.value}")
                result_text = f"已切换到{mode_names.get(param, param)}模式"
            else:
                result_text = "模式切换失败"

        elif result.command.type == "set_volume":
            param = result.command.param
            arg = result.argument
            if param == "absolute":
                nums = re.findall(r"\d+", arg)
                vol = int(nums[0]) if nums else play_state.volume
                vol = max(0, min(100, vol))
            elif param == "up":
                vol = min(100, play_state.volume + 10)
            elif param == "down":
                vol = max(0, play_state.volume - 10)
            else:
                vol = play_state.volume

            if miot_client:
                await miot_client.set_volume(device_id, vol)
            play_state.volume = vol
            logger.info(f"[VoiceCmd] 音量设置为: {vol}")
            result_text = f"音量已调到{vol}"

        elif result.command.type == "create_alarm":
            alarm_text = result.argument
            alarm_match = _parse_alarm_from_query(alarm_text) if alarm_text else None
            if alarm_match:
                hour_val, minute_val, song_hint = alarm_match
                alarm_id = str(uuid.uuid4())[:8]
                days = []
                song_index = None
                if song_hint:
                    results = scanner.search(song_hint)
                    if results:
                        all_songs = scanner.get_songs(limit=500)
                        for i, s in enumerate(all_songs):
                            if s.get("filepath") == results[0].get("filepath"):
                                song_index = i
                                break
                task = asyncio.create_task(_alarm_loop(alarm_id, hour_val, minute_val, days, song_index))
                _alarm_tasks[alarm_id] = task
                alarms_cfg = config.get("alarms", [])
                alarms_cfg.append({
                    "id": alarm_id, "hour": hour_val, "minute": minute_val,
                    "days": days, "song_index": song_index, "enabled": True,
                    "song_hint": song_hint or "",
                })
                config.set("alarms", alarms_cfg)
                log_msg = f"创建闹钟: 每天 {hour_val:02d}:{minute_val:02d}"
                if song_hint:
                    log_msg += f" 歌曲: {song_hint}"
                logger.info(f"[VoiceCmd] {log_msg}")
                song_info = f" 播放《{song_hint}》" if song_hint else ""
                result_text = f"已创建闹钟，每天 {hour_val:02d}:{minute_val:02d}{song_info}"
            else:
                result_text = "没听清闹钟时间，试试说「设置闹钟 每天早上8点播放」"

    except Exception as e:
        logger.error(f"[VoiceCmd] 指令执行异常: {e}", exc_info=True)
        if not result_text:
            result_text = "指令执行失败"

    # TTS 语音反馈（异步，不阻塞主流程）
    if result_text and config.get("tts_enabled", True) and miot_client:
        try:
            await miot_client.text_to_speech(device_id, result_text)
            logger.info(f"[VoiceTTS] 播报: {result_text}")
        except Exception as e:
            logger.warning(f"[VoiceTTS] TTS 播报失败: {e}")


async def _enrich_playlist_metadata(song_name: str, real_index: int, all_songs: list) -> None:
    """后台补全播放列表中的在线元数据（fire-and-forget，不阻塞播放）

    从酷我/网易云获取封面、歌手、标题等显示字段，写入 play_state.playlist[real_index]。
    失败静默，不影响播放。
    """
    try:
        kw_search = await search_by_keyword(song_name)
        if kw_search.get("code") == 0 and kw_search.get("data"):
            online = kw_search["data"]
            if (real_index is not None
                and real_index < len(all_songs)
                and real_index < len(play_state.playlist)):
                play_state.playlist[real_index] = dict(all_songs[real_index])
                enriched = play_state.playlist[real_index]
                if online.get("cover_url"):
                    enriched["cover_url"] = online["cover_url"]
                if online.get("artist"):
                    enriched["display_artist"] = online["artist"]
                if online.get("title"):
                    enriched["display_title"] = online["title"]
                logger.info(
                    f"[VoiceCmd] 在线元数据已补充: title={online.get('title')} artist={online.get('artist')}"
                )
    except Exception as e:
        logger.debug(f"[VoiceCmd] 在线元数据查询失败: {e}")


async def _on_native_playback_intercept(device_id: str, query: Optional[str]) -> None:
    """轨道2（media_watcher）检测到原生播放时的拦截回调

    Args:
        device_id: 设备 ID
        query: 反查到的最近对话 query（可能为 None）
    """
    if not query:
        return

    # 复用 _on_voice_message 的逻辑
    logger.info(f"[MediaWatcher] 触发拦截: device={device_id[:12]}... query={query!r}")
    await _on_voice_message(device_id, {"query": query, "answer": ""})


async def _smart_resume_playback(device_id: str, timeout: int = 30) -> None:
    """语音打断后智能恢复播放

    场景：用户问"今天天气"等非音乐问题，小爱 TTS 回答打断了音乐。
    逻辑（参考 songloft-plugin-miot 的 smart_resume）:
    1. 等待 3 秒让 TTS 开始
    2. 轮询设备状态每 2 秒
    3. 检测 status != 1（设备空闲）→ 重新推送当前歌曲 URL
    4. 超时但设备仍在播放 → 说明已自动恢复，不重发 URL
    """
    if not play_state.is_playing or not play_state.current_song():
        return
    client = await _check_miot()
    if not client:
        return

    # 等待 3 秒让 TTS 开始播报
    await asyncio.sleep(3)
    if not play_state.is_playing:
        return

    poll_interval = 2.0
    start_time = time.monotonic()
    device_became_idle = False

    while time.monotonic() - start_time < timeout:
        if not play_state.is_playing:
            return
        try:
            raw = await client.get_player_status(device_id)
            info = _parse_player_info(raw)
        except Exception as e:
            logger.debug(f"[SmartResume] 状态轮询失败: {e}")
            await asyncio.sleep(poll_interval)
            continue
        # status != 1 表示设备空闲（TTS 结束）
        if info["status"] != 1:
            device_became_idle = True
            break
        await asyncio.sleep(poll_interval)

    if not play_state.is_playing:
        return

    if not device_became_idle:
        # 超时：设备一直在播放，说明已自动恢复，无需重发
        logger.info("[SmartResume] 设备仍在播放，跳过恢复")
        return

    # 设备空闲（TTS 结束），重新推送当前歌曲 URL
    if play_state.current_index is None:
        return
    song = play_state.current_song()
    if not song:
        return
    logger.info(f"[SmartResume] 检测到设备空闲，恢复播放: {song.get('title', '')}")
    ok = await _play_on_device(device_id, play_state.current_index)
    if ok:
        play_state._play_start_time = time.monotonic()
        play_state.is_playing = True
        logger.info("[SmartResume] 播放已恢复")


def _parse_timer_minutes(query: str) -> Optional[int]:
    """从语音文本中解析睡眠定时分钟数，如 '30分钟后停止播放' -> 30"""
    m = re.search(r'(\d+)\s*分钟?后?(?:停止|关闭|停|关)', query)
    if m:
        return int(m.group(1))
    m = re.search(r'定时\s*(\d+)\s*分钟?', query)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*分钟?后?自动停止', query)
    if m:
        return int(m.group(1))
    return None


def _parse_alarm_from_query(query: str) -> Optional[tuple[int, int, Optional[str]]]:
    """从语音文本中解析闹钟时间，如 '每天早上8点播放' -> (8, 0, None)

    支持 12 小时制：下午/晚上 X 点 → hour + 12（13~23 点）。
    中午 X 点视为 12 点（中午12点 = 12:00，中午1点 = 13:00）。
    """
    # 检测时段修饰词
    period_match = re.search(r'(早上|上午|中午|下午|晚上|傍晚|清晨)', query)
    period = period_match.group(1) if period_match else ""

    # 模式: 每天早上X点Y分播放 或 X点Y分播放XXX
    m = re.search(r'每[天日](?:早上|上午|中午|下午|晚上|傍晚|清晨)?\s*(\d{1,2})\s*点\s*(?:\s*(\d{1,2})\s*分)?\s*(?:播放|放)(?:歌曲?\s*)?(.+)?', query)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        song = m.group(3).strip() if m.group(3) else None
        # 时段转换
        if period in ("下午", "晚上", "傍晚") and 1 <= hour <= 11:
            hour += 12
        elif period == "中午" and hour == 12:
            hour = 12  # 中午12点 = 12:00
        elif period == "中午" and 1 <= hour <= 11:
            hour += 12  # 中午1点 = 13:00
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, song)
    # 简单模式: X点播放
    m = re.search(r'(?:每[天日])?\s*(\d{1,2})\s*点\s*(?:\s*(\d{1,2})\s*分)?\s*播放', query)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        # 时段转换
        if period in ("下午", "晚上", "傍晚") and 1 <= hour <= 11:
            hour += 12
        elif period == "中午" and hour == 12:
            hour = 12
        elif period == "中午" and 1 <= hour <= 11:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, None)
    return None


# ===== 生命周期 =====

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭"""
    logger.info("musicnest 启动中...")

    # uvicorn 启动时会重新配置日志（dictConfig），可能覆盖我们在 _setup_debug_logging
    # 中设置的 propagate。此处重新确保 uvicorn/fastapi logger 传播到 root logger，
    # 使其错误日志也能写入 debug.log 文件。
    if config.get("debug_logging", True) and _debug_fh is not None:
        for name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
            lg = logging.getLogger(name)
            lg.propagate = True
            if not lg.handlers:
                lg.setLevel(logging.INFO)
        logger.debug("[DebugLog] 已重新应用 uvicorn/fastapi 日志 propagate 设置")

    # 初始化语音引擎
    _init_voice_engine()

    # 初始化下载数据库
    try:
        init_db()
        logger.info("下载数据库已初始化")
    except Exception as e:
        logger.error(f"下载数据库初始化失败: {e}")

    # 初始化定时扫描
    auto_scan = config.get("auto_scan_interval", 0)
    if auto_scan > 0:
        scanner.set_auto_scan(auto_scan)
        logger.info(f"定时扫描已启动: 每 {auto_scan} 分钟")

    # 首次启动自动扫描（检测曲库是否为空，避免空库）
    stats = scanner.get_stats()
    if stats["total_songs"] == 0:
        # 先尝试从缓存加载
        if not scanner._load_cache():
            logger.info("曲库为空，后台启动首次扫描...")
            async def _background_scan():
                try:
                    await scanner.scan()
                    s = scanner.get_stats()
                    logger.info(f"首次扫描完成: {s['total_songs']} 首歌曲, {s['total_albums']} 张专辑")
                except Exception as e:
                    logger.warning(f"首次扫描失败: {e}")
            asyncio.create_task(_background_scan())
        else:
            s = scanner.get_stats()
            logger.info(f"从缓存恢复曲库: {s['total_songs']} 首歌曲")

    # 启动下载 Worker
    from app.download.worker import set_scan_callback
    set_scan_callback(scanner.scan_new)
    _download_task = asyncio.create_task(download_worker(poll_interval=5.0))
    logger.info("下载 Worker 已启动")

    # 启动歌单同步定时任务
    sync_interval = config.get("playlist_sync_interval", 1800)
    _sync_task = asyncio.create_task(playlist_sync_worker(sync_interval=sync_interval))
    logger.info(f"歌单同步任务已启动, 间隔={sync_interval}s")

    # 如果已配置小米 token，自动连接
    token = config.miot_token
    user_id = config.get("miot_user_id", "")
    if token and user_id:
        await _init_miot_client(user_id, token)
        logger.info(f"小米客户端已初始化 (userId={user_id})")

        # 自动检测 LAN IP 作为 server_host 默认值
        server_host = config.get("server_host", "")
        if not server_host or "localhost" in server_host:
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                lan_ip = s.getsockname()[0]
                s.close()
                default_host = f"http://{lan_ip}:58092"
                config.set("server_host", default_host)
                logger.info(f"[Network] 自动检测 LAN IP: {lan_ip} → server_host={default_host}")
            except Exception:
                logger.warning("[Network] 无法检测 LAN IP, 请手动配置 server_host")

        # 自动启动对话监控
        if config.get("conversation_monitor_enabled", True):
            await _start_monitor()

        # 恢复闹钟任务
        _restore_alarms()

        # 启动 token 自动刷新定时任务（2h 检查 / 3h 阈值 / 60s 节流）
        from app.miot.token_refresh import start_refresh_loop
        start_refresh_loop(miauth)
        logger.info("[TokenRefresh] token 自动刷新任务已启动")

    yield

    # 关闭
    # 停止 token 自动刷新
    from app.miot.token_refresh import stop_refresh_loop
    stop_refresh_loop()
    # 停止下载 Worker
    stop_worker()
    _download_task.cancel()
    try:
        await _download_task
    except asyncio.CancelledError:
        pass
    _sync_task.cancel()
    try:
        await _sync_task
    except asyncio.CancelledError:
        pass

    # 取消定时器
    global _sleep_timer_task
    if _sleep_timer_task:
        _sleep_timer_task.cancel()
    for aid, task in _alarm_tasks.items():
        task.cancel()
    _alarm_tasks.clear()
    if monitor:
        await monitor.stop()
    if media_watcher:
        await media_watcher.stop()
    if miot_client:
        await miot_client.close()
    await miauth.close()
    logger.info("musicnest 已停止")


app.router.lifespan_context = lifespan


# ===== 辅助函数 =====

async def _init_miot_client(user_id: str, service_token: str) -> None:
    global miot_client
    ssecurity = config.get("miot_ssecurity", "")
    device_id = config.get("miot_device_id", "")
    miot_client = MinaHTTPClient(user_id, service_token, device_id, ssecurity)
    # 注入 401 自动刷新回调
    from app.miot.token_refresh import handle_token_expired
    miot_client.set_token_expired_callback(
        lambda: handle_token_expired(miauth)
    )


async def _start_monitor() -> None:
    global monitor, media_watcher
    if not miot_client:
        return

    poll_interval = config.get("poll_interval", 0.2)
    monitor = ConversationMonitor(miot_client, poll_interval)

    # 注册日志回调
    async def log_callback(device_id: str, msg: dict) -> None:
        query = msg.get("query", "")
        answer = msg.get("answer", "")
        logger.info(f"[对话] device={device_id[:8]}... query=\"{query}\" answer=\"{answer}\"")

    monitor.register_callback("logger", log_callback)
    # 注册语音指令回调
    monitor.register_callback("voice", _on_voice_message)

    # 获取设备列表并启动（强制刷新，确保启动时拿到最新列表）
    devices = await _get_device_list(use_cache=False)
    if devices:
        logger.info(f"发现 {len(devices)} 个设备，启动对话监控 (poll_interval={poll_interval}s)")
        await monitor.start(devices)

        # 启动轨道2：媒体状态高频轮询（兜底机制）
        if config.get("media_watcher_enabled", True):
            watcher_interval = config.get("media_watcher_interval", 0.2)
            media_watcher = MediaWatcher(miot_client, monitor, watcher_interval)
            media_watcher.register_intercept_callback("voice", _on_native_playback_intercept)
            await media_watcher.start(devices)
            logger.info(f"[MediaWatcher] 轨道2 已启动 (interval={watcher_interval}s)")
    else:
        logger.warning("未发现设备，跳过对话监控")


async def _check_miot() -> MinaHTTPClient:
    """检查并返回 miot_client"""
    if miot_client is None:
        token = config.miot_token
        user_id = config.get("miot_user_id", "")
        if token and user_id:
            await _init_miot_client(user_id, token)
        else:
            raise ValueError("小米账号未配置，请先授权登录")
    assert miot_client is not None
    return miot_client


# ===== 设备列表缓存（TTL 60s，避免高频远程调用） =====
_device_list_cache: list[dict] = []
_device_list_cache_at: float = 0.0
_DEVICE_LIST_CACHE_TTL = 60.0  # 秒


async def _get_device_list(use_cache: bool = True) -> list[dict]:
    """获取设备列表（带 60s TTL 缓存）

    Args:
        use_cache: True 时优先用缓存；False 强制刷新
    """
    global _device_list_cache, _device_list_cache_at
    now = time.time()
    if use_cache and _device_list_cache and (now - _device_list_cache_at) < _DEVICE_LIST_CACHE_TTL:
        return _device_list_cache
    client = await _check_miot()
    devices = await client.get_device_list()
    if devices:
        _device_list_cache = devices
        _device_list_cache_at = now
    return devices or []


def _restart_monitor_if_running() -> None:
    """如果监控在运行，重启它以应用新配置"""
    global monitor
    if monitor is not None and monitor.is_running:
        # 标记停止，下次生命周期会重新启动
        logger.info("监控配置已更新，需要重启服务生效")
        # 简单方案：停止再启动
        asyncio.ensure_future(_do_restart_monitor())


async def _do_restart_monitor() -> None:
    global monitor
    if monitor:
        await monitor.stop()
        monitor = None
    await _start_monitor()


# ===== 页面路由 =====

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """后台管理页面"""
    return _render_template("index.html", request)


@app.get("/logo-preview", response_class=HTMLResponse)
async def logo_preview(request: Request) -> HTMLResponse:
    """Logo 预览页"""
    return _render_template("logo-preview.html", request)


# ===== API 路由 =====

@app.get("/api/status")
async def api_status() -> dict:
    """服务状态"""
    return {
        "status": "running",
        "miot_configured": config.is_miot_configured(),
        "monitor_running": monitor.is_running if monitor else False,
        "voice_engine_enabled": voice_engine.enabled,
        "voice_commands_count": len(voice_engine.commands),
        "version": __version__,
    }


@app.get("/api/devices")
async def api_devices() -> dict:
    """设备列表（含勾选状态）"""
    try:
        client = await _check_miot()
        devices = await client.get_device_list()
        # 合并本地勾选状态
        selections: dict = config.get("device_selections", {})
        for d in devices:
            did = d.get("deviceID", "")
            d["selected"] = bool(selections.get(did, False))
        return {"code": 0, "data": devices}
    except ValueError:
        return {"code": 1, "msg": "未配置小米账号", "data": []}
    except Exception as e:
        logger.error(f"[API] /api/devices 获取设备列表失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e), "data": []}


@app.post("/api/devices/select")
async def api_devices_select(body: dict) -> dict:
    """更新设备勾选状态"""
    device_id = body.get("device_id", "")
    selected = body.get("selected", False)
    if not device_id:
        return {"code": 1, "msg": "缺少 device_id"}
    selections: dict = config.get("device_selections", {})
    selections[device_id] = bool(selected)
    config.set("device_selections", selections)
    logger.info(f"[DeviceSelect] device={device_id[:12]}... selected={selected}")
    return {"code": 0, "msg": "ok"} 


@app.post("/api/devices/auth")
async def api_devices_auth(body: dict) -> dict:
    """小米登录"""
    action = body.get("action", "")

    if action == "get_qr":
        result = await miauth.get_qr_code()
        if not result:
            return {"code": 1, "msg": "获取二维码失败"}
        config.set("_auth_lp_url", result["lp_url"])
        config.set("_auth_device_id", result["device_id"])
        return {"code": 0, "data": result}

    elif action == "password_login":
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            return {"code": 1, "msg": "请输入账号和密码"}
        result = await miauth.login_with_password(username, password)
        if result.get("ok"):
            service_token = result.get("serviceToken", "")
            user_id = result.get("userId", "")
            ssecurity = result.get("ssecurity", "")
            device_id = _generate_device_id()
            if service_token and user_id:
                config.update({
                    "miot_token": service_token,
                    "miot_user_id": user_id,
                    "miot_ssecurity": ssecurity,
                    "miot_device_id": device_id,
                })
                await _init_miot_client(user_id, service_token)
                # 记录 token 创建时间，供刷新循环估算有效期
                from app.miot.token_refresh import record_token_created
                record_token_created()
                return {"code": 0, "msg": "登录成功", "data": {"userId": user_id}}
        return {"code": 1, "msg": result.get("msg", "登录失败")}

    elif action == "poll_qr":
        lp_url = body.get("lp_url") or config.get("_auth_lp_url", "")
        device_id = body.get("device_id") or config.get("_auth_device_id", "")
        if not lp_url or not device_id:
            return {"code": 1, "msg": "请先生成二维码"}

        result = await miauth.poll_qr_result(lp_url, device_id)
        if not result:
            return {"code": 0, "msg": "等待扫码...", "data": None}

        if result.get("state") == "expired":
            return {
                "code": 0,
                "msg": result.get("message", "二维码已过期"),
                "data": {"expired": True},
            }

        if result.get("state") == "confirmed":
            pass_token = result.get("passToken", "")
            user_id = result.get("userId", "")
            c_user_id = result.get("cUserId", "")

            # 用 passToken 交换 micoapi 的 serviceToken
            token_result = await miauth.exchange_token(pass_token, user_id, c_user_id)
            if token_result:
                service_token = token_result["serviceToken"]
                user_id = token_result["userId"]
                ssecurity = token_result.get("ssecurity", "")

                config.update({
                    "miot_token": service_token,
                    "miot_user_id": user_id,
                    "miot_ssecurity": ssecurity,
                    "miot_device_id": device_id,
                    "miot_pass_token": pass_token,  # 持久化 passToken 用于后续自动刷新
                })
                config.set("_auth_lp_url", "")
                config.set("_auth_device_id", "")

                await _init_miot_client(user_id, service_token)
                # 记录 token 创建时间，供刷新循环估算有效期
                from app.miot.token_refresh import record_token_created
                record_token_created()
                return {
                    "code": 0,
                    "msg": "登录成功",
                    "data": {"userId": user_id},
                }

            return {"code": 1, "msg": "Token 交换失败"}

        # state == "waiting" / "failed"
        return {"code": 0, "msg": result.get("message", "等待扫码..."), "data": None}

    elif action == "check":
        if config.is_miot_configured():
            return {"code": 0, "data": {"configured": True, "userId": config.get("miot_user_id")}}
        return {"code": 0, "data": {"configured": False}}

    elif action == "logout":
        config.update({
            "miot_token": "",
            "miot_user_id": "",
            "miot_ssecurity": "",
            "miot_pass_token": "",
        })
        # 停止 token 自动刷新
        from app.miot.token_refresh import stop_refresh_loop
        stop_refresh_loop()
        global miot_client, monitor, media_watcher
        if miot_client:
            await miot_client.close()
            miot_client = None
        if monitor:
            await monitor.stop()
            monitor = None
        if media_watcher:
            await media_watcher.stop()
            media_watcher = None
        return {"code": 0, "msg": "已登出"}

    return {"code": 1, "msg": f"未知操作: {action}"}


@app.post("/api/search")
async def api_search(body: dict) -> dict:
    """搜索歌曲"""
    keyword = body.get("keyword", "")
    if not keyword:
        return {"code": 400, "msg": "缺少keyword", "data": None}

    local_results = scanner.search(keyword)
    if local_results:
        return {
            "code": 0,
            "msg": "success",
            "data": {
                "source": "local",
                "results": local_results[:10],
            },
        }

    return await search_by_keyword(keyword)


@app.get("/api/music/scan")
async def api_music_scan() -> dict:
    """手动扫描 NAS 音乐库"""
    songs = await scanner.scan()
    stats = scanner.get_stats()
    logger.info(f"音乐库扫描完成: {stats['total_songs']} 首歌曲")
    return {"code": 0, "data": {"songs_count": len(songs), "stats": stats}}


@app.get("/api/music/stats")
async def api_music_stats() -> dict:
    """音乐库统计"""
    stats = scanner.get_stats()
    return {"code": 0, "data": stats}


@app.get("/api/music/songs")
async def api_music_songs(limit: int = 500, offset: int = 0) -> dict:
    """获取歌曲列表"""
    songs = scanner.get_songs(limit=limit, offset=offset)
    stats = scanner.get_stats()
    return {"code": 0, "data": {"songs": songs, "total": stats["total_songs"]}}


@app.get("/api/music/play/{song_index}")
async def api_music_play(song_index: int, request: Request) -> Response:
    """播放指定索引的歌曲（返回音频流）"""
    songs = scanner.get_songs(limit=1, offset=song_index)
    if not songs:
        return JSONResponse({"code": 1, "msg": "歌曲不存在"}, status_code=404)
    song = songs[0]
    filepath = song.get("filepath", "")
    if not filepath or not os.path.isfile(filepath):
        return JSONResponse({"code": 1, "msg": "文件不存在"}, status_code=404)

    # MIME 类型自动检测
    _MIME_MAP = {'.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.wav': 'audio/wav',
                 '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
                 '.wma': 'audio/x-ms-wma'}
    ext = os.path.splitext(filepath)[1].lower()
    mime = _MIME_MAP.get(ext, 'audio/mpeg')

    # 根据请求参数决定是否转码 MP3（针对仅支持 MP3 的音箱）
    transcode = request.query_params.get("transcode", "0") == "1"
    if transcode and not filepath.lower().endswith('.mp3'):
        import hashlib
        import subprocess

        cache_dir = Path("/tmp/musicnest_audio")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.md5(filepath.encode()).hexdigest()
        cache_path = cache_dir / f"{cache_key}.mp3"

        if not cache_path.exists():
            try:
                subprocess.run([
                    "ffmpeg", "-i", filepath,
                    "-codec:a", "libmp3lame", "-b:a", "192k",
                    "-y", str(cache_path),
                ], check=True, capture_output=True)
                logger.info(f"[Transcode] {filepath} -> {cache_path}")
            except subprocess.CalledProcessError as e:
                logger.error(f"[Transcode] 转码失败: {filepath}, stderr={e.stderr.decode(errors='replace')[:200]}")
                # 回退：直接返回原文件
                return FileResponse(filepath, media_type=mime, filename=os.path.basename(filepath))

        # 转码成功用 MPEG 返回
        return FileResponse(str(cache_path), media_type="audio/mpeg", filename=os.path.basename(filepath))

    return FileResponse(filepath, media_type=mime, filename=os.path.basename(filepath))


@app.get("/api/music/proxy/{url_hash}")
async def api_music_proxy(url_hash: str) -> Response:
    """代理在线音频流（带上正确 header 拉取 KUWO），流式传输避免 OOM"""
    kuwo_url = _online_urls.get(url_hash)
    if not kuwo_url:
        return JSONResponse({"code": 1, "msg": "URL not found"}, status_code=404)

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
    try:
        req = client.build_request("GET", kuwo_url, headers=KUWO_HEADERS)
        resp = await client.send(req, follow_redirects=True)
        if resp.status_code != 200:
            await resp.aclose()
            await client.aclose()
            return JSONResponse({"code": 1, "msg": f"KUWO返回{resp.status_code}"}, status_code=502)

        media_type = resp.headers.get("content-type", "audio/mpeg")

        async def _stream_and_close():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(_stream_and_close(), media_type=media_type)
    except Exception as e:
        logger.error(f"[API] /api/music/proxy 代理音频流失败: {e}", exc_info=True)
        await client.aclose()
        return JSONResponse({"code": 1, "msg": str(e)}, status_code=502)


@app.get("/api/music/cover/{song_index}")
async def api_music_cover(song_index: int) -> Response:
    """获取歌曲封面图片（无封面时返回默认黑胶唱片图）"""
    songs = scanner.get_songs(limit=1, offset=song_index)
    if not songs:
        return Response(status_code=404)
    filepath = songs[0].get("filepath", "")
    if not filepath or not os.path.isfile(filepath):
        return Response(status_code=404)

    cache_dir = Path("/tmp/musicnest_cover")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(filepath.encode()).hexdigest()
    cache_path = cache_dir / f"{cache_key}.jpg"

    if not cache_path.exists():
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", filepath, "-an", "-vcodec", "mjpeg",
                 "-vframes", "1", "-y", str(cache_path)],
                capture_output=True, timeout=10
            )
            if result.returncode != 0 or not cache_path.exists():
                # 无封面 → 返回默认黑胶唱片图
                default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
                if os.path.isfile(default_svg):
                    return FileResponse(default_svg, media_type="image/svg+xml")
                return Response(status_code=404)
        except Exception:
            default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
            if os.path.isfile(default_svg):
                return FileResponse(default_svg, media_type="image/svg+xml")
            return Response(status_code=404)

    return FileResponse(str(cache_path), media_type="image/jpeg")


@app.get("/api/music/artist_cover")
async def api_music_artist_cover(artist: str = "") -> Response:
    """获取歌手封面图片（优先 artist.jpg/png，其次 cover.jpg/png）"""
    if not artist:
        return Response(status_code=404)
    music_path = config.get("music_path", "/music")
    artist_dir = Path(music_path) / artist
    if not artist_dir.exists() or not artist_dir.is_dir():
        default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
        if os.path.isfile(default_svg):
            return FileResponse(default_svg, media_type="image/svg+xml")
        return Response(status_code=404)
    # 优先查找 artist.jpg（与扫描器逻辑一致），其次 cover.jpg
    for prefix in ("artist", "cover"):
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            cover_path = artist_dir / f"{prefix}{ext}"
            if cover_path.exists():
                media_type_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                  ".png": "image/png", ".webp": "image/webp"}
                return FileResponse(str(cover_path), media_type=media_type_map.get(ext, "image/jpeg"))
    default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
    if os.path.isfile(default_svg):
        return FileResponse(default_svg, media_type="image/svg+xml")
    return Response(status_code=404)


@app.get("/api/music/album_cover")
async def api_music_album_cover(artist: str = "", album: str = "") -> Response:
    """获取专辑封面图片（专辑目录下的 cover.jpg/png）"""
    if not artist or not album:
        return Response(status_code=404)
    music_path = config.get("music_path", "/music")
    album_dir = Path(music_path) / artist / album
    return _serve_cover_from_dir(album_dir)


def _serve_cover_from_dir(directory: Path) -> Response:
    """从目录查找 cover 图片并返回"""
    if not directory.exists() or not directory.is_dir():
        default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
        if os.path.isfile(default_svg):
            return FileResponse(default_svg, media_type="image/svg+xml")
        return Response(status_code=404)
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cover_path = directory / f"cover{ext}"
        if cover_path.exists():
            media_type_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
            return FileResponse(str(cover_path), media_type=media_type_map.get(ext, "image/jpeg"))
    # No cover found, return default
    default_svg = os.path.join(os.path.dirname(__file__), "static", "vinyl.svg")
    if os.path.isfile(default_svg):
        return FileResponse(default_svg, media_type="image/svg+xml")
    return Response(status_code=404)


# ===== 音乐库删除与歌手/专辑列表 API =====


@app.get("/api/music/artists")
async def api_music_artists() -> dict:
    """获取歌手列表（名、歌曲数、封面路径）"""
    from collections import Counter
    artists: dict[str, dict] = {}
    for s in scanner._songs:
        name = s.get("artist", "未知歌手") or "未知歌手"
        if name not in artists:
            artists[name] = {"name": name, "song_count": 0, "cover": None}
        artists[name]["song_count"] += 1
        if not artists[name]["cover"]:
            # 尝试从本地取歌手图
            ap = s.get("artist_path", "")
            if ap:
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    p = os.path.join(ap, f"artist{ext}")
                    if os.path.isfile(p):
                        artists[name]["cover"] = p
                        break
    return {"code": 0, "data": sorted(artists.values(), key=lambda x: x["name"].lower())}


@app.get("/api/music/albums")
async def api_music_albums() -> dict:
    """获取专辑列表（名、歌手、歌曲数、封面路径）"""
    from collections import Counter
    albums: dict[str, dict] = {}
    for s in scanner._songs:
        album_name = s.get("album", "未知专辑") or "未知专辑"
        artist = s.get("artist", "") or ""
        key = (album_name, artist)
        if key not in albums:
            albums[key] = {"name": album_name, "artist": artist, "song_count": 0, "cover": None}
        albums[key]["song_count"] += 1
        if not albums[key]["cover"]:
            ap = s.get("album_path", "")
            if ap:
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    p = os.path.join(ap, f"cover{ext}")
                    if os.path.isfile(p):
                        albums[key]["cover"] = p
                        break
    return {"code": 0, "data": sorted(albums.values(), key=lambda x: (x["artist"].lower(), x["name"].lower()))}


def _fix_play_state_after_delete(deleted_filepath: Optional[str] = None) -> None:
    """歌曲删除后修复 play_state（防止 current_index 指向已删除的歌曲）"""
    if play_state.current_index is None:
        return
    if deleted_filepath:
        # 如果当前播放的歌曲正好是被删的那首，停止播放
        current = play_state.current_song()
        if current and current.get("filepath") == deleted_filepath:
            play_state.stop_playing()
            logger.info("[PlayState] 当前播放歌曲已被删除，已停止播放")
            return
    else:
        # 批量删除（歌手/专辑），检查当前歌曲是否还在 playlist 中
        current = play_state.current_song()
        if current:
            filepath = current.get("filepath", "")
            if filepath and filepath not in {s.get("filepath", "") for s in scanner._songs}:
                play_state.stop_playing()
                logger.info("[PlayState] 当前播放歌曲已被批量删除，已停止播放")


_AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".wma", ".aac"}


def _is_safe_path(path: str) -> bool:
    """校验路径是否在音乐库目录下，防止误删系统文件

    使用 Path.is_relative_to() 替代字符串前缀匹配，
    避免 /music_evil 这类前缀攻击绕过校验。
    """
    if not path:
        return False
    try:
        music_root = Path(config.get("music_path", "/music")).resolve()
        target = Path(path).resolve()
        # is_relative_to 在 Python 3.9+ 可用，正确判断父子目录关系
        return target.is_relative_to(music_root)
    except Exception:
        return False


def _has_audio_files(dirpath: str) -> bool:
    """检查目录下是否还有音频文件"""
    if not dirpath or not os.path.isdir(dirpath):
        return False
    for fname in os.listdir(dirpath):
        if os.path.splitext(fname)[1].lower() in _AUDIO_EXTS:
            return True
    return False


@app.post("/api/music/song/{index}/delete")
async def api_music_song_delete(index: int) -> dict:
    """删除单首歌曲（删文件+歌词），如果歌手目录无音频则删歌手目录"""
    songs = scanner.get_songs(limit=5000)
    if index < 0 or index >= len(songs):
        return {"code": 1, "msg": "歌曲索引越界"}
    song = songs[index]
    filepath = song.get("filepath", "")
    artist_path = song.get("artist_path", "")
    lyrics_path = song.get("lyrics_path", "")

    # 删音频
    deleted_audio = False
    if filepath and os.path.isfile(filepath):
        os.remove(filepath)
        deleted_audio = True
    # 删歌词
    if lyrics_path and os.path.isfile(lyrics_path):
        os.remove(lyrics_path)
    # 删歌曲目录下的封面（如果有且只属于这首歌）
    song_dir = os.path.dirname(filepath) if filepath else ""
    if song_dir and song_dir != artist_path:
        for fname in os.listdir(song_dir):
            fp = os.path.join(song_dir, fname)
            if os.path.isfile(fp) and os.path.splitext(fname)[1].lower() in {".jpg", ".jpeg", ".png", ".webp", ".lrc"}:
                os.remove(fp)
        # 专辑目录空则删
        if os.path.isdir(song_dir) and not os.listdir(song_dir):
            os.rmdir(song_dir)

    # 检查歌手目录是否还有音频
    if artist_path and not _has_audio_files(artist_path):
        import shutil
        shutil.rmtree(artist_path, ignore_errors=True)

    if deleted_audio:
        scanner.remove_song(index)
        _fix_play_state_after_delete(filepath)
    return {"code": 0, "msg": "已删除"}


@app.post("/api/music/artist/{artist_name}/delete")
async def api_music_artist_delete(artist_name: str) -> dict:
    """删除整个歌手文件夹及其所有歌曲"""
    import shutil
    from urllib.parse import unquote
    name = unquote(artist_name)
    # 校验路径安全性
    artist_dir_path = os.path.join(config.get("music_path", "/music"), name)
    if not _is_safe_path(artist_dir_path):
        return {"code": 1, "msg": "路径不在音乐库范围内，已拒绝删除"}
    deleted = 0
    for s in scanner._songs[:]:
        if s.get("artist") == name:
            fp = s.get("filepath", "")
            if fp and os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
    # 删歌手根目录
    for s in scanner._songs:
        ap = s.get("artist_path", "")
        if ap and s.get("artist") == name and os.path.isdir(ap):
            shutil.rmtree(ap, ignore_errors=True)
            break
    if deleted > 0:
        scanner._songs = [s for s in scanner._songs if s.get("artist") != name]
        scanner._save_cache()
        _fix_play_state_after_delete()  # 检查当前播放是否被删
    return {"code": 0, "msg": f"已删除 {deleted} 首歌曲"}


@app.post("/api/music/album/{album_name}/{artist_name}/delete")
async def api_music_album_delete(album_name: str, artist_name: str) -> dict:
    """删除整个专辑文件夹"""
    import shutil
    from urllib.parse import unquote
    aname = unquote(album_name)
    artname = unquote(artist_name)
    # 校验路径安全性
    album_dir_path = os.path.join(config.get("music_path", "/music"), artname, aname)
    if not _is_safe_path(album_dir_path):
        return {"code": 1, "msg": "路径不在音乐库范围内，已拒绝删除"}
    album_dir = ""
    deleted = 0
    for s in scanner._songs[:]:
        if s.get("album") == aname and s.get("artist") == artname:
            fp = s.get("filepath", "")
            ap = s.get("album_path", "")
            if ap and not album_dir:
                album_dir = ap
            if fp and os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
    if album_dir and os.path.isdir(album_dir):
        shutil.rmtree(album_dir, ignore_errors=True)
    # 检查歌手目录是否还有音频
    artist_path = ""
    for s in scanner._songs:
        if s.get("artist") == artname:
            artist_path = s.get("artist_path", "")
            break
    if artist_path and not _has_audio_files(artist_path):
        shutil.rmtree(artist_path, ignore_errors=True)
    if deleted > 0:
        scanner._songs = [s for s in scanner._songs if not (s.get("album") == aname and s.get("artist") == artname)]
        scanner._save_cache()
        _fix_play_state_after_delete()  # 检查当前播放是否被删
    return {"code": 0, "msg": f"已删除 {deleted} 首歌曲"}


@app.get("/api/monitor/status")
async def api_monitor_status() -> dict:
    """获取对话监控状态"""
    if monitor:
        return {"code": 0, "data": monitor.get_status()}
    return {"code": 0, "data": {"is_running": False, "device_count": 0, "message_count": 0}}


@app.get("/api/monitor/messages")
async def api_monitor_messages(limit: int = 50) -> dict:
    """获取对话消息"""
    if monitor:
        msgs = monitor.get_messages(limit=limit)
        return {"code": 0, "data": msgs}
    return {"code": 0, "data": []}


@app.post("/api/device/play")
async def api_device_play(body: dict) -> dict:
    """播放 URL（仅限已勾选设备，自动根据设备型号选择 API 和转码）"""
    try:
        client = await _check_miot()
        device_id = body.get("device_id", "")
        url = body.get("url", "")
        if not device_id or not url:
            return {"code": 1, "msg": "缺少 device_id 或 url"}
        # 检查设备是否已勾选
        selections: dict = config.get("device_selections", {})
        if not selections.get(device_id, False):
            return {"code": 1, "msg": "设备未勾选，请在设置中启用"}

        # 自动根据型号决定是否转码
        hardware = await _get_device_hardware(device_id)
        if needs_mp3(hardware) and "/api/music/play/" in url and "?transcode=" not in url:
            url += "?transcode=1"

        # 自动选择播放 API
        auto_api = config.get("auto_music_api", True)
        if auto_api:
            if needs_music_api(hardware):
                ok = await client.play_music_url(device_id, url)
                return {"code": 0, "data": {"success": ok, "api": "play_music_url", "hardware": hardware}}
        ok = await client.play_url(device_id, url)
        return {"code": 0, "data": {"success": ok, "api": "play_url"}}
    except Exception as e:
        logger.error(f"[API] /api/device/play 播放失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


@app.post("/api/device/control")
async def api_device_control(body: dict) -> dict:
    """设备控制: play/pause/stop/volume/tts（仅限已勾选设备）"""
    try:
        client = await _check_miot()
        device_id = body.get("device_id", "")
        action = body.get("action", "")
        if not device_id or not action:
            return {"code": 1, "msg": "缺少 device_id 或 action"}
        # 检查设备是否已勾选
        selections: dict = config.get("device_selections", {})
        if not selections.get(device_id, False):
            return {"code": 1, "msg": "设备未勾选，请在设置中启用"}

        ok = False
        if action == "play":
            ok = await client.player_play(device_id)
        elif action == "pause":
            ok = await client.player_pause(device_id)
        elif action == "stop":
            ok = await client.player_stop(device_id)
        elif action == "volume":
            vol = body.get("value", 50)
            ok = await client.set_volume(device_id, int(vol))
        elif action == "tts":
            text = body.get("text", "")
            ok = await client.text_to_speech(device_id, text)
        elif action == "status":
            status = await client.get_player_status(device_id)
            return {"code": 0, "data": status}

        return {"code": 0, "data": {"success": ok}}
    except Exception as e:
        logger.error(f"[API] /api/device/control 控制失败(action={body.get('action', '?')}): {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


# ===== 播放器 API =====

async def _get_target_device() -> Optional[str]:
    """获取第一个在线且已勾选的设备"""
    try:
        devices = await _get_device_list()
        selections: dict = config.get("device_selections", {})
        for d in devices:
            did = d.get("deviceID", "")
            if d.get("presence") == "online" and selections.get(did, False):
                return did
        return None
    except Exception:
        return None


async def _get_device_hardware(device_id: str) -> str:
    """根据 device_id 查询设备的 hardware 型号"""
    try:
        devices = await _get_device_list()
        for d in devices:
            if d.get("deviceID", "") == device_id:
                return d.get("hardware", "")
        return ""
    except Exception:
        return ""


async def _play_on_device(device_id: str, song_index: int) -> bool:
    """在指定设备上播放指定索引的歌曲，自动根据设备型号选择 API 和转码"""
    try:
        client = await _check_miot()
        selections: dict = config.get("device_selections", {})
        if not selections.get(device_id, False):
            logger.warning(f"[Player] 设备未勾选: {device_id[:12]}...")
            return False
        server_host = config.get("server_host", "http://localhost:58092")

        # 通过 filepath 找到正确的 scanner 索引（解决子集播放索引错位）
        song = play_state.current_song()
        if not song:
            logger.warning("[Player] 当前无歌曲")
            return False
        filepath = song.get("filepath", "")
        real_index = scanner.get_index_by_filepath(filepath)
        if real_index is None:
            logger.warning(f"[Player] 歌曲 filepath 不在扫描器中: {filepath[-60:]}")
            real_index = song_index  # fallback：用传入的索引

        # 根据型号决定是否转码
        hardware = await _get_device_hardware(device_id)
        needs_mp3_flag = needs_mp3(hardware)
        transcode_suffix = "?transcode=1" if needs_mp3_flag else ""
        url = f"{server_host}/api/music/play/{real_index}{transcode_suffix}"
        logger.info(f"[Player] 构造播放URL: server_host={server_host} song_index={song_index} "
                     f"real_index={real_index} filepath={filepath[-40:]} "
                     f"hardware={hardware!r} needs_mp3={needs_mp3_flag} url={url}")

        # 自动选择播放 API
        use_music_api = False
        auto_api = config.get("auto_music_api", True)
        if auto_api:
            use_music_api = needs_music_api(hardware)
            logger.info(f"[Player] device={device_id[:12]}... hardware={hardware!r} "
                        f"transcode={'yes' if transcode_suffix else 'no'} "
                        f"api={'play_music_url' if use_music_api else 'play_url'}")

        if use_music_api:
            ok = await client.play_music_url(device_id, url)
        else:
            ok = await client.play_url(device_id, url)

        if ok:
            logger.info(f"[Player] UBus 播放命令发送成功: index={song_index} device={device_id[:12]}...")
            play_state._play_start_time = time.monotonic()
            # 从歌曲元数据获取时长，供本地进度估算使用（设备不回报进度时必要）
            song = play_state.current_song()
            if song:
                song_duration = song.get("duration", 0)
                if isinstance(song_duration, (int, float)) and song_duration > 0:
                    play_state.duration = int(song_duration)
        else:
            logger.error(f"[Player] UBus 播放命令返回失败: index={song_index} device={device_id[:12]}...")
        return ok
    except Exception as e:
        logger.error(f"[Player] 播放异常: {e}", exc_info=True)
        return False


def _parse_player_info(raw: dict) -> dict:
    """解析 UBus player_get_play_status 响应。

    响应格式: {"code":0, "data":{"info":'{"status":1,"play_song_detail":{"position":12000,"duration":240000,"name":"歌名"}}'}}

    Returns:
        {"status": int, "position": int(秒), "duration": int(秒), "name": str}
        status: 1=播放中, 0=空闲/暂停; 解析失败返回 {"status": -1, ...}
    """
    result = {"status": -1, "position": 0, "duration": 0, "name": ""}
    if not isinstance(raw, dict):
        return result
    data = raw.get("data")
    info_str = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info_str, str):
        return result
    try:
        parsed = json.loads(info_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return result
    if not isinstance(parsed, dict):
        return result
    if "status" in parsed and isinstance(parsed["status"], (int, float)):
        result["status"] = int(parsed["status"])
    detail = parsed.get("play_song_detail")
    if isinstance(detail, dict):
        result["position"] = int(detail.get("position", 0)) // 1000  # ms → s
        result["duration"] = int(detail.get("duration", 0)) // 1000
        result["name"] = str(detail.get("name", "") or detail.get("title", ""))
    return result


@app.get("/api/player/state")
async def api_player_state() -> dict:
    """获取播放器状态，自动感知音箱是否正在独立播放"""
    state = play_state.get_state_dict()
    if state["current_index"] is not None:
        return {"code": 0, "data": state}
    
    # MusicNest 无活动歌单，检测音箱是否正在独立播放（如语音指令发起的播放）
    try:
        device_id = play_state.device_id or await _get_target_device()
        if not device_id:
            return {"code": 0, "data": state}
        
        client = await _check_miot()
        if not client:
            return {"code": 0, "data": state}
        
        raw = await client.get_player_status(device_id)
        if not raw:
            return {"code": 0, "data": state}
        
        info = _parse_player_info(raw)
        if info["status"] != 1:  # 1 = 播放中
            return {"code": 0, "data": state}
        
        duration = info["duration"]
        song_name = info["name"]
        
        # 尝试匹配正在播放的歌曲
        matched_song = None
        matched_index = None
        if song_name and scanner._songs:
            results = scanner.search(song_name)
            if results:
                matched_song = results[0]
                # 找到匹配歌曲在 _songs 中的索引
                for i, s in enumerate(scanner._songs):
                    if s.get("title") == matched_song.get("title") and s.get("artist") == matched_song.get("artist"):
                        matched_index = i
                        break
        
        state["is_playing"] = True
        state["duration"] = duration
        state["current_index"] = matched_index
        state["song"] = matched_song
        state["playlist_length"] = 0  # 表示非 MusicNest 歌单
        
        logger.info(f"[Player] 检测到音箱独立播放: name={song_name} duration={duration}s matched={matched_song is not None}")
        return {"code": 0, "data": state}
    except Exception as e:
        logger.debug(f"[Player] 检测音箱播放状态失败: {e}")
        return {"code": 0, "data": state}


@app.post("/api/player/play")
async def api_player_play(body: dict) -> dict:
    """播放指定歌曲"""
    song_index = body.get("song_index", 0)
    songs = body.get("songs", None)
    device_id = body.get("device_id", None)

    # 更新歌单
    if songs and isinstance(songs, list) and len(songs) > 0:
        play_state.playlist = songs
    elif not play_state.playlist:
        # 自动加载全部歌曲
        play_state.playlist = scanner.get_songs(limit=500)

    if not play_state.playlist:
        return {"code": 1, "msg": "播放列表为空"}

    if not isinstance(song_index, int) or song_index < 0 or song_index >= len(play_state.playlist):
        return {"code": 1, "msg": f"歌曲索引越界: {song_index}"}

    play_state.current_index = song_index

    # 确定播放设备
    if device_id:
        play_state.device_id = device_id
    if not play_state.device_id:
        play_state.device_id = await _get_target_device()

    if not play_state.device_id:
        return {"code": 1, "msg": "没有可用的播放设备（请确保设备在线且已勾选）"}

    # 实际播放
    ok = await _play_on_device(play_state.device_id, song_index)
    if ok:
        play_state.is_playing = True
        return {"code": 0, "data": play_state.get_state_dict()}
    else:
        play_state.stop_playing()
        return {"code": 1, "msg": "播放指令发送失败"}


@app.post("/api/player/toggle_play")
async def api_player_toggle_play(body: dict) -> dict:
    """切换播放/暂停"""
    device_id = body.get("device_id") or play_state.device_id
    try:
        client = await _check_miot()
        if not device_id:
            return {"code": 1, "msg": "未指定播放设备"}

        if play_state.is_playing:
            # 暂停：发送 pause 命令，保存已播放时间
            ok = await client.player_pause(device_id)
            if ok:
                play_state._pause_elapsed = time.monotonic() - play_state._play_start_time
                play_state.is_playing = False
                logger.info(f"[Player] 已暂停 (已播放 {play_state._pause_elapsed:.0f}s)")
            else:
                return {"code": 1, "msg": "暂停指令发送失败"}
        else:
            # 恢复播放：发送 play 命令（从暂停位置继续），回退 _play_start_time
            if play_state.current_index is not None:
                ok = await client.player_play(device_id)
                if ok:
                    play_state.is_playing = True
                    # 回退开始时间，使估算从暂停位置继续
                    play_state._play_start_time = time.monotonic() - play_state._pause_elapsed
                    logger.info(f"[Player] 已恢复播放 (从 {play_state._pause_elapsed:.0f}s 继续)")
                else:
                    return {"code": 1, "msg": "恢复播放失败"}
            else:
                return {"code": 1, "msg": "没有正在播放的歌曲"}

        return {"code": 0, "data": play_state.get_state_dict()}
    except Exception as e:
        logger.error(f"[API] /api/player/toggle_play 切换播放失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


@app.post("/api/player/next")
async def api_player_next() -> dict:
    """下一曲"""
    next_idx = play_state._get_next_index()
    if next_idx is None:
        play_state.stop_playing()
        logger.info("[Player] 没有下一曲，播放结束")
        return {"code": 0, "data": play_state.get_state_dict(), "msg": "播放列表已结束"}

    play_state.current_index = next_idx
    if play_state.device_id:
        ok = await _play_on_device(play_state.device_id, next_idx)
        play_state.is_playing = ok
        if not ok:
            return {"code": 1, "msg": "播放指令发送失败"}
    else:
        play_state.stop_playing()

    return {"code": 0, "data": play_state.get_state_dict()}


@app.post("/api/player/prev")
async def api_player_prev() -> dict:
    """上一曲"""
    prev_idx = play_state._get_prev_index()
    if prev_idx is None:
        # 重播当前歌曲
        if play_state.current_index is not None and play_state.device_id:
            ok = await _play_on_device(play_state.device_id, play_state.current_index)
            play_state.is_playing = ok
            return {"code": 0, "data": play_state.get_state_dict(), "msg": "已是第一首，重播当前歌曲"}
        return {"code": 0, "data": play_state.get_state_dict(), "msg": "没有上一曲"}

    play_state.current_index = prev_idx
    if play_state.device_id:
        ok = await _play_on_device(play_state.device_id, prev_idx)
        play_state.is_playing = ok
        if not ok:
            return {"code": 1, "msg": "播放指令发送失败"}
    else:
        play_state.stop_playing()

    return {"code": 0, "data": play_state.get_state_dict()}


@app.post("/api/player/mode")
async def api_player_mode(body: dict) -> dict:
    """设置播放模式"""
    mode_str = body.get("mode", "")
    # 支持直接设置或循环切换
    if mode_str == "cycle":
        # 循环切换到下一个模式
        try:
            current_idx = _PLAY_MODE_ORDER.index(play_state.mode)
        except ValueError:
            current_idx = 0
        next_idx = (current_idx + 1) % len(_PLAY_MODE_ORDER)
        play_state.mode = _PLAY_MODE_ORDER[next_idx]
    else:
        try:
            play_state.mode = PlayMode(mode_str)
        except ValueError:
            return {"code": 1, "msg": f"无效的播放模式: {mode_str}"}

    logger.info(f"[Player] 播放模式: {play_state.mode.value}")
    return {"code": 0, "data": play_state.get_state_dict()}


@app.post("/api/player/playlist")
async def api_player_playlist(body: dict) -> dict:
    """设置播放列表"""
    songs = body.get("songs", [])
    if not isinstance(songs, list):
        return {"code": 1, "msg": "songs 必须是数组"}
    play_state.playlist = songs
    if play_state.current_index is not None and play_state.current_index >= len(songs):
        play_state.current_index = None
        play_state.stop_playing()
    logger.info(f"[Player] 歌单已更新: {len(songs)} 首")
    return {"code": 0, "data": play_state.get_state_dict()}


@app.get("/api/player/progress")
async def api_player_progress() -> dict:
    """获取当前播放进度"""
    if not play_state.is_playing or not play_state.device_id:
        return {"code": 0, "data": {"position": 0, "duration": play_state.duration}}

    try:
        client = await _check_miot()
        if not client:
            return {"code": 0, "data": {"position": 0, "duration": play_state.duration}}

        raw = await client.get_player_status(play_state.device_id)
        if not raw:
            return {"code": 0, "data": {"position": 0, "duration": play_state.duration}}

        info = _parse_player_info(raw)
        position = info["position"]
        duration = info["duration"]

        if duration > 0:
            play_state.duration = duration

        # 本地回退：设备不报告位置时（如播 URL），用本地计时估算
        if position == 0 and play_state._play_start_time > 0:
            elapsed = int(time.monotonic() - play_state._play_start_time)
            if duration > 0:
                position = min(elapsed, duration)
            else:
                position = elapsed

        # duration 回退：设备不报告时长时，从歌曲元数据中获取
        final_duration = duration or play_state.duration
        if final_duration <= 0:
            song = play_state.current_song()
            if song:
                sd = song.get("duration", 0)
                if isinstance(sd, (int, float)) and sd > 0:
                    final_duration = int(sd)

        return {"code": 0, "data": {"position": position, "duration": final_duration}}
    except Exception as e:
        logger.error(f"[API] /api/player/progress 获取进度失败: {e}", exc_info=True)
        return {"code": 0, "data": {"position": 0, "duration": play_state.duration}}


@app.post("/api/player/seek")
async def api_player_seek(body: dict) -> dict:
    """跳转到指定位置"""
    position = body.get("position", 0)
    if not play_state.device_id:
        return {"code": 1, "msg": "没有活动的播放设备"}
    try:
        client = await _check_miot()
        ok = await client.seek(play_state.device_id, position)
        return {"code": 0, "data": {"success": ok}}
    except Exception as e:
        logger.error(f"[API] /api/player/seek 跳转失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


@app.post("/api/player/volume")
async def api_player_volume(body: dict) -> dict:
    """设置音量"""
    volume = body.get("volume", 50)
    try:
        client = await _check_miot()
        if not client:
            return {"code": 1, "msg": "未登录"}
        device_id = play_state.device_id or body.get("device_id", "")
        if not device_id:
            return {"code": 1, "msg": "没有活动的播放设备"}
        ok = await client.set_volume(device_id, volume)
        if ok:
            play_state.volume = volume
        return {"code": 0, "data": {"success": ok}}
    except Exception as e:
        logger.error(f"[API] /api/player/volume 设置音量失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


# ===== 定时器 API =====

@app.post("/api/timer/sleep")
async def api_timer_sleep(body: dict) -> dict:
    """设置睡眠定时"""
    minutes = int(body.get("minutes", 30))
    if minutes < 1 or minutes > 1440:
        return {"code": 1, "msg": "分钟数需在 1-1440 之间"}
    global _sleep_timer_task
    if _sleep_timer_task:
        _sleep_timer_task.cancel()
    _sleep_timer_task = asyncio.create_task(_sleep_timer(minutes))
    logger.info(f"[Timer] 睡眠定时设置: {minutes} 分钟")
    return {"code": 0, "data": {"minutes": minutes}}


@app.get("/api/timer/sleep")
async def api_timer_sleep_status() -> dict:
    """获取睡眠定时状态"""
    active = _sleep_timer_task is not None and not _sleep_timer_task.done()
    return {"code": 0, "data": {"active": active, "remaining_seconds": _sleep_timer_remaining}}


@app.post("/api/timer/sleep/cancel")
async def api_timer_sleep_cancel() -> dict:
    """取消睡眠定时"""
    global _sleep_timer_task, _sleep_timer_remaining
    if _sleep_timer_task:
        _sleep_timer_task.cancel()
        _sleep_timer_task = None
    _sleep_timer_remaining = 0
    return {"code": 0, "msg": "已取消"}


# ===== 闹钟 API =====

@app.post("/api/alarm")
async def api_alarm_create(body: dict) -> dict:
    """创建闹钟"""
    alarm_id = str(uuid.uuid4())[:8]
    hour = int(body.get("hour", 8))
    minute = int(body.get("minute", 0))
    days = body.get("days", [])  # 0=Mon ... 6=Sun
    song_index = body.get("song_index")
    song_hint = body.get("song_hint", "")

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return {"code": 1, "msg": "时间格式错误"}

    task = asyncio.create_task(_alarm_loop(alarm_id, hour, minute, days, song_index))
    _alarm_tasks[alarm_id] = task

    alarm_config = {
        "id": alarm_id, "hour": hour, "minute": minute,
        "days": days, "song_index": song_index, "enabled": True,
        "song_hint": song_hint,
    }
    alarms = config.get("alarms", [])
    alarms.append(alarm_config)
    config.set("alarms", alarms)

    return {"code": 0, "data": alarm_config}


@app.get("/api/alarm")
async def api_alarm_list() -> dict:
    """列出所有闹钟"""
    return {"code": 0, "data": config.get("alarms", [])}


@app.post("/api/alarm/delete")
async def api_alarm_delete(body: dict) -> dict:
    """删除闹钟"""
    alarm_id = body.get("id", "")
    if alarm_id in _alarm_tasks:
        _alarm_tasks[alarm_id].cancel()
        del _alarm_tasks[alarm_id]
    alarms = [a for a in config.get("alarms", []) if a.get("id") != alarm_id]
    config.set("alarms", alarms)
    return {"code": 0}


# ===== 歌单管理 API =====

@app.get("/api/playlist")
async def api_playlist_list() -> dict:
    """获取所有歌单"""
    playlists = config.get("playlists", [])
    return {"code": 0, "data": playlists}


@app.post("/api/playlist")
async def api_playlist_create(body: dict) -> dict:
    """创建歌单 {name}"""
    name = body.get("name", "").strip()
    if not name:
        return {"code": 1, "msg": "歌单名称不能为空"}
    playlists = config.get("playlists", [])
    # 检查重名
    for pl in playlists:
        if pl.get("name") == name:
            return {"code": 1, "msg": "同名歌单已存在"}
    playlist_id = str(uuid.uuid4())[:8]
    playlist = {
        "id": playlist_id,
        "name": name,
        "songs": [],
        "created_at": datetime.now().isoformat(),
    }
    playlists = config.get("playlists", [])
    playlists.append(playlist)
    config.set("playlists", playlists)
    logger.info(f"[Playlist] 创建歌单: id={playlist_id} name={name}")
    return {"code": 0, "data": playlist}


@app.post("/api/playlist/{playlist_id}")
async def api_playlist_update(playlist_id: str, body: dict) -> dict:
    """更新歌单 {name?, songs?}"""
    playlists = config.get("playlists", [])
    for pl in playlists:
        if pl.get("id") == playlist_id:
            if "name" in body:
                new_name = body["name"].strip()
                # 检查重名（排除自己）
                for pl2 in playlists:
                    if pl2.get("id") != playlist_id and pl2.get("name") == new_name:
                        return {"code": 1, "msg": "同名歌单已存在"}
                pl["name"] = new_name
            if "songs" in body:
                pl["songs"] = body["songs"]
            config.set("playlists", playlists)
            logger.info(f"[Playlist] 更新歌单: id={playlist_id}")
            return {"code": 0, "data": pl}
    return {"code": 1, "msg": "歌单不存在"}


@app.post("/api/playlist/{playlist_id}/delete")
async def api_playlist_delete(playlist_id: str) -> dict:
    """删除歌单"""
    playlists = config.get("playlists", [])
    new_list = [pl for pl in playlists if pl.get("id") != playlist_id]
    if len(new_list) == len(playlists):
        return {"code": 1, "msg": "歌单不存在"}
    config.set("playlists", new_list)
    logger.info(f"[Playlist] 删除歌单: id={playlist_id}")
    return {"code": 0, "msg": "已删除"}


@app.post("/api/playlist/{playlist_id}/add")
async def api_playlist_add_song(playlist_id: str, body: dict) -> dict:
    """向歌单添加歌曲 {filepath}"""
    filepath = body.get("filepath")
    if not filepath:
        return {"code": 1, "msg": "缺少 filepath"}
    filepath = str(filepath)
    playlists = config.get("playlists", [])
    for pl in playlists:
        if pl.get("id") == playlist_id:
            songs = pl.get("songs", [])
            if filepath not in songs:
                songs.append(filepath)
                pl["songs"] = songs
                config.set("playlists", playlists)
                logger.info(f"[Playlist] 添加歌曲: playlist={playlist_id} filepath={filepath[-40:]}")
                return {"code": 0, "data": pl}
            return {"code": 0, "data": pl, "msg": "歌曲已在歌单中"}
    return {"code": 1, "msg": "歌单不存在"}


@app.post("/api/playlist/{playlist_id}/remove")
async def api_playlist_remove_song(playlist_id: str, body: dict) -> dict:
    """从歌单移除歌曲 {filepath}"""
    filepath = body.get("filepath")
    if not filepath:
        return {"code": 1, "msg": "缺少 filepath"}
    filepath = str(filepath)
    playlists = config.get("playlists", [])
    for pl in playlists:
        if pl.get("id") == playlist_id:
            songs = pl.get("songs", [])
            if filepath in songs:
                songs.remove(filepath)
                pl["songs"] = songs
                config.set("playlists", playlists)
                logger.info(f"[Playlist] 移除歌曲: playlist={playlist_id}")
                return {"code": 0, "data": pl}
            return {"code": 0, "data": pl, "msg": "歌曲不在歌单中"}
    return {"code": 1, "msg": "歌单不存在"}


# ===== 配置 API =====

@app.post("/api/config")
async def api_config(body: dict) -> dict:
    """更新配置"""
    config.update(body)

    cookie_val = body.get("netease_cookie", "")
    if cookie_val:
        logger.info("[Config] 网易云 Cookie 已更新: %d 字符", len(cookie_val))
        # 同时写入嵌套结构，确保搜索/下载能读到
        netease_config = config.get("netease", {})
        if isinstance(netease_config, dict):
            netease_config["cookie"] = cookie_val
            config.set("netease", netease_config)

    # 如果更新了定时扫描间隔，实时生效
    if "auto_scan_interval" in body:
        scanner.set_auto_scan(int(body["auto_scan_interval"]))

    # 如果更新了语音引擎开关，实时生效
    if "voice_engine_enabled" in body:
        voice_engine.enabled = bool(body["voice_engine_enabled"])

    # 如果更新了对话监控开关，实时生效
    if "conversation_monitor_enabled" in body:
        enabled = bool(body["conversation_monitor_enabled"])
        if enabled and miot_client:
            if monitor is None or not monitor.is_running:
                await _start_monitor()
                logger.info("[Config] 对话监控已实时启动")
        else:
            if monitor and monitor.is_running:
                await monitor.stop()
                logger.info("[Config] 对话监控已实时停止")

    # 如果更新了 poll_interval，通知日志
    if "poll_interval" in body and monitor and monitor.is_running:
        logger.info("轮询间隔已更新，需重启服务后生效")

    # 如果更新了 debug_logging，重新设置调试日志
    if "debug_logging" in body:
        _setup_debug_logging()

    return {"code": 0, "data": config.get_all()}


@app.get("/api/config")
async def api_get_config() -> dict:
    """获取配置"""
    return {"code": 0, "data": {**config.get_all(), "version": app.version}}


# ===== 语音指令 CRUD API =====

@app.get("/api/voice/commands")
async def api_voice_commands() -> dict:
    """获取所有语音指令"""
    return {"code": 0, "data": _serialize_commands()}


@app.post("/api/voice/commands")
async def api_voice_add_command(body: dict) -> dict:
    """添加语音指令"""
    cmd_type = body.get("type", "play_song")
    keywords = body.get("keywords", [])
    param = body.get("param")
    enabled = body.get("enabled", True)

    if not keywords:
        return {"code": 1, "msg": "关键词列表不能为空"}

    cmd = VoiceCommand(type=cmd_type, keywords=keywords, param=param, enabled=enabled)
    voice_engine.add_command(cmd)
    config.set("voice_commands", _serialize_commands())
    logger.info(f"[VoiceCmd] 新增指令: type={cmd_type} keywords={keywords}")
    return {"code": 0, "msg": "添加成功", "data": _serialize_commands()}


@app.put("/api/voice/commands/{index}")
async def api_voice_update_command(index: int, body: dict) -> dict:
    """更新指定索引的语音指令"""
    commands = voice_engine.commands
    if index < 0 or index >= len(commands):
        return {"code": 1, "msg": "索引越界"}

    old = commands[index]
    updated = VoiceCommand(
        type=body.get("type", old.type),
        keywords=body.get("keywords", old.keywords),
        param=body.get("param", old.param),
        enabled=body.get("enabled", old.enabled),
    )

    all_cmds = voice_engine.commands
    all_cmds[index] = updated
    voice_engine.set_commands(all_cmds)
    config.set("voice_commands", _serialize_commands())
    logger.info(f"[VoiceCmd] 更新指令 [{index}]: type={updated.type}")
    return {"code": 0, "msg": "更新成功", "data": _serialize_commands()}


@app.delete("/api/voice/commands/{index}")
async def api_voice_delete_command(index: int) -> dict:
    """删除指定索引的语音指令"""
    removed = voice_engine.remove_command(index)
    if removed is None:
        return {"code": 1, "msg": "索引越界"}
    config.set("voice_commands", _serialize_commands())
    logger.info(f"[VoiceCmd] 删除指令 [{index}]: type={removed.type}")
    return {"code": 0, "msg": "删除成功", "data": _serialize_commands()}


@app.post("/api/voice/commands/reset")
async def api_voice_reset_commands() -> dict:
    """重置为默认语音指令"""
    voice_engine.set_commands(_default_commands())
    config.set("voice_commands", _serialize_commands())
    logger.info("[VoiceCmd] 已重置为默认指令")
    return {"code": 0, "msg": "重置成功", "data": _serialize_commands()}


@app.get("/api/voice/status")
async def api_voice_status() -> dict:
    """语音引擎状态"""
    return {
        "code": 0,
        "data": {
            "enabled": voice_engine.enabled,
            "total_commands": len(voice_engine.commands),
        },
    }


# ===== 在线搜索 API =====

@app.get("/api/search/online")
async def api_search_online(keyword: str = "", search_type: str = "song", source: str = "all", limit: int = 20) -> dict:
    """在线音乐搜索（酷我 + 网易云）
    search_type: song(歌曲), artist(歌手), album(专辑)
    """
    if not keyword:
        return {"code": 1, "msg": "缺少 keyword", "data": []}

    tasks = []
    if source in ("all", "kuwo"):
        tasks.append(kuwo_search(keyword, limit=limit, search_type=search_type, skip_formats=True))
    if source in ("all", "netease"):
        cookie = config.get("netease", {}).get("cookie", "")
        tasks.append(netease_search(keyword, limit=limit, search_type=search_type, cookie=cookie, skip_formats=True))

    if not tasks:
        return {"code": 1, "msg": "无效的音源", "data": []}

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)

    # 所有结果全量发送到前端（前端自己按 title|artist 分组合并显示多音源）
    return {"code": 0, "data": [r.to_dict() for r in all_results]}


@app.get("/api/artist/{source}/{artist_id}")
async def api_artist_detail(source: str, artist_id: str) -> dict:
    """获取歌手详情（基本信息 + 热门歌曲 + 专辑列表）"""
    try:
        if source == "kuwo":
            from app.search.kuwo import get_artist_detail as kuwo_artist
            data = await kuwo_artist(artist_id)
        elif source == "netease":
            from app.search.netease import get_artist_detail as netease_artist
            data = await netease_artist(artist_id)
        else:
            return {"code": 1, "msg": f"不支持的音源: {source}"}
        return {"code": 0, "data": data}
    except Exception as e:
        logger.error(f"[Artist] 获取歌手详情失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


@app.get("/api/album/{source}/{album_id}")
async def api_album_detail(source: str, album_id: str) -> dict:
    """获取专辑详情（基本信息 + 曲目列表）"""
    try:
        if source == "kuwo":
            from app.search.kuwo import get_album_detail as kuwo_album
            data = await kuwo_album(album_id)
        elif source == "netease":
            from app.search.netease import get_album_detail as netease_album
            data = await netease_album(album_id)
        else:
            return {"code": 1, "msg": f"不支持的音源: {source}"}
        return {"code": 0, "data": data}
    except Exception as e:
        logger.error(f"[Album] 获取专辑详情失败: {e}", exc_info=True)
        return {"code": 1, "msg": str(e)}


# ===== 下载队列 API =====

@app.post("/api/download/song")
async def api_download_song(body: dict) -> dict:
    """添加歌曲到下载队列"""
    source = body.get("source", "")
    music_id = body.get("music_id", "")
    title = body.get("title", "")
    artist = body.get("artist", "")
    album = body.get("album", "")
    cover_url = body.get("cover_url", "")
    format_type = body.get("format_type", "flac")

    if not source or not music_id:
        return {"code": 1, "msg": "缺少 source 或 music_id"}

    # FLAC 优先
    if config.get("download", {}).get("flac_priority", True) and format_type != "flac":
        format_type = "flac"

    task_id = f"dl_{uuid.uuid4().hex[:12]}"
    task = add_task(
        task_id=task_id,
        source=source,
        music_id=music_id,
        title=title or "未知歌曲",
        artist=artist or "未知歌手",
        album=album or "未知专辑",
        cover_url=cover_url or "",
        format_type=format_type,
    )

    stats = get_download_stats()
    position = stats.get("waiting", 0) + stats.get("loading", 0)

    logger.info(f"[Download] 添加到队列: {task_id} {title} - {artist} [{source}]")
    return {
        "code": 0,
        "data": {
            "task_id": task_id,
            "position_in_queue": position,
        },
    }


@app.post("/api/download/album")
async def api_download_album(body: dict) -> dict:
    """下载整张专辑"""
    source = body.get("source", "")
    album_id = body.get("album_id", "")
    title = body.get("title", "")
    artist = body.get("artist", "")
    album_name = body.get("album", "")
    cover_url = body.get("cover_url", "")
    format_type = body.get("format_type", "flac")

    if not source or not album_id:
        return {"code": 1, "msg": "缺少 source 或 album_id"}

    if source == "netease":
        from app.search.netease import get_album_tracks
        cookie = config.get("netease", {}).get("cookie", "")
        tracks = await get_album_tracks(album_id, cookie=cookie)
    else:
        # 酷我不支持专辑下载
        return {"code": 1, "msg": "酷我暂不支持专辑下载"}

    if not tracks:
        return {"code": 1, "msg": "未获取到专辑歌曲"}

    count = 0
    for track in tracks:
        task_id = f"dl_{uuid.uuid4().hex[:12]}"
        add_task(
            task_id=task_id,
            source=source,
            music_id=track.id.replace(f"{source}_", ""),
            title=track.title,
            artist=track.artist,
            album=track.album,
            cover_url=track.cover or cover_url or "",
            format_type=format_type,
        )
        count += 1

    logger.info(f"[Download] 专辑下载: {album_name} 共 {count} 首已加入队列")
    return {
        "code": 0,
        "data": {"count": count, "album": album_name},
    }


@app.get("/api/download/tasks")
async def api_download_tasks(status: str = "", limit: int = 50, offset: int = 0) -> dict:
    """获取下载任务列表"""
    tasks = get_tasks(status=status, limit=limit, offset=offset)
    stats = get_download_stats()
    return {
        "code": 0,
        "data": {
            "tasks": [t.to_dict() for t in tasks],
            "stats": stats,
        },
    }


@app.get("/api/download/stats")
async def api_download_stats() -> dict:
    """获取下载统计"""
    return {"code": 0, "data": get_download_stats()}


@app.post("/api/download/tasks/{task_id}/retry")
async def api_download_retry(task_id: str) -> dict:
    """重试失败的下载任务"""
    task = get_task_by_id(task_id)
    if not task:
        return {"code": 1, "msg": "任务不存在"}
    if task.status != "error":
        return {"code": 1, "msg": "只能重试失败的任务"}
    update_task_status(task_id, "waiting", error_msg="", file_path="")
    logger.info(f"[Download] 重试任务: {task_id}")
    return {"code": 0, "msg": "已重新加入队列"}


@app.delete("/api/download/tasks/{task_id}")
async def api_download_delete_task(task_id: str) -> dict:
    """删除下载任务"""
    delete_task(task_id)
    return {"code": 0, "msg": "已删除"}


@app.post("/api/download/tasks/clear")
async def api_download_clear_tasks() -> dict:
    """清空已完成的任务"""
    clear_finished_tasks()
    return {"code": 0, "msg": "已清空"}


# ===== 歌单监听 API =====

@app.get("/api/playlist-sync")
async def api_playlist_sync_list() -> dict:
    """获取所有监听歌单"""
    playlists = config.get("playlist_sync", [])
    # 补充同步状态
    result = []
    for pl in playlists:
        source = pl.get("source", "")
        pl_id = pl.get("id", "")
        synced = len(get_synced_ids(source, pl_id))
        pl_copy = dict(pl)
        pl_copy["synced_count"] = synced
        result.append(pl_copy)
    return {"code": 0, "data": result}


@app.post("/api/playlist-sync")
async def api_playlist_sync_add(body: dict) -> dict:
    """添加歌单监听"""
    try:
        source = body.get("source", "netease")
        playlist_url_or_id = body.get("url", "").strip()

        if not playlist_url_or_id:
            return {"code": 1, "msg": "请输入歌单链接或ID"}

        # 从 URL 中提取 ID
        pl_id = playlist_url_or_id
        if "playlist?id=" in playlist_url_or_id:
            import re as _re
            m = _re.search(r"id=(\d+)", playlist_url_or_id)
            if m:
                pl_id = m.group(1)
        elif playlist_url_or_id.isdigit():
            pl_id = playlist_url_or_id

        # 验证歌单
        if source == "netease":
            cookie = config.get("netease", {}).get("cookie", "")
            tracks = await netease_get_playlist_tracks(pl_id, cookie=cookie)
            if not tracks:
                return {"code": 1, "msg": "验证失败，请检查歌单ID是否正确，或Cookie是否有效"}

            # 获取歌单名称（从第一个 track 的 album 信息推断，或直接用 ID）
            pl_name = body.get("name", f"网易云歌单-{pl_id}")
            # 尝试获取歌单名称
            from app.search.netease import _netease_request
            detail = await _netease_request("/playlist/detail", params={"id": pl_id}, cookie=cookie)
            if detail.get("code") == 200:
                pl_name = detail.get("playlist", {}).get("name", pl_name)

            playlist_info = {
                "source": source,
                "id": pl_id,
                "name": pl_name,
                "enabled": True,
                "track_count": len(tracks),
            }
        else:
            return {"code": 1, "msg": "暂不支持该音源的歌单"}

        # 保存到配置
        playlists = config.get("playlist_sync", [])
        # 检查重复
        for pl in playlists:
            if pl.get("source") == source and pl.get("id") == pl_id:
                return {"code": 1, "msg": "该歌单已在监听列表中"}

        playlists.append(playlist_info)
        config.set("playlist_sync", playlists)

        # 立即同步一次
        try:
            from app.download.worker import playlist_sync_worker as _psw
        except Exception as e:
            logger.warning(f"[Startup] playlist_sync_worker 导入失败: {e}")
            pass

        logger.info(f"[PlaylistSync] 添加歌单: {pl_name} ({source}/{pl_id})")
        return {"code": 0, "data": playlist_info}
    except Exception as e:
        logger.exception(f"[PlaylistSync] 添加歌单异常: {e}")
        return {"code": 1, "msg": f"添加歌单失败: {e}"}


@app.delete("/api/playlist-sync/{pl_index}")
async def api_playlist_sync_remove(pl_index: int) -> dict:
    """移除歌单监听"""
    playlists = config.get("playlist_sync", [])
    if pl_index < 0 or pl_index >= len(playlists):
        return {"code": 1, "msg": "歌单不存在"}
    removed = playlists.pop(pl_index)
    config.set("playlist_sync", playlists)
    logger.info(f"[PlaylistSync] 移除歌单: {removed.get('name', '')}")
    return {"code": 0, "msg": "已移除"}


@app.post("/api/playlist-sync/{pl_index}/refresh")
async def api_playlist_sync_refresh(pl_index: int) -> dict:
    """立即刷新指定歌单（触发同步）"""
    playlists = config.get("playlist_sync", [])
    if pl_index < 0 or pl_index >= len(playlists):
        return {"code": 1, "msg": "歌单不存在"}

    pl = playlists[pl_index]
    source = pl.get("source", "")
    pl_id = pl.get("id", "")
    pl_name = pl.get("name", "")

    if source == "netease":
        cookie = config.get("netease", {}).get("cookie", "")
        tracks = await netease_get_playlist_tracks(pl_id, cookie=cookie)
    else:
        return {"code": 1, "msg": "暂不支持该音源"}

    synced = get_synced_ids(source, pl_id)
    new_tracks = [t for t in tracks if t.id not in synced]

    count = 0
    flac_priority = config.get("download", {}).get("flac_priority", True)
    fmt = "flac" if flac_priority else "mp3"

    for track in new_tracks:
        task_id = f"{source}_sync_{pl_id}_{track.id}"
        add_task(
            task_id=task_id,
            source=source,
            music_id=track.id.replace(f"{source}_", ""),
            title=track.title,
            artist=track.artist,
            album=track.album,
            cover_url=track.cover or "",
            format_type=fmt,
        )
        record_sync(source, pl_id, track.id)
        count += 1

    logger.info(f"[PlaylistSync] 手动同步歌单 [{pl_name}]: 新增 {count} 首待下载")
    return {
        "code": 0,
        "data": {
            "total_tracks": len(tracks),
            "new_tracks": count,
            "synced_tracks": len(synced),
        },
    }


# ===== 网易云 Cookie 验证 =====

@app.post("/api/netease/verify-cookie")
async def api_netease_verify_cookie(body: dict) -> dict:
    """验证网易云 Cookie"""
    cookie = body.get("cookie", "")
    if not cookie:
        return {"code": 1, "msg": "请输入 Cookie"}
    valid = await netease_verify_cookie(cookie)
    if valid:
        return {"code": 0, "msg": "Cookie 有效", "data": {"valid": True}}
    return {"code": 1, "msg": "Cookie 无效，请重新获取", "data": {"valid": False}}
