"""酷我音乐搜索 - 增强版：统一格式 + 多音质"""

import asyncio
import re
import json
import logging
from typing import Optional, List
from urllib.parse import quote

import httpx

from app.search.base import SearchResult, MusicFormat, SearchProvider

logger = logging.getLogger("musicnest.kuwo")

# 模块级共享 httpx 客户端，复用连接池
_shared_client: Optional[httpx.AsyncClient] = None


async def _get_client(timeout: float = 10.0) -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
                     "Referer": "http://www.kuwo.cn/"}
        )
    return _shared_client


async def close_client():
    """应用关闭时调用，关闭共享 httpx 客户端"""
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None


KUWO_SEARCH_URL = "http://search.kuwo.cn/r.s"
KUWO_MOBI_URL = "https://mobi.kuwo.cn/mobi.s"

# PC 端公共参数（参考 sqmusic 配置）——移动端 artistinfo+ft API 返回空，必须用 PC 端
_KUWO_PC_PARAMS = (
    "plat=pc&thost=search.kuwo.cn&vipver=MUSIC_9.1.1.2_BCS2"
    "&devid=38668888&newver=1&pcjson=1&alflac=1&show_copyright_off=1&pcmp4=1"
)

# 音质定义: (name, br参数, bitrate, type)
FORMAT_DEFS = [
    ("FLAC", "2000kflac", 2000, "flac"),
    ("APE", "1000kape", 1000, "ape"),
    ("320K", "320kmp3", 320, "mp3"),
    ("192K", "192kmp3", 192, "mp3"),
    ("128K", "128kmp3", 128, "mp3"),
]


def _python_to_json(raw_text: str) -> str:
    """将酷我返回的 Python 字面量转为 JSON"""
    import ast
    s = raw_text.strip()
    # 移除 BOM
    if s and ord(s[0]) == 0xFEFF:
        s = s[1:]
    try:
        # 用 ast.literal_eval 安全解析 Python 字面量（含 True/False/None/单引号）
        parsed = ast.literal_eval(s)
        return json.dumps(parsed, ensure_ascii=False)
    except (ValueError, SyntaxError) as e:
        logger.debug("[Kuwo] ast.literal_eval 失败: %s，返回原文", e)
        return s


def _parse_nminfo(nminfo: str) -> list:
    """从酷我搜索结果的 nMinfo 字段解析可用音质列表（sqmusic 方式）"""
    formats = []
    if not nminfo:
        return formats
    try:
        for part in nminfo.split(";"):
            for kv in part.split(","):
                kv_parts = kv.split(":")
                if len(kv_parts) == 2 and kv_parts[0] == "bitrate":
                    br = kv_parts[1]
                    if br == "2000":
                        formats.append(MusicFormat(name="FLAC", bitrate=2000, type="flac"))
                    elif br == "320":
                        formats.append(MusicFormat(name="320K", bitrate=320, type="mp3"))
                    elif br == "128":
                        formats.append(MusicFormat(name="128K", bitrate=128, type="mp3"))
    except Exception:
        pass
    # 去重
    seen = set()
    unique = []
    for f in formats:
        key = (f.name, f.bitrate, f.type)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


async def _get_music_formats(pure_id: str, timeout: float = 10.0) -> List[MusicFormat]:
    """获取歌曲的可用音质列表（通过 mobi.kuwo.cn API）"""
    async def _check_format(name: str, br_value: str, bitrate: int, ftype: str) -> Optional[MusicFormat]:
        url = (
            f"{KUWO_MOBI_URL}?f=web&user=0"
            f"&source=kwplayer_ar_5.0.0.0_B_jiakong_vh.apk"
            f"&type=convert_url_with_sign&rid={pure_id}&br={br_value}"
        )
        logger.debug("[Kuwo] _check_format: name=%s br=%s url=%s", name, br_value, url[:120])
        try:
            client = await _get_client(timeout=timeout)
            resp = await client.get(url, timeout=httpx.Timeout(timeout))
            logger.debug("[Kuwo] format响应: name=%s status=%d", name, resp.status_code)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("code") != 200:
                logger.debug("[Kuwo] format不可用: name=%s code=%s", name, data.get("code"))
                return None
            play_url = data.get("data", {}).get("url", "")
            if play_url and play_url.startswith("http"):
                logger.debug("[Kuwo] format可用: name=%s url=%s", name, play_url[:80])
                return MusicFormat(name=name, bitrate=bitrate, type=ftype, url=play_url)
            logger.debug("[Kuwo] format无URL: name=%s", name)
        except Exception as e:
            logger.debug("[Kuwo] format检查异常: name=%s error=%s", name, e)
        return None

    logger.debug("[Kuwo] 开始获取音质列表: pure_id=%s", pure_id)
    tasks = [_check_format(name, br, bitrate, ftype) for name, br, bitrate, ftype in FORMAT_DEFS]
    results = await asyncio.gather(*tasks)
    formats = [r for r in results if r is not None]
    logger.debug("[Kuwo] 音质获取完成: pure_id=%s formats=%d", pure_id, len(formats))
    return formats


async def search(
    keyword: str,
    limit: int = 10,
    search_type: str = "music",
    timeout: float = 10.0,
    skip_formats: bool = False,
) -> List[SearchResult]:
    """
    酷我音乐搜索（统一格式，批量返回）

    Args:
        keyword: 搜索关键词
        limit: 返回结果数量
        search_type: 搜索类型 "music"(歌曲) | "artist"(歌手) | "album"(专辑)
        timeout: 超时秒数
        skip_formats: 是否跳过音质获取（前端搜索用 True 加速，下载用 False）

    Returns:
        List[SearchResult]
    """
    if not keyword:
        return []

    # 映射 search_type 到酷我 ft 参数
    ft_map = {"music": "music", "artist": "artist", "album": "album"}
    ft = ft_map.get(search_type, "music")

    try:
        client = await _get_client(timeout=timeout)
        search_url = (
            f"{KUWO_SEARCH_URL}?client=kt&encoding=utf8&rformat=json"
            f"&mobi=1&vipver=1&pn=0&rn={limit}&correct=1&all={quote(keyword)}&ft={ft}"
        )
        logger.debug("[Kuwo] 搜索请求: url=%s", search_url)
        resp = await client.get(search_url, timeout=httpx.Timeout(timeout))
        logger.debug("[Kuwo] 搜索响应: status=%d", resp.status_code)
        resp.raise_for_status()
        raw_text = resp.text

        # 优先按标准 JSON 解析（PC 端返回双引号标准 JSON）；
        # 失败再降级用 _python_to_json 处理移动端单引号格式
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            json_text = _python_to_json(raw_text)
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                logger.error(f"[Kuwo] JSON解析失败: {json_text[:200]}")
                return []

        abslist = data.get("abslist")
        # 专辑搜索返回 albumlist 而非 abslist
        if not abslist and search_type == "album":
            abslist = data.get("albumlist")
        if not abslist:
            logger.debug("[Kuwo] 搜索无结果: keyword=%s", keyword)
            return []

        logger.debug("[Kuwo] 搜索结果: type=%s count=%d", ft, len(abslist))
        results = []

        # ===== 歌手搜索 =====
        if search_type == "artist":
            for item in abslist:
                artist_id_raw = item.get("ARTISTID") or item.get("artistid") or ""
                artist_id = str(artist_id_raw).strip() if artist_id_raw else ""
                if not artist_id:
                    continue
                artist_name = (item.get("ARTIST") or item.get("artist") or "").strip()
                pic = item.get("PICPATH") or item.get("picpath") or item.get("pictitle") or ""
                # 补全图片 URL
                if pic and not pic.startswith("http"):
                    pic = f"https://star.kuwo.cn/star/starheads/{pic.lstrip('/')}"
                # 兜底：PICPATH 为空时用 artist_id 构造头像 URL
                if not pic:
                    pic = f"https://star.kuwo.cn/star/starheads/180/{artist_id}.jpg"
                results.append(SearchResult(
                    id=f"kuwo_artist_{artist_id}",
                    title=artist_name or "未知歌手",
                    artist=artist_name or "未知歌手",
                    album="",
                    cover=pic or None,
                    source="kuwo",
                    duration=0,
                    formats=[],
                    artist_id=artist_id,
                    album_id="",
                ))
            return results

        # ===== 专辑搜索 =====
        if search_type == "album":
            for item in abslist:
                # 尝试多种字段名（移动端/PC端字段名可能不同）
                album_id_raw = (item.get("ALBUMID") or item.get("albumid") or
                               item.get("ALBUMID_STR") or item.get("id") or "")
                album_id = str(album_id_raw).strip() if album_id_raw else ""
                if not album_id:
                    continue
                album_name = (item.get("NAME") or item.get("ALBUM") or
                             item.get("name") or item.get("album") or "").strip()
                artist_name = (item.get("ARTIST") or item.get("artist") or "").strip()
                # 专辑封面
                cover = None
                if album_id != "0":
                    cover = f"https://img3.kuwo.cn/star/albumcover/500/0/0/{album_id}.jpg"
                results.append(SearchResult(
                    id=f"kuwo_album_{album_id}",
                    title=album_name or "未知专辑",
                    artist=artist_name or "未知歌手",
                    album=album_name or "未知专辑",
                    cover=cover,
                    source="kuwo",
                    duration=0,
                    formats=[],
                    artist_id="",
                    album_id=album_id,
                ))
            return results

        # ===== 歌曲搜索 =====
        async def _fetch_song(song: dict) -> Optional[SearchResult]:
            song_id_raw = song.get("MUSICRID", "")
            pure_id: Optional[str] = None
            if song_id_raw:
                match = re.search(r"\d+", str(song_id_raw))
                if match:
                    pure_id = match.group(0)
            if not pure_id:
                return None

            song_name = (song.get("NAME") or "").strip()
            artist_name = (song.get("ARTIST") or "").strip()
            album_name = (song.get("ALBUM") or "").strip()
            try:
                duration = int(song.get("DURATION", 180))
            except (TypeError, ValueError):
                duration = 180

            # 提取 artist_id 和 album_id
            raw_artist_id = song.get("ARTISTID", "") or song.get("artistid", "") or ""
            # 如果 ARTISTID 为空，尝试用 allartistid 的第一个
            if not raw_artist_id:
                all_ids = song.get("allartistid", "") or song.get("ALLARTISTID", "") or ""
                if all_ids:
                    raw_artist_id = all_ids.split("&")[0].strip()
            artist_id = str(raw_artist_id).strip() if raw_artist_id else ""
            raw_album_id = song.get("ALBUMID") or song.get("albumid") or song.get("album_id") or song.get("ALBUMID_STR") or ""
            album_id = str(raw_album_id).strip() if raw_album_id else ""

            logger.debug(
                "[Kuwo] 获取音质: pure_id=%s title=%s artist=%s album_id=%s",
                pure_id, song_name[:30], artist_name[:20], album_id
            )

            # 封面（从搜索 API 的 web_albumpic_short 字段构建）
            cover_url = None
            album_pic = song.get("web_albumpic_short", "")
            if album_pic:
                cover_url = re.sub(r'/120(?=/|$)', '/500', f"https://img3.kuwo.cn/star/albumcover/{album_pic}")
            if not cover_url:
                artist_pic = song.get("web_artistpic_short", "")
                if artist_pic:
                    cover_url = re.sub(r'/120(?=/|$)', '/500', f"https://star.kuwo.cn/star/starheads/{artist_pic}")

            # 音质获取：优先用 nMinfo 字段（sqmusic 方式），无则调 API
            nminfo = song.get("nMinfo", "") or song.get("NMINFO", "") or ""
            if nminfo:
                formats = _parse_nminfo(nminfo)
            elif not skip_formats:
                formats = await _get_music_formats(pure_id, timeout=timeout)
            else:
                formats = []

            return SearchResult(
                id=f"kuwo_{pure_id}",
                title=song_name or "未知歌曲",
                artist=artist_name or "未知歌手",
                album=album_name or "未知专辑",
                cover=cover_url,
                source="kuwo",
                duration=duration,
                formats=formats,
                artist_id=artist_id,
                album_id=album_id,
            )

        song_tasks = [_fetch_song(s) for s in abslist]
        song_results = await asyncio.gather(*song_tasks, return_exceptions=True)
        results = [r for r in song_results if r is not None and not isinstance(r, Exception)]

        return results

    except httpx.RequestError as e:
        logger.error(f"[Kuwo] 搜索请求失败: {e}")
        return []
    except Exception as e:
        logger.error(f"[Kuwo] 搜索异常: {e}")
        return []


async def query_song_by_id(song_id: str, timeout: float = 10.0) -> Optional[SearchResult]:
    """根据歌曲 ID 获取完整信息（含封面、专辑、歌手）——对标 sqmusic querySongById"""
    if not song_id:
        return None
    pure_id = song_id.replace("kuwo_", "").replace("MUSIC_", "")
    try:
        # 用酷我新 H5 接口获取歌曲详情（含专辑、歌手、封面）
        client = await _get_client(timeout=timeout)
        url = f"https://m.kuwo.cn/newh5/singles/songinfoandlrc?musicId={pure_id}&httpsStatus=1"
        resp = await client.get(url, timeout=httpx.Timeout(timeout))
        data = resp.json()
        if data.get("status") != 200:
            logger.warning(f"[Kuwo] query_song_by_id 失败: id={pure_id}, status={data.get('status')}")
            return None
        song_data = data.get("data", {})
        song_name = (song_data.get("songName") or "").strip()
        artist_name = (song_data.get("artist") or "").strip()
        album_name = (song_data.get("album") or "").strip()
        cover_url = song_data.get("pic") or song_data.get("albumPic") or ""
        if cover_url and not cover_url.startswith("http"):
            cover_url = ""
        artist_id = str(song_data.get("artistid", "") or "")
        album_id = str(song_data.get("albumid", "") or "")
        duration = int(song_data.get("duration", 0) or 0)
        # 音质列表从 nMinfo 解析
        nminfo = song_data.get("nMinfo", "") or song_data.get("nminfo", "") or ""
        formats = _parse_nminfo(nminfo)
        return SearchResult(
            id=f"kuwo_{pure_id}",
            title=song_name or "未知歌曲",
            artist=artist_name or "未知歌手",
            album=album_name or "未知专辑",
            cover=cover_url,
            source="kuwo",
            duration=duration,
            formats=formats,
            artist_id=artist_id,
            album_id=album_id,
        )
    except Exception as e:
        logger.warning(f"[Kuwo] query_song_by_id 异常: id={pure_id}, err={e}")
        return None


# 保留旧接口兼容（返回第一个结果的 JSON 格式）
async def search_by_keyword(keyword: str, timeout: float = 10.0) -> dict:
    """
    旧版兼容接口：搜索并返回第一个结果的播放 URL
    用于语音指令中的在线播放 fallback
    """
    results = await search(keyword, limit=1, timeout=timeout)
    if not results:
        return {"code": 404, "msg": "未找到歌曲", "data": None}

    r = results[0]
    # 优先选 MP3（在线播放兼容性最好）
    url = None
    for fmt in r.formats:
        if fmt.type == "mp3" and fmt.url:
            url = fmt.url
            break
    # MP3 都没有才用其他格式
    if not url:
        for fmt in r.formats:
            if fmt.url:
                url = fmt.url
                break

    return {
        "code": 0,
        "msg": "success",
        "data": {
            "title": r.title,
            "artist": r.artist,
            "album": r.album,
            "duration": r.duration,
            "cover_url": r.cover,
            "url": url,
            "source_data": {
                "platform": "kuwo",
                "musicId": r.id.replace("kuwo_", ""),
            },
        },
    }


async def get_artist_detail(artist_id: str, timeout: float = 10.0) -> dict:
    """
    获取歌手详情：基本信息 + 热门歌曲 + 专辑列表

    Args:
        artist_id: 歌手 ID
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
        client = await _get_client(timeout=timeout)
        # 1. 获取歌手热门歌曲（PC 端 artist2music 接口，参考 sqmusic ArtistSongListUrl）
        songs_url = (
            f"{KUWO_SEARCH_URL}?pn=0&rn=50&artistid={artist_id}"
            f"&stype=artist2music&sortby=0&encoding=utf8&{_KUWO_PC_PARAMS}"
        )
        logger.debug("[Kuwo] 歌手歌曲请求: url=%s", songs_url)
        resp = await client.get(songs_url, timeout=httpx.Timeout(timeout))
        resp.raise_for_status()

        # PC 端 API 返回标准 JSON（双引号），直接解析；移动端返回 Python 风格（单引号）需 _python_to_json
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            # 降级：尝试 Python 风格转 JSON
            try:
                data = json.loads(_python_to_json(resp.text))
            except json.JSONDecodeError:
                logger.error(f"[Kuwo] 歌手歌曲JSON解析失败")
                return result

        # PC 端 artist 字段在顶层，musiclist 是歌曲列表
        result["name"] = (data.get("artist") or "").strip()
        # 歌手头像：PC 端顶层可能有 pic/artistpic/web_artistpic_short 字段
        artist_pic = (data.get("pic") or data.get("artistpic") or data.get("web_artistpic_short") or "").strip()
        if artist_pic:
            if not artist_pic.startswith("http"):
                artist_pic = f"https://star.kuwo.cn/star/starheads/{artist_pic.lstrip('/')}"
            result["image"] = artist_pic
        else:
            # 兜底：用 artist_id 构造头像 URL
            result["image"] = f"https://star.kuwo.cn/star/starheads/180/{artist_id}.jpg"
        songs = data.get("musiclist", [])

        # 解析热门歌曲（含音质，字段为小写：name/artist/album/musicrid 等）
        async def _parse_artist_song(song: dict) -> Optional[SearchResult]:
            # musicrid 形如 "MUSIC_12345"，提取纯数字 ID
            song_id_raw = str(song.get("musicrid") or song.get("id") or "")
            pure_id: Optional[str] = None
            match = re.search(r"\d+", song_id_raw)
            if match:
                pure_id = match.group(0)
            if not pure_id:
                return None

            song_name = (song.get("name") or "").strip()
            artist_name = (song.get("artist") or song.get("aartist") or "").strip()
            album_name = (song.get("album") or "").strip()
            try:
                duration = int(song.get("duration", 180))
            except (TypeError, ValueError):
                duration = 180

            album_id_val = str(song.get("albumid") or "").strip()

            # 封面：优先专辑封面短路径，无则用歌手封面短路径（参考 sqmusic）
            cover_url = None
            album_pic_short = song.get("web_albumpic_short") or ""
            if album_pic_short:
                cover_url = re.sub(r'/120(?=/|$)', '/500', "https://img3.kuwo.cn/star/albumcover/" + album_pic_short)
            if not cover_url:
                artist_pic_short = song.get("web_artistpic_short") or ""
                if artist_pic_short:
                    cover_url = re.sub(r'/120(?=/|$)', '/500', "https://star.kuwo.cn/star/starheads/" + artist_pic_short)

            # 音质走 N_MINFO
            nminfo = song.get("N_MINFO") or song.get("nMinfo") or ""
            formats = _parse_nminfo(nminfo)

            # 取 allartistid 第一个作为 artist_id 回填
            raw_artist_id = song.get("artistid") or ""
            if not raw_artist_id:
                all_ids = song.get("allartistid") or ""
                if all_ids:
                    raw_artist_id = str(all_ids).split("&")[0].strip()
            song_artist_id = str(raw_artist_id).strip() if raw_artist_id else artist_id

            return SearchResult(
                id=f"kuwo_{pure_id}",
                title=song_name or "未知歌曲",
                artist=artist_name or "未知歌手",
                album=album_name or "未知专辑",
                cover=cover_url,
                source="kuwo",
                duration=duration,
                formats=formats,
                artist_id=song_artist_id,
                album_id=album_id_val,
            )

        song_tasks = [_parse_artist_song(s) for s in songs[:20]]
        song_results = await asyncio.gather(*song_tasks, return_exceptions=True)
        result["top_songs"] = [r for r in song_results if r is not None and not isinstance(r, Exception)]

        # 2. 获取歌手专辑列表（PC 端 albumlist 接口，参考 sqmusic ArtistAlbumListUrl）
        albums_url = (
            f"{KUWO_SEARCH_URL}?pn=0&rn=10000&artistid={artist_id}"
            f"&stype=albumlist&sortby=1&encoding=utf8&{_KUWO_PC_PARAMS}"
        )
        logger.debug("[Kuwo] 歌手专辑请求: url=%s", albums_url)
        resp2 = await client.get(albums_url, timeout=httpx.Timeout(timeout))
        resp2.raise_for_status()

        # PC 端标准 JSON，直接解析；降级用 _python_to_json
        try:
            album_data = json.loads(resp2.text)
        except json.JSONDecodeError:
            try:
                album_data = json.loads(_python_to_json(resp2.text))
            except json.JSONDecodeError:
                logger.error(f"[Kuwo] 歌手专辑JSON解析失败")
                return result

        # PC 端返回 albumlist，字段：albumid/name/artist/artistid/pic/pub
        album_list = album_data.get("albumlist", [])
        for alb in album_list[:20]:
            alb_id = str(alb.get("albumid") or "").strip()
            if not alb_id:
                continue
            alb_name = (alb.get("name") or "").strip()
            # 封面：SongCoverUrl + pic，/120 → /500
            alb_pic_short = alb.get("pic") or ""
            alb_pic = ""
            if alb_pic_short:
                alb_pic = re.sub(r'/120(?=/|$)', '/500', "https://img3.kuwo.cn/star/albumcover/" + alb_pic_short)
            if not alb_pic:
                alb_pic = f"https://img3.kuwo.cn/star/albumcover/500/0/0/{alb_id}.jpg"
            alb_year = str(alb.get("pub") or "").strip()

            result["albums"].append({
                "id": alb_id,
                "name": alb_name or "未知专辑",
                "cover": alb_pic,
                "year": alb_year,
            })

        logger.debug(
            "[Kuwo] 歌手详情: name=%s songs=%d albums=%d",
            result["name"], len(result["top_songs"]), len(result["albums"])
        )

    except Exception as e:
        logger.error(f"[Kuwo] 获取歌手详情失败: {e}")

    return result


async def get_album_detail(album_id: str, timeout: float = 10.0) -> dict:
    """
    获取专辑详情：基本信息 + 曲目列表（含音质）

    Args:
        album_id: 专辑 ID
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
        client = await _get_client(timeout=timeout)
        # PC 端 albuminfo 接口（参考 sqmusic AlbumInfoUrl），无 ft=music
        songs_url = (
            f"{KUWO_SEARCH_URL}?pn=0&rn=100&albumid={album_id}"
            f"&stype=albuminfo&encoding=utf8&{_KUWO_PC_PARAMS}"
        )
        logger.debug("[Kuwo] 专辑歌曲请求: url=%s", songs_url)
        resp = await client.get(songs_url, timeout=httpx.Timeout(timeout))
        resp.raise_for_status()

        # PC 端标准 JSON，直接解析；降级用 _python_to_json
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            try:
                data = json.loads(_python_to_json(resp.text))
            except json.JSONDecodeError:
                logger.error(f"[Kuwo] 专辑歌曲JSON解析失败")
                return result

        # PC 端专辑元信息在顶层：name/artist/aartist/img/pic/albumid
        result["name"] = (data.get("name") or "").strip()
        result["artist"] = (data.get("artist") or data.get("aartist") or "").strip()
        # 封面：优先 img，其次 pic，最后按 albumid 兜底
        cover = (data.get("img") or data.get("pic") or "").strip()
        if cover and not cover.startswith("http"):
            cover = re.sub(r'/120(?=/|$)', '/500', "https://img3.kuwo.cn/star/albumcover/" + cover)
        if not cover:
            cover = f"https://img3.kuwo.cn/star/albumcover/500/0/0/{album_id}.jpg"
        result["cover"] = cover

        # 曲目列表在 musiclist（PC 端小写字段）
        songs = data.get("musiclist", [])

        # 解析曲目（含音质，字段小写：name/artist/album/musicrid 等）
        async def _parse_track(song: dict) -> Optional[SearchResult]:
            # musicrid 形如 "MUSIC_12345"，提取纯数字 ID
            song_id_raw = str(song.get("musicrid") or song.get("id") or "")
            pure_id: Optional[str] = None
            match = re.search(r"\d+", song_id_raw)
            if match:
                pure_id = match.group(0)
            if not pure_id:
                return None

            song_name = (song.get("name") or "").strip()
            artist_name = (song.get("artist") or song.get("aartist") or "").strip()
            album_name = (song.get("album") or result["name"] or "").strip()
            try:
                duration = int(song.get("duration", 180))
            except (TypeError, ValueError):
                duration = 180

            # 曲目封面：优先 web_albumpic_short，否则用专辑封面
            track_cover = result["cover"]
            album_pic_short = song.get("web_albumpic_short") or ""
            if album_pic_short:
                track_cover = re.sub(r'/120(?=/|$)', '/500', "https://img3.kuwo.cn/star/albumcover/" + album_pic_short)

            # 音质走 N_MINFO
            nminfo = song.get("N_MINFO") or song.get("nMinfo") or ""
            formats = _parse_nminfo(nminfo)

            # artistid 优先，无则取 allartistid 第一个
            raw_artist_id = song.get("artistid") or ""
            if not raw_artist_id:
                all_ids = song.get("allartistid") or ""
                if all_ids:
                    raw_artist_id = str(all_ids).split("&")[0].strip()
            track_artist_id = str(raw_artist_id).strip() if raw_artist_id else ""

            return SearchResult(
                id=f"kuwo_{pure_id}",
                title=song_name or "未知歌曲",
                artist=artist_name or "未知歌手",
                album=album_name or "未知专辑",
                cover=track_cover,
                source="kuwo",
                duration=duration,
                formats=formats,
                artist_id=track_artist_id,
                album_id=album_id,
            )

        track_tasks = [_parse_track(s) for s in songs]
        track_results = await asyncio.gather(*track_tasks, return_exceptions=True)
        result["tracks"] = [r for r in track_results if r is not None and not isinstance(r, Exception)]

        logger.debug(
            "[Kuwo] 专辑详情: name=%s tracks=%d",
            result["name"], len(result["tracks"])
        )

    except Exception as e:
        logger.error(f"[Kuwo] 获取专辑详情失败: {e}")

    return result


class KuwoProvider(SearchProvider):
    """酷我音乐 SearchProvider 实现"""

    @property
    def name(self) -> str:
        return "kuwo"

    async def search(self, keyword: str, limit: int = 10,
                     search_type: str = "music",
                     skip_formats: bool = False,
                     cookie: str = "") -> list[SearchResult]:
        # kuwo 不需要 cookie，忽略
        return await search(keyword, limit=limit, search_type=search_type,
                           skip_formats=skip_formats)

    async def get_artist_detail(self, artist_id: str) -> dict:
        return await get_artist_detail(artist_id)

    async def get_album_detail(self, album_id: str) -> dict:
        return await get_album_detail(album_id)
