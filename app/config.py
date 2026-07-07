"""配置管理 - 持久化到 /data/config.yaml"""

import copy
import logging
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
    "poll_interval": 0.2,                       # 对话轮询间隔（轨道1）— 0.2s 快速捕获
    "conversation_monitor_enabled": False,
    "media_watcher_enabled": True,              # 轨道2：播放状态高频轮询兜底
    "media_watcher_interval": 0.2,              # 轨道2 轮询间隔（秒）
    "log_lines": 200,
    "auto_scan_interval": 0,
    "voice_engine_enabled": True,
    "auto_music_api": True,         # 自动根据设备型号选择播放 API（play_music_url vs play_url）
    "online_only_voice": False,     # 语音指令只走在线播放（不播NAS，避免转码耗时）；web页面点播不受影响
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
        {"type": "download_current", "keywords": ["下载当前歌曲", "下载这首歌", "下载当前", "下载此歌"], "enabled": True},
        {"type": "download", "keywords": ["下载歌曲", "下载"], "enabled": True},
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
}

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")


class ConfigManager:
    """线程安全的配置管理器，自动持久化到 YAML 文件"""

    def __init__(self, config_path: str = CONFIG_PATH):
        self._config_path = config_path
        self._lock = threading.Lock()
        # C1: 深拷贝避免 DEFAULT_CONFIG 被永久污染
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        try:
            self._load()
        except Exception as e:
            logging.getLogger("musicnest.config").warning(
                "[Config] 配置加载失败，使用默认配置: %s", e
            )
        if not Path(self._config_path).exists():
            try:
                self._save()
            except Exception as e:
                logging.getLogger("musicnest.config").warning(
                    "[Config] 默认配置保存失败，使用内存配置: %s", e
                )

    def _deep_merge(self, default: dict, loaded: dict) -> dict:
        """递归合并：loaded 覆盖 default，对 dict 子键递归合并"""
        result = copy.deepcopy(default)
        for key, val in loaded.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = self._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    def _load(self) -> None:
        """从文件加载配置，不存在则创建默认配置"""
        path = Path(self._config_path)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                with self._lock:
                    self._data = self._deep_merge(dict(DEFAULT_CONFIG), loaded)
            except Exception as e:
                logging.getLogger("musicnest.config").warning(
                    "[Config] 配置文件加载失败，使用默认配置: %s", e
                )
        else:
            self._save()

    def _save(self) -> None:
        """保存配置到文件（原子写入）"""
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = copy.deepcopy(self._data)
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)
            os.replace(str(tmp_path), str(path))  # 原子替换
        except Exception as e:
            logging.getLogger("musicnest.config").error(
                "[Config] 配置保存失败: %s", e
            )
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            val = self._data.get(key, default)
            if isinstance(val, (list, dict)):
                return copy.deepcopy(val)
            return val

    def set(self, key: str, value: Any) -> None:
        # M1: 先更新内存后持久化，_save 失败时内存已改但文件未持久化（接受此语义，避免回滚引入复杂性）
        # H1: 深拷贝入参，避免外部对象与配置内部状态共享引用
        with self._lock:
            self._data[key] = copy.deepcopy(value)
        self._save()

    def update(self, updates: dict[str, Any]) -> None:
        # M1: 先更新内存后持久化，_save 失败时内存已改但文件未持久化（接受此语义，避免回滚引入复杂性）
        # H1: 深拷贝入参，避免外部对象与配置内部状态共享引用
        with self._lock:
            for k, v in updates.items():
                self._data[k] = copy.deepcopy(v)
        self._save()

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

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
