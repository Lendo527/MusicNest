"""音乐搜索模块 - Provider 注册中心

统一入口：
    from app.search import get_provider, get_all_providers

    provider = get_provider("kuwo")
    results = await provider.search("周杰伦")
"""

from typing import Optional

from app.search.base import SearchProvider, SearchResult, MusicFormat

_PROVIDERS: dict[str, SearchProvider] = {}


def register_provider(provider: SearchProvider) -> None:
    """注册一个搜索提供者"""
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> Optional[SearchProvider]:
    """按名称获取搜索提供者"""
    return _PROVIDERS.get(name)


def get_all_providers() -> dict[str, SearchProvider]:
    """获取所有已注册的提供者"""
    return dict(_PROVIDERS)


def _init_default_providers() -> None:
    """懒加载注册默认 Provider（避免循环导入）"""
    if _PROVIDERS:
        return
    try:
        from app.search.kuwo import KuwoProvider
        register_provider(KuwoProvider())
    except Exception:
        pass
    try:
        from app.search.netease import NeteaseProvider
        register_provider(NeteaseProvider())
    except Exception:
        pass


# 首次访问时自动初始化
def _ensure_init():
    if not _PROVIDERS:
        _init_default_providers()


def search_all(keyword: str, limit: int = 20, search_type: str = "music",
               skip_formats: bool = False, cookie: str = "",
               sources: Optional[list[str]] = None) -> "list[SearchResult]":
    """跨所有/指定音源并行搜索（async 调用方需 await）

    返回一个 coroutine，结果为合并后的 SearchResult 列表。
    """
    import asyncio
    _ensure_init()

    targets = sources or list(_PROVIDERS.keys())
    coros = []
    for name in targets:
        p = _PROVIDERS.get(name)
        if p is None:
            continue
        coros.append(p.search(keyword, limit=limit, search_type=search_type,
                              skip_formats=skip_formats, cookie=cookie))

    async def _gather():
        results = await asyncio.gather(*coros, return_exceptions=True)
        merged: list[SearchResult] = []
        for r in results:
            if isinstance(r, list):
                merged.extend(r)
        return merged

    return _gather()


__all__ = [
    "SearchProvider", "SearchResult", "MusicFormat",
    "register_provider", "get_provider", "get_all_providers",
    "search_all",
]
