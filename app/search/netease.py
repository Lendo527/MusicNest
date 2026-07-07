"""网易云音乐搜索 - 通过第三方网关 POST JSON 模式（sqmusic 风格）"""

import logging
import re
import time
from typing import Optional, List

import httpx

from app.search.base import SearchResult, MusicFormat, SearchProvider

logger = logging.getLogger("musicnest.netease")

# 第三方网关列表（抄 sqmusic application-netease.yml + 本地 fallback）
NETEASE_API_BASE_URLS = [
    "https://www.musicapi.cn",
    "http://45.152.64.114:3005",
    "https://music-api.heheda.top",
    "https://apis.netstart.cn/music",
    "http://dg-t.cn:3000",
    "https://163api.qijieya.cn",
    "https://zm.armoe.cn",
    "https://wyy.xhily.com",
    "http://plugin.changsheng.space:3000",
    # 本地自建 fallback
    "http://localhost:3000",
]


async def _netease_request(
    endpoint: str,
    params: dict = None,
    cookie: str = "",
    timeout: float = 10.0,
    base_url_override: str = None,
) -> dict:
    """
    向第三方网关发 POST JSON 请求，遍历 base_urls 直到成功。
    如果指定 base_url_override，则只尝试该网关。
    所有网关都失败时返回 {"code": -1}。
    """
    if params is None:
        params = {}

    headers = {
        "Content-Type": "application/json; charset=utf-8",
    }
    if cookie:
        headers["Cookie"] = cookie

    urls_to_try = [base_url_override] if base_url_override else NETEASE_API_BASE_URLS

    last_error = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for base_url in urls_to_try:
            url = f"{base_url}{endpoint}"
            # 加时间戳到 URL 防网关缓存（Post JSON body 不会影响缓存 key）
            url += ('?' if '?' not in endpoint else '&') + '_t=' + str(int(time.time() * 1000))
            try:
                resp = await client.post(url, json=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                # 成功返回
                return data
            except httpx.TimeoutException as e:
                last_error = f"timeout: {e}"
                logger.warning(f"[Netease] {url} 超时，尝试下一个网关")
            except httpx.RequestError as e:
                last_error = f"request: {e}"
                logger.warning(f"[Netease] {url} 请求失败: {e}，尝试下一个网关")
            except Exception as e:
                last_error = f"exception: {e}"
                logger.warning(f"[Netease] {url} 异常: {e}，尝试下一个网关")

    logger.error(f"[Netease] 所有网关均失败: {last_error}")
    return {"code": -1, "msg": f"所有网关失败: {last_error}"}


def _build_quality_formats(song: dict) -> List[MusicFormat]:
    """
    根据歌曲的音质字段（l/m/h/sq/hr）构建 MusicFormat 列表。
    
    sqmusic 音质映射：
    - hr  → Hi-Res FLAC (3000k)
    - sq  → FLAC 无损 (2000k)
    - h   → MP3 320K
    - m   → MP3 192K
    - l   → MP3 128K
    """
    formats = []

    l = song.get("l")
    if isinstance(l, dict) and l.get("size", 0) > 0:
        formats.append(MusicFormat(name="128K", bitrate=128, type="mp3"))

    m = song.get("m")
    if isinstance(m, dict) and m.get("size", 0) > 0:
        formats.append(MusicFormat(name="192K", bitrate=192, type="mp3"))

    h = song.get("h")
    if isinstance(h, dict) and h.get("size", 0) > 0:
        formats.append(MusicFormat(name="320K", bitrate=320, type="mp3"))

    sq = song.get("sq")
    if isinstance(sq, dict) and sq.get("size", 0) > 0:
        formats.append(MusicFormat(name="FLAC", bitrate=2000, type="flac"))

    hr = song.get("hr")
    if isinstance(hr, dict) and hr.get("size", 0) > 0:
        formats.append(MusicFormat(name="Hi-Res FLAC", bitrate=3000, type="flac"))

    # 全空时返回空列表，不强行兜底 128K（避免掩盖真实问题）

    # 按 bitrate 降序排列（高音质在前）
    formats.sort(key=lambda f: f.bitrate, reverse=True)

    return formats


def _parse_song(song: dict, source: str = "netease") -> Optional[SearchResult]:
    """将网关返回的 song 字典转为 SearchResult"""
    song_id = song.get("id")
    if song_id is None:
        return None
    song_id = str(song_id)

    song_name = (song.get("name") or "").strip()
    if not song_name:
        song_name = "未知歌曲"

    # 歌手
    ar = song.get("ar")
    if ar and isinstance(ar, list):
        artists = [a.get("name", "") for a in ar if isinstance(a, dict) and a.get("name")]
        artist_name = "/".join(artists) if artists else "未知歌手"
        # 提取第一个同时有 name 和 id 的歌手 ID（避免越界风险）
        artist_id = ""
        for a in ar:
            if isinstance(a, dict) and a.get("name") and a.get("id"):
                artist_id = str(a.get("id"))
                break
    else:
        artist_name = "未知歌手"
        artist_id = ""

    # 专辑 & 封面
    al = song.get("al") or {}
    album_name = al.get("name") or "未知专辑"
    cover_url = al.get("picUrl") or None
    album_id = str(al.get("id", "")) if al.get("id") else ""

    # 时长: dt 是毫秒，转秒
    dt = song.get("dt", 0)
    try:
        if dt:
            duration = int(dt) // 1000
        else:
            # fallback: 兼容 duration 字段（也是毫秒）
            duration = int(song.get("duration", 0)) // 1000
    except (TypeError, ValueError):
        duration = 0

    formats = _build_quality_formats(song)

    return SearchResult(
        id=f"{source}_{song_id}",
        title=song_name,
        artist=artist_name,
        album=album_name,
        cover=cover_url,
        source=source,
        duration=duration,
        formats=formats,
        artist_id=artist_id,
        album_id=album_id,
    )


async def search(
    keyword: str,
    limit: int = 10,
    search_type: str = "music",
    timeout: float = 10.0,
    cookie: str = "",
    skip_formats: bool = False,
) -> List[SearchResult]:
    """
    网易云音乐搜索

    Args:
        keyword: 搜索关键词
        limit: 返回结果数量
        search_type: 搜索类型 "music"(歌曲) | "artist"(歌手) | "album"(专辑)
        cookie: 网易云 Cookie
        timeout: 超时秒数

    Returns:
        List[SearchResult]
    """
    if not keyword:
        return []

    # 映射 search_type 到 type 参数
    type_map = {"music": 1, "artist": 100, "album": 10}
    stype = type_map.get(search_type, 1)

    # 调一次 _netease_request，由其内部遍历所有网关（避免 N² 复杂度）
    result = await _netease_request(
        "/cloudsearch",
        params={
            "keywords": keyword,
            "limit": limit,
            "type": stype,
            "offset": 0,
            "timestamp": int(time.time() * 1000),
        },
        cookie=cookie,
        timeout=timeout,
    )

    if result.get("code") != 200:
        logger.warning("[Netease] 搜索 '%s' 失败: %s", keyword, result.get("msg", "未知错误"))
        return []

    res_data = result.get("result", {})
    songs = res_data.get("songs", []) if search_type == "music" else []
    artists = res_data.get("artists", []) if search_type == "artist" else []
    albums = res_data.get("albums", []) if search_type == "album" else []

    # 各类型的结果列表
    type_results = {"music": songs, "artist": artists, "album": albums}
    current_results = type_results.get(search_type, [])

    if not current_results:
        logger.debug("[Netease] 搜索 '%s' 返回 0 条 %s 结果", keyword, search_type)
        return []

    logger.info("[Netease] 搜索 '%s' 返回 %d 条, 首条: %s",
                 keyword, len(current_results),
                 current_results[0].get("name", "?") if current_results else "(无)")

    # 根据搜索类型解析不同字段
    if search_type == "artist":
        artists = res_data.get("artists", [])
        results = []
        for art in artists:
            art_id = str(art.get("id", ""))
            art_name = art.get("name", "").strip()
            pic_url = art.get("picUrl") or art.get("img1v1Url") or None
            if art_id:
                results.append(SearchResult(
                    id=f"netease_{art_id}",
                    title=art_name or "未知歌手",
                    artist=art_name or "未知歌手",
                    album="",
                    cover=pic_url,
                    source="netease",
                    duration=0,
                    formats=[],
                    artist_id=art_id,
                    album_id="",
                ))
        return results

    if search_type == "album":
        albums = res_data.get("albums", [])
        results = []
        for alb in albums:
            alb_id = str(alb.get("id", ""))
            alb_name = alb.get("name", "").strip()
            artist_obj = alb.get("artist") or alb.get("ar") or {}
            artist_name = (artist_obj.get("name") or "").strip()
            artist_id_val = str(artist_obj.get("id", "")) if artist_obj.get("id") else ""
            pic_url = alb.get("picUrl") or None
            if alb_id:
                results.append(SearchResult(
                    id=f"netease_{alb_id}",
                    title=alb_name or "未知专辑",
                    artist=artist_name or "未知歌手",
                    album=alb_name or "未知专辑",
                    cover=pic_url,
                    source="netease",
                    duration=0,
                    formats=[],
                    artist_id=artist_id_val,
                    album_id=alb_id,
                ))
        return results

    # 默认 music 类型
    songs = res_data.get("songs", [])
    if not songs:
        return []

    results = []
    for song in songs:
        parsed = _parse_song(song, source="netease")
        if parsed:
            # 搜索时跳过音质加速返回，下载时保留
            if skip_formats:
                parsed.formats = []
            results.append(parsed)

    return results


async def get_download_url(
    song_id: str,
    br: int = 999000,
    cookie: str = "",
    timeout: float = 10.0,
) -> Optional[str]:
    """
    获取网易云歌曲下载 URL

    Args:
        song_id: 歌曲 ID（数字，不带 netease_ 前缀）
        br: 比特率（如 320000, 128000），>=999000 则请求 FLAC
        cookie: 网易云 Cookie

    Returns:
        下载 URL 或 None
    """
    params = {"id": song_id}
    if br >= 999000:
        params["type"] = "flac"
    else:
        params["br"] = br

    # 先尝试 /song/download/url，失败再尝试 /song/url（不同网关端点不同）
    result = await _netease_request(
        "/song/download/url",
        params=params,
        cookie=cookie,
        timeout=timeout,
    )

    # 如果 /song/download/url 全部网关失败，尝试 /song/url
    if result.get("code") != 200:
        logger.debug("[Netease] /song/download/url 失败，尝试 /song/url 端点")
        fallback_params = {"id": song_id}
        if br >= 999000:
            fallback_params["type"] = "flac"
        else:
            fallback_params["br"] = br
        result = await _netease_request(
            "/song/url",
            params=fallback_params,
            cookie=cookie,
            timeout=timeout,
        )

    if result.get("code") != 200:
        logger.warning(f"[Netease] 下载链接获取失败: {result.get('msg', '未知错误')}")
        return None

    data = result.get("data")
    if not data:
        return None

    # data 可能是 dict 或 list
    if isinstance(data, dict):
        return data.get("url")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("url")

    return None


async def get_playlist_detail(
    playlist_id: str,
    cookie: str = "",
    timeout: float = 10.0,
) -> dict:
    """获取歌单元数据（用于增量同步锚点）

    Returns:
        {"id": int, "name": str, "update_time": int, "track_update_time": int,
         "track_count": int, "play_count": int}
    """
    result = await _netease_request(
        "/playlist/detail",
        params={"id": playlist_id},
        cookie=cookie,
        timeout=timeout,
    )
    if result.get("code") != 200:
        return {"id": 0, "name": "", "update_time": 0, "track_update_time": 0,
                "track_count": 0, "play_count": 0}
    pl = result.get("playlist", {}) or {}
    return {
        "id": pl.get("id", 0),
        "name": pl.get("name", ""),
        "update_time": pl.get("updateTime", 0),
        "track_update_time": pl.get("trackUpdateTime", 0),
        "track_count": pl.get("trackCount", 0),
        "play_count": pl.get("playCount", 0),
    }


async def get_playlist_tracks(
    playlist_id: str,
    cookie: str = "",
    timeout: float = 10.0,
) -> List[SearchResult]:
    """
    获取歌单所有歌曲

    Args:
        playlist_id: 歌单 ID
        cookie: 网易云 Cookie

    Returns:
        List[SearchResult]
    """
    result = await _netease_request(
        "/playlist/track/all",
        params={
            "id": playlist_id,
            "limit": 1000,
            "offset": 0,
        },
        cookie=cookie,
        timeout=timeout,
    )

    if result.get("code") != 200:
        logger.warning(f"[Netease] 歌单获取失败: {result.get('msg', '未知错误')}")
        return []

    songs = result.get("songs", [])
    if not songs:
        return []

    results = []
    for song in songs:
        parsed = _parse_song(song, source="netease")
        if parsed:
            results.append(parsed)

    return results


async def get_song_detail(
    song_id: str,
    cookie: str = "",
    timeout: float = 10.0,
) -> Optional[SearchResult]:
    """
    获取歌曲详情（含完整音质信息）

    Args:
        song_id: 歌曲 ID（数字，不带 netease_ 前缀）
        cookie: 网易云 Cookie

    Returns:
        SearchResult 或 None
    """
    result = await _netease_request(
        "/song/detail",
        params={"ids": song_id},
        cookie=cookie,
        timeout=timeout,
    )

    if result.get("code") != 200:
        logger.warning(f"[Netease] 歌曲详情获取失败: {result.get('msg', '未知错误')}")
        return None

    songs = result.get("songs", [])
    if not songs:
        return None

    return _parse_song(songs[0], source="netease")


async def get_lyrics(song_id: str, cookie: str = "", timeout: float = 10.0) -> str:
    """获取网易云歌词（原文 + 翻译合并为双语 LRC）

    合并策略：按时间戳对齐，翻译追加到原文同一行后。
    若无翻译，返回纯原文歌词。

    Returns:
        LRC 格式歌词字符串，失败返回空字符串
    """
    try:
        result = await _netease_request(
            "/lyric",
            params={"id": song_id},
            cookie=cookie,
            timeout=timeout,
        )
        if result.get("code") != 200:
            return ""

        lrc_obj = result.get("lrc", {}) or {}
        orig_lrc = lrc_obj.get("lyric", "") or ""
        if not orig_lrc:
            return ""

        tlyric_obj = result.get("tlyric", {}) or {}
        trans_lrc = tlyric_obj.get("lyric", "") or ""

        if not trans_lrc:
            return orig_lrc

        # 解析翻译：[mm:ss.xx]text → {timestamp: text}
        # 注意：网易云翻译时间戳可能比原文稍有偏差，按整秒对齐
        def _parse(lrc: str) -> dict:
            parsed = {}
            for line in lrc.split("\n"):
                # 匹配 [mm:ss.xx] 或 [mm:ss]
                m = re.match(r"\[(\d+):(\d+)(?:\.\d+)?\](.*)", line.strip())
                if m:
                    minute, sec, txt = int(m.group(1)), int(m.group(2)), m.group(3).strip()
                    ts = minute * 60 + sec  # 整秒对齐
                    parsed[ts] = txt
            return parsed

        orig_map = _parse(orig_lrc)
        trans_map = _parse(trans_lrc)
        if not trans_map:
            return orig_lrc

        # 合并：原文每行后追加翻译（若同一整秒有翻译）
        # 保留原文行的原始时间戳格式 [mm:ss.xx]
        merged_lines = []
        for line in orig_lrc.split("\n"):
            m = re.match(r"(\[\d+:\d+(?:\.\d+)?\])(.*)", line.strip())
            if m:
                ts_prefix = m.group(1)  # 如 [01:23.45]
                rest = m.group(2).strip()
                # 从时间戳提取整秒用于查翻译
                ts_match = re.match(r"\[(\d+):(\d+)", ts_prefix)
                if ts_match:
                    ts = int(ts_match.group(1)) * 60 + int(ts_match.group(2))
                    trans_txt = trans_map.get(ts, "")
                    if trans_txt and trans_txt != rest:
                        merged_lines.append(f"{ts_prefix}{rest} ({trans_txt})")
                        continue
            merged_lines.append(line)

        return "\n".join(merged_lines)
    except Exception as e:
        logger.warning(f"[Netease] 获取歌词失败: {e}")
        return ""


async def verify_cookie(cookie: str, timeout: float = 10.0) -> bool:
    """
    验证网易云 Cookie 是否有效

    逐个网关尝试多种验证端点，记录详细日志。

    Args:
        cookie: 网易云 Cookie（如 MUSIC_U=xxx）
        timeout: 超时秒数

    Returns:
        True 如果 Cookie 有效
    """
    if not cookie:
        return False

    # 验证端点列表：优先用带 cookie 校验的端点
    verify_endpoints = [
        ("/user/account", "account"),
        ("/login/status", "account"),
        ("/user/detail", "profile"),
    ]

    # 阶段 1：逐端点 + 逐网关验证
    for endpoint, key_path in verify_endpoints:
        for base_url in NETEASE_API_BASE_URLS:
            # 搜索需要传 params，其他端点不需要
            params = {"uid": 1} if endpoint == "/user/detail" else {}
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    url = f"{base_url}{endpoint}"
                    headers = {"Content-Type": "application/json; charset=utf-8", "Cookie": cookie}
                    resp = await client.post(url, json=params, headers=headers)
                    data = resp.json()
                    if data.get("code") == 200:
                        # 检查关键字段
                        account = data.get("account") or data.get("data", {}).get("account")
                        profile = data.get("profile") or data.get("data", {}).get("profile")
                        if account or profile:
                            nickname = (profile or {}).get("nickname") or (account or {}).get("userName", "未知")
                            logger.info("[Netease] Cookie 有效 (via %s %s), 用户: %s",
                                        base_url, endpoint, nickname)
                            return True
                        # 有结果但字段名不同，打印提示
                        logger.debug("[Netease] %s %s 返回 200 但无 account/profile: keys=%s",
                                     base_url, endpoint, list(data.keys()))
                    else:
                        logger.debug("[Netease] %s %s 返回 code=%s",
                                     base_url, endpoint, data.get("code"))
            except Exception as e:
                logger.debug("[Netease] %s %s 失败: %s", base_url, endpoint, e)

    # 只保留阶段 1（/user/account 等端点验证）；
    # 搜索/下载验证已删除：部分网关搜索不校验 cookie，会误判无效 Cookie 为有效

    logger.warning("[Netease] 所有验证方式均失败，Cookie 无效或网关不可达")
    return False




async def get_artist_detail(
    artist_id: str,
    cookie: str = "",
    timeout: float = 10.0,
) -> dict:
    """
    获取歌手详情：基本信息 + 热门歌曲

    Args:
        artist_id: 歌手 ID
        cookie: 网易云 Cookie
        timeout: 超时秒数

    Returns:
        dict: {
            "name": str,
            "image": str,
            "top_songs": [SearchResult],
            "albums": [{"id": str, "name": str, "cover": str, "year": str}]
        }
    """
    result = {
        "name": "",
        "image": "",
        "top_songs": [],
        "albums": [],
    }

    if not artist_id:
        return result

    try:
        # 1. 获取歌手热门歌曲（top 50）
        songs_result = await _netease_request(
            "/artist/top/song",
            params={"id": artist_id},
            cookie=cookie,
            timeout=timeout,
        )

        if songs_result.get("code") == 200:
            songs = songs_result.get("songs", [])
            for song in songs[:20]:
                parsed = _parse_song(song, source="netease")
                if parsed:
                    result["top_songs"].append(parsed)
            if songs:
                # 从第一首歌的歌手信息中获取歌手名
                first_song = songs[0]
                ar = first_song.get("ar", [])
                if ar:
                    result["name"] = (ar[0].get("name") or "").strip()

        # 2. 获取歌手详情（含头像）
        detail_result = await _netease_request(
            "/artist/detail",
            params={"id": artist_id},
            cookie=cookie,
            timeout=timeout,
        )

        if detail_result.get("code") == 200:
            artist_data = (detail_result.get("data") or {}).get("artist", {})
            if artist_data:
                if not result["name"]:
                    result["name"] = (artist_data.get("name") or "").strip()
                result["image"] = artist_data.get("picUrl") or artist_data.get("cover") or ""

        # 3. 获取歌手专辑列表
        albums_result = await _netease_request(
            "/artist/album",
            params={"id": artist_id, "limit": 50},
            cookie=cookie,
            timeout=timeout,
        )

        if albums_result.get("code") == 200:
            albums_data = albums_result.get("hotAlbums") or albums_result.get("albums") or []
            for alb in albums_data[:20]:
                alb_id = str(alb.get("id", ""))
                alb_name = (alb.get("name") or "").strip()
                alb_cover = alb.get("picUrl") or ""
                # 网易云返回 publishTime 是毫秒时间戳
                publish_time = alb.get("publishTime", 0)
                alb_year = ""
                if publish_time:
                    import datetime
                    try:
                        alb_year = str(datetime.datetime.fromtimestamp(publish_time / 1000).year)
                    except Exception:
                        pass

                if alb_id:
                    result["albums"].append({
                        "id": alb_id,
                        "name": alb_name or "未知专辑",
                        "cover": alb_cover,
                        "year": alb_year,
                    })

        logger.debug(
            "[Netease] 歌手详情: name=%s songs=%d albums=%d",
            result["name"], len(result["top_songs"]), len(result["albums"])
        )

    except Exception as e:
        logger.error(f"[Netease] 获取歌手详情失败: {e}")

    return result


async def get_album_detail(
    album_id: str,
    cookie: str = "",
    timeout: float = 10.0,
) -> dict:
    """
    获取专辑详情：基本信息 + 曲目列表（含音质）

    Args:
        album_id: 专辑 ID
        cookie: 网易云 Cookie
        timeout: 超时秒数

    Returns:
        dict: {
            "name": str,
            "cover": str,
            "artist": str,
            "tracks": [SearchResult]
        }
    """
    result = {
        "name": "",
        "cover": "",
        "artist": "",
        "tracks": [],
    }

    if not album_id:
        return result

    try:
        detail_result = await _netease_request(
            "/album",
            params={"id": album_id},
            cookie=cookie,
            timeout=timeout,
        )

        if detail_result.get("code") != 200:
            logger.warning(f"[Netease] 专辑详情获取失败: {detail_result.get('msg', '未知错误')}")
            return result

        album_data = detail_result.get("album", {})
        if album_data:
            result["name"] = (album_data.get("name") or "").strip()
            result["cover"] = album_data.get("picUrl") or ""

            # 歌手信息
            ar_obj = album_data.get("artist") or album_data.get("ar") or {}
            if isinstance(ar_obj, dict):
                result["artist"] = (ar_obj.get("name") or "").strip()
            elif isinstance(ar_obj, list) and ar_obj:
                result["artist"] = (ar_obj[0].get("name") or "").strip()

            # 曲目列表
            songs = album_data.get("songs", [])
            for song in songs:
                # 补全缺失的专辑/封面信息
                if not song.get("al"):
                    song["al"] = {
                        "id": album_id,
                        "name": result["name"],
                        "picUrl": result["cover"],
                    }
                parsed = _parse_song(song, source="netease")
                if parsed:
                    result["tracks"].append(parsed)

            logger.debug(
                "[Netease] 专辑详情: name=%s tracks=%d",
                result["name"], len(result["tracks"])
            )

    except Exception as e:
        logger.error(f"[Netease] 获取专辑详情失败: {e}")

    return result


class NeteaseProvider(SearchProvider):
    """网易云音乐 SearchProvider 实现"""

    @property
    def name(self) -> str:
        return "netease"

    async def search(self, keyword: str, limit: int = 10,
                     search_type: str = "music",
                     skip_formats: bool = False,
                     cookie: str = "") -> list[SearchResult]:
        return await search(keyword, limit=limit, search_type=search_type,
                           cookie=cookie, skip_formats=skip_formats)

    async def get_artist_detail(self, artist_id: str) -> dict:
        cookie = ""
        # 从 config 取 cookie（避免 main.py 传入）
        try:
            from app.config import config
            cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")
        except Exception:
            pass
        return await get_artist_detail(artist_id, cookie=cookie)

    async def get_album_detail(self, album_id: str) -> dict:
        cookie = ""
        try:
            from app.config import config
            cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")
        except Exception:
            pass
        return await get_album_detail(album_id, cookie=cookie)
