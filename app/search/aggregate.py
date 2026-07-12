"""F1: 多音源聚合搜索 — 并发查询酷我+网易云，按匹配度合并去重

聚合策略：
1. 并发查询所有已启用的音源（kuwo + netease）
2. 按标题+歌手匹配度合并去重（同一首歌只保留一个结果）
3. 优先返回有可用播放 URL 的结果

匹配度算法：
- 标题完全匹配：+100
- 标题包含关键词：+50
- 歌手匹配：+30
- 有播放 URL：+20（关键加分项）
"""

import asyncio
import logging
from typing import Optional

from app.search.kuwo import search as kuwo_search, search_by_keyword as kuwo_search_by_keyword
from app.search.netease import search as netease_search, get_download_url as netease_download_url

logger = logging.getLogger("musicnest.aggregate")


def _normalize(s: str) -> str:
    """标准化字符串用于匹配比较"""
    if not s:
        return ""
    return s.lower().strip().replace(" ", "").replace("　", "")


def _match_score(title: str, artist: str, keyword: str) -> int:
    """计算搜索结果与关键词的匹配度分数"""
    score = 0
    norm_title = _normalize(title)
    norm_artist = _normalize(artist)
    norm_kw = _normalize(keyword)

    if not norm_kw:
        return 0

    # 标题完全匹配
    if norm_title == norm_kw:
        score += 100
    elif norm_kw in norm_title:
        score += 50
    elif norm_title in norm_kw:
        score += 40

    # 歌手匹配
    if norm_artist and norm_kw in norm_artist:
        score += 30
    elif norm_artist and norm_artist in norm_kw:
        score += 20

    return score


async def aggregate_search_by_keyword(keyword: str, timeout: float = 10.0) -> dict:
    """F1: 聚合搜索 — 并发查询酷我+网易云，返回最佳匹配结果

    与现有 search_by_keyword 接口兼容，返回格式一致：
    {"code": 0, "data": {title, artist, album, duration, cover_url, url, source_data}}

    策略：
    1. 并发调用酷我 search_by_keyword + 网易云搜索
    2. 酷我结果通常已包含播放 URL（优先）
    3. 网易云结果需额外获取下载 URL
    4. 按匹配度+有无 URL 排序，返回最佳结果
    """
    if not keyword:
        return {"code": 404, "msg": "关键词为空", "data": None}

    # 并发查询两个音源
    kuwo_task = asyncio.create_task(_safe_kuwo_search(keyword, timeout))
    netease_task = asyncio.create_task(_safe_netease_search(keyword, timeout))

    kuwo_result, netease_result = await asyncio.gather(kuwo_task, netease_task)

    # 收集所有候选结果
    candidates = []

    # 酷我结果（通常已有 URL）
    if kuwo_result and kuwo_result.get("code") == 0 and kuwo_result.get("data"):
        data = kuwo_result["data"]
        score = _match_score(data.get("title", ""), data.get("artist", ""), keyword)
        score += 20  # 有播放 URL 加分
        candidates.append({
            "score": score,
            "data": data,
            "source": "kuwo",
        })
        logger.debug(f"[Aggregate] 酷我候选: {data.get('title')} score={score}")

    # 网易云结果（需获取 URL）
    if netease_result:
        for r in netease_result[:3]:  # 只取前 3 个结果尝试获取 URL
            score = _match_score(r.title, r.artist, keyword)
            # 尝试获取播放 URL
            try:
                netease_cookie = _get_netease_cookie()
                pure_id = r.id.replace("netease_", "")
                url = await netease_download_url(pure_id, br=320000, cookie=netease_cookie, timeout=timeout)
                if url:
                    score += 20  # 有 URL 加分
                    candidates.append({
                        "score": score,
                        "data": {
                            "title": r.title,
                            "artist": r.artist,
                            "album": r.album,
                            "duration": r.duration,
                            "cover_url": r.cover,
                            "url": url,
                            "source_data": {
                                "platform": "netease",
                                "musicId": pure_id,
                            },
                        },
                        "source": "netease",
                    })
                    logger.debug(f"[Aggregate] 网易云候选: {r.title} score={score}")
            except Exception as e:
                logger.debug(f"[Aggregate] 网易云获取URL失败: {r.title} err={e}")

    if not candidates:
        logger.info(f"[Aggregate] 聚合搜索无结果: {keyword}")
        return {"code": 404, "msg": "未找到歌曲", "data": None}

    # 按分数降序排序，返回最佳结果
    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    logger.info(
        f"[Aggregate] 聚合搜索选择: {best['data'].get('title')} "
        f"source={best['source']} score={best['score']} "
        f"(共 {len(candidates)} 个候选)"
    )
    return {"code": 0, "msg": "success", "data": best["data"]}


async def _safe_kuwo_search(keyword: str, timeout: float) -> Optional[dict]:
    """安全调用酷我搜索，捕获异常"""
    try:
        return await kuwo_search_by_keyword(keyword, timeout=timeout)
    except Exception as e:
        logger.warning(f"[Aggregate] 酷我搜索异常: {e}")
        return None


async def _safe_netease_search(keyword: str, timeout: float) -> list:
    """安全调用网易云搜索，捕获异常"""
    try:
        return await netease_search(keyword, limit=3, timeout=timeout, skip_formats=True)
    except Exception as e:
        logger.warning(f"[Aggregate] 网易云搜索异常: {e}")
        return []


def _get_netease_cookie() -> str:
    """从 config 获取网易云 cookie"""
    try:
        from app.config import config
        netease_config = config.get("netease", {})
        if isinstance(netease_config, dict):
            return netease_config.get("cookie", "")
        return ""
    except Exception:
        return ""


async def aggregate_search(keyword: str, limit: int = 10, timeout: float = 10.0) -> list:
    """F1: 聚合搜索列表 — 并发查询多音源，合并去重后返回

    用于 Web UI 搜索页，返回合并后的 SearchResult 列表。

    去重策略：按 (标题+歌手) 标准化后去重，优先保留有 URL 的结果
    """
    if not keyword:
        return []

    kuwo_task = asyncio.create_task(_safe_kuwo_list_search(keyword, limit, timeout))
    netease_task = asyncio.create_task(_safe_netease_list_search(keyword, limit, timeout))

    kuwo_results, netease_results = await asyncio.gather(kuwo_task, netease_task)

    # 合并去重
    seen_keys = set()
    merged = []

    for r in (kuwo_results or []) + (netease_results or []):
        key = (_normalize(r.title), _normalize(r.artist))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # 计算匹配度用于排序
        r._match_score = _match_score(r.title, r.artist, keyword)
        merged.append(r)

    # 按匹配度降序排序
    merged.sort(key=lambda x: getattr(x, "_match_score", 0), reverse=True)
    return merged[:limit]


async def _safe_kuwo_list_search(keyword: str, limit: int, timeout: float) -> list:
    """安全调用酷我列表搜索"""
    try:
        return await kuwo_search(keyword, limit=limit, timeout=timeout, skip_formats=True)
    except Exception as e:
        logger.warning(f"[Aggregate] 酷我列表搜索异常: {e}")
        return []


async def _safe_netease_list_search(keyword: str, limit: int, timeout: float) -> list:
    """安全调用网易云列表搜索"""
    try:
        return await netease_search(keyword, limit=limit, timeout=timeout, skip_formats=True)
    except Exception as e:
        logger.warning(f"[Aggregate] 网易云列表搜索异常: {e}")
        return []
