"""搜索接口抽象层 - 统一结果结构 + Provider ABC"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MusicFormat:
    """音质格式"""
    name: str       # "FLAC", "320K", "128K"
    bitrate: int    # kbps, e.g. 2000, 320, 128
    type: str       # "flac", "mp3", "wav"
    url: Optional[str] = None  # 下载 URL（可能为空，需实时获取）


@dataclass
class SearchResult:
    """统一搜索结果"""
    id: str                 # 唯一标识，格式: "kuwo_12345" / "netease_12345"
    title: str
    artist: str
    album: str
    cover: Optional[str] = None
    source: str = "kuwo"    # "kuwo" | "netease"
    duration: int = 0       # 秒
    formats: list[MusicFormat] = field(default_factory=list)
    artist_id: str = ""      # 歌手 ID
    album_id: str = ""       # 专辑 ID

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "cover": self.cover,
            "source": self.source,
            "duration": self.duration,
            "artist_id": self.artist_id,
            "album_id": self.album_id,
            "formats": [
                {"name": f.name, "bitrate": f.bitrate, "type": f.type, "url": f.url}
                for f in self.formats
            ],
        }


class SearchProvider(ABC):
    """音源搜索提供者抽象基类

    各音源（酷我/网易云）实现此接口，统一被 main.py 调用，
    便于后续扩展新音源（如 QQ 音乐）只需新增一个 Provider 子类。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """音源名称，如 kuwo / netease"""

    @abstractmethod
    async def search(self, keyword: str, limit: int = 10,
                     search_type: str = "music",
                     skip_formats: bool = False,
                     cookie: str = "") -> list[SearchResult]:
        """关键词搜索

        Args:
            keyword: 搜索关键词
            limit: 返回结果数量上限
            search_type: "music"(歌曲) | "artist"(歌手) | "album"(专辑)
            skip_formats: True 时跳过格式信息获取（加速搜索）
            cookie: 部分音源需要（如网易云）
        """

    @abstractmethod
    async def get_artist_detail(self, artist_id: str) -> dict:
        """获取歌手详情（基本信息 + 热门歌曲 + 专辑列表）"""

    @abstractmethod
    async def get_album_detail(self, album_id: str) -> dict:
        """获取专辑详情（基本信息 + 曲目列表）"""
