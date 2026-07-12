"""F12: 音源插件模板 — 复制此文件创建新音源插件

实现步骤：
1. 将类名 TemplateProvider 改为 XxxProvider
2. name 属性返回音源标识（如 "qqmusic"）
3. 实现 search / get_artist_detail / get_album_detail 方法
4. 重命名文件为 <音源名>.py
"""

from typing import Optional
from app.search.base import SearchProvider, SearchResult, MusicFormat


class TemplateProvider(SearchProvider):
    """模板音源 Provider — 仅供参考，不实际加载"""

    @property
    def name(self) -> str:
        return "template"

    async def search(self, keyword: str, limit: int = 10,
                     search_type: str = "music",
                     skip_formats: bool = False,
                     cookie: str = "") -> list[SearchResult]:
        """搜索歌曲

        Args:
            keyword: 搜索关键词
            limit: 返回结果数量上限
            search_type: "music" | "artist" | "album"
            skip_formats: True 时跳过格式信息获取
            cookie: 部分音源需要

        Returns:
            SearchResult 列表
        """
        # TODO: 实现实际搜索逻辑
        return []

    async def get_artist_detail(self, artist_id: str) -> dict:
        """获取歌手详情"""
        # TODO: 实现
        return {}

    async def get_album_detail(self, album_id: str) -> dict:
        """获取专辑详情"""
        # TODO: 实现
        return {}
