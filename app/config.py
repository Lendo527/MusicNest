"""配置管理 - 持久化到 /data/config.yaml"""

import os
import threading
from pathlib import Path
from typing import Any, Optional

import yaml


DEFAULT_CONFIG = {
    "server_host": "http://localhost:58092",
    "miot_token": "",
    "miot_user_id": "",
    "miot_device_id": "",
    "miot_ssecurity": "",
    "music_path": os.environ.get("MUSIC_PATH", "/music"),
    "poll_interval": 0.5,
    "conversation_monitor_enabled": False,
    "log_lines": 200,
    "auto_scan_interval": 0,
    "voice_engine_enabled": True,
    "tts_enabled": True,            # TTS 语音反馈开关（语音指令执行后播报结果）
    "auto_music_api": True,         # 自动根据设备型号选择播放 API（play_music_url vs play_url）
    "debug_logging": True,          # 启用调试日志（输出到 /data/debug.log）
    "device_selections": {},  # deviceID -> bool: 勾选的设备才参与拦截和播放
    "voice_commands": [
        {"type": "play_playlist", "keywords": ["播放歌单", "放歌单", "播放列表"], "enabled": True},
        {"type": "play_song", "keywords": ["播放歌曲", "放歌曲", "我想听", "播放"], "enabled": True},
        {"type": "set_play_mode", "keywords": ["随机播放", "随机模式"], "param": "random", "enabled": True},
        {"type": "set_play_mode", "keywords": ["单曲循环", "循环播放这首"], "param": "single", "enabled": True},
        {"type": "set_play_mode", "keywords": ["列表循环", "循环播放"], "param": "loop", "enabled": True},
        {"type": "set_play_mode", "keywords": ["顺序播放"], "param": "order", "enabled": True},
        {"type": "set_volume", "keywords": ["设置音量", "音量调到", "音量", "声音", "声音调到"], "param": "absolute", "enabled": True},
        {"type": "set_volume", "keywords": ["大声一点", "声音大一点", "音量大一点"], "param": "up", "enabled": True},
        {"type": "set_volume", "keywords": ["小声一点", "声音小一点", "音量小一点"], "param": "down", "enabled": True},
        {"type": "next", "keywords": ["下一首", "切歌", "换一首", "下一曲"], "enabled": True},
        {"type": "previous", "keywords": ["上一首", "上一曲"], "enabled": True},
        {"type": "stop", "keywords": ["停止播放", "停止", "别播了", "关掉音乐", "关机"], "enabled": True},
    ],
    "alarms": [],
    "playlists": [],  # [{"id", "name", "songs": [索引列表], "created_at"}]
    "playlist_sync": [],  # [{"source", "id", "name", "enabled"}]
    "playlist_sync_interval": 1800,  # 歌单同步间隔（秒）
    "download": {
        "flac_priority": True,  # 优先下载 FLAC 格式
    },
    "netease": {
        "cookie": "",  # 网易云 Cookie
        "enabled": True,
    },
    "tts_enabled": True,
}

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")


class ConfigManager:
    """线程安全的配置管理器，自动持久化到 YAML 文件"""

    def __init__(self, config_path: str = CONFIG_PATH):
        self._config_path = config_path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self) -> None:
        """从文件加载配置，不存在则创建默认配置"""
        path = Path(self._config_path)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                with self._lock:
                    for key in DEFAULT_CONFIG:
                        if key in loaded:
                            self._data[key] = loaded[key]
            except Exception:
                pass
        else:
            self._save()

    def _save(self) -> None:
        """保存配置到文件"""
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = dict(self._data)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self._save()

    def update(self, updates: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(updates)
        self._save()

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    @property
    def miot_token(self) -> str:
        return self.get("miot_token", "")

    @miot_token.setter
    def miot_token(self, value: str) -> None:
        self.set("miot_token", value)

    @property
    def miot_user_id(self) -> str:
        return self.get("miot_user_id", "")

    @miot_user_id.setter
    def miot_user_id(self, value: str) -> None:
        self.set("miot_user_id", value)

    def is_miot_configured(self) -> bool:
        """检查小米账号是否已配置"""
        return bool(self.miot_token and self.miot_user_id)


# 全局单例
config = ConfigManager()
