"""搜索接口抽象层 - 统一结果结构"""

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
                {"name": f.name, "bitrate": f.bitrate, "type": f.type}
                for f in self.formats
            ],
        }
