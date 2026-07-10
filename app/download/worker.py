"""后台下载消费者 - asyncio 事件循环"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from app.download.tracker import (
    get_waiting_tasks,
    update_task_status,
    add_task,
    reset_stale_loading_tasks,
)
from app.search.kuwo import search as kuwo_search
from app.search.netease import get_download_url as netease_download_url

logger = logging.getLogger("musicnest.download")

RUNNING = False

# 并发下载上限（semaphore 真正限流，DB 查询 limit 放大以避免饿死后排任务）
MAX_CONCURRENT = 2

# 扫描器增量更新回调（由 main.py 注入，避免循环导入）
_scan_new_callback = None

# 外部 MusicScanner 引用（由 main.py 注入，避免 worker 内部另建实例导致缓存不同步）
_external_scanner = None

# 文件名/目录名非法字符（Windows + Linux 通用）
# Windows: \ / : * ? " < > |
# 额外去除前导/尾随空格和点（Windows 不允许）
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_filename(name: str, max_len: int = 200) -> str:
    """清理文件名/目录名中的非法字符，防止路径越界和创建失败

    Args:
        name: 原始名称（歌名/歌手/专辑）
        max_len: 最大长度（防止文件名过长）

    Returns:
        清理后的安全名称，永远非空（空时返回 'Unknown'）
    """
    if not name:
        return "Unknown"
    # 替换非法字符为下划线
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name)
    # 去除前导/尾随空格和点（Windows 不允许）
    cleaned = cleaned.strip(" .")
    # 折叠连续空格
    cleaned = re.sub(r"\s+", " ", cleaned)
    # 限制长度（按字符数）
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "Unknown"


def set_scan_callback(cb) -> None:
    """注入扫描器增量更新回调"""
    global _scan_new_callback
    _scan_new_callback = cb


def set_scanner_ref(scanner) -> None:
    """注入外部 MusicScanner 引用，避免 worker 内部另建实例导致缓存不同步"""
    global _external_scanner
    _external_scanner = scanner


def _min_size_for_format(fmt: str) -> int:
    """根据音频格式返回最小文件大小阈值（字节）

    用于判断已存在文件是否为完整下载（过小的文件视为损坏/不完整）。
    """
    if fmt == "flac":
        return 100 * 1024  # 100KB
    if fmt == "mp3":
        return 30 * 1024   # 30KB
    return 10 * 1024       # 10KB


async def _download_file(url: str, dest: Path, task_id: str = "", timeout: float = 120.0) -> bool:
    """下载文件到目标路径，含进度日志

    采用原子写入：先下载到 `.part` 临时文件，成功后 os.replace 原子重命名，
    避免下载中断时残留半成品文件被误判为已下载。
    """
    dest_part = dest.with_suffix(dest.suffix + ".part")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Referer": "http://www.kuwo.cn/",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                last_log_pct = -1
                with open(dest_part, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = int(downloaded * 100 / total)
                            if pct >= last_log_pct + 10:
                                logger.info("[Download] 下载进度: %s - %d%% (%d/%d KB)", task_id[:8] if task_id else "?", pct, downloaded // 1024, total // 1024)
                                last_log_pct = pct
                # 下载完成，原子重命名到目标路径
                os.replace(dest_part, dest)
                return True
    except Exception as e:
        logger.warning(f"[Download] 下载文件失败: {url[:80]}... err={e}")
        # 清理 partial 文件，避免下次误判为成功
        try:
            if dest_part.exists():
                dest_part.unlink()
        except Exception:
            pass
        return False


async def _download_cover(cover_url: str, artist_dir: Path, album_dir: Path) -> bool:
    """下载封面图片到专辑目录，同时存一份到歌手目录作为歌手图"""
    if not cover_url:
        return False
    cover_path = album_dir / "cover.jpg"
    artist_img_path = artist_dir / "artist.jpg"
    ok = True
    if not cover_path.exists():
        ok = await _download_file(cover_url, cover_path)
    # 同时下载一份到歌手目录作为歌手头像（如果还没有）
    if ok and not artist_img_path.exists():
        artist_ok = await _download_file(cover_url, artist_img_path)
        if not artist_ok:
            logger.warning(f"[Cover] 歌手头像下载失败: {cover_url[:80]}")
    return ok


async def _fetch_lyrics(source: str, music_id: str, title: str, artist: str) -> Optional[str]:
    """获取歌词（酷我原文 / 网易云原文+翻译合并双语）"""
    # 酷我歌词 API（简化）
    if source == "kuwo":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                lrc_url = f"http://m.kuwo.cn/newh5/singles/songinfoandlrc?musicId={music_id}"
                resp = await client.get(lrc_url)
                data = resp.json()
                if data.get("status") == 200:
                    lrclist = data.get("data", {}).get("lrclist", [])
                    if lrclist:
                        lines = []
                        for item in lrclist:
                            t = item.get("time", "")
                            txt = item.get("lineLyric", "")
                            if txt:
                                lines.append(f"[{t}]{txt}")
                        return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[Lyrics] 获取酷我歌词失败: {e}")
    elif source == "netease":
        try:
            from app.search.netease import get_lyrics as netease_lyrics
            from app.config import config
            cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")
            lrc = await netease_lyrics(music_id, cookie=cookie)
            if lrc:
                logger.info(f"[Lyrics] 网易云歌词已获取: {title} ({len(lrc)} 字符)")
            return lrc or None
        except Exception as e:
            logger.warning(f"[Lyrics] 获取网易云歌词失败: {e}")
    return None


async def _write_id3_tags(track_path: Path, title: str, artist: str, album: str, cover_path: Optional[Path] = None) -> bool:
    """用 ffmpeg 写入音频元数据（标题、歌手、专辑、嵌入封面图）

    Returns:
        True 表示写入成功
    """
    try:
        cmd = ["ffmpeg", "-y", "-i", str(track_path)]
        if cover_path and cover_path.exists():
            cmd += ["-i", str(cover_path)]
            cmd += ["-map", "0:a", "-map", "1:v"]
            cmd += ["-disposition:v", "attached_pic"]
        cmd += ["-metadata", f"title={title}"]
        cmd += ["-metadata", f"artist={artist}"]
        cmd += ["-metadata", f"album={album}"]
        cmd += ["-c:a", "copy"]
        if cover_path and cover_path.exists():
            cmd += ["-c:v", "mjpeg"]
        tmp_path = str(track_path) + ".tmp"
        cmd += [tmp_path]

        # start_new_session=True 让子进程成为新进程组组长，便于 killpg 杀掉整个进程组
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            # 优先优雅终止整个进程组（ffmpeg 可能派生子进程）
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                # 2 秒后仍未退出，强杀整个进程组
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                except Exception:
                    pass
                await process.wait()
            logger.warning(f"[ID3] ffmpeg 超时: title={title}, artist={artist}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False

        if process.returncode == 0:
            os.replace(tmp_path, str(track_path))
            logger.info(f"[ID3] 标签写入成功: title={title}, artist={artist}, album={album}")
            return True
        else:
            stderr_text = stderr.decode(errors="replace") if stderr else ""
            logger.warning(f"[ID3] ffmpeg 写入失败: {stderr_text[:200]}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False
    except Exception as e:
        logger.warning(f"[ID3] 标签写入异常: {e}")
        return False


async def _process_task(task) -> bool:
    """处理单个下载任务（sqmusic 风格三步式）"""
    task_id = task.task_id
    source = task.source
    music_id = task.music_id
    title = task.title or "未知歌曲"
    artist = task.artist or "未知歌手"
    album = task.album or "未知专辑"
    cover_url = task.cover_url or ""
    format_type = task.format_type or "flac"

    logger.info(f"[Download] 开始处理: {task_id} {title} - {artist} [{source}]")

    # 标记为 loading
    await update_task_status(task_id, "loading")

    try:
        # ==== 第一步：获取歌曲详情（含封面、专辑ID、歌手ID）====
        download_url = None
        resolved_cover = cover_url
        resolved_artist = artist
        resolved_album = album

        if source == "kuwo":
            from app.search.kuwo import query_song_by_id, _get_music_formats

            # 优先用 music_id 精确查询，避免搜索匹配到错误歌曲
            matched = await query_song_by_id(f"kuwo_{music_id}")
            if not matched:
                # 退回到搜索匹配
                results = await kuwo_search(title, limit=5)
                for r in results:
                    rid = r.id.replace("kuwo_", "")
                    if rid == music_id:
                        matched = r
                        break
                if not matched:
                    # 标题归一化比较（去掉空格和特殊字符后精确匹配）
                    norm_title = re.sub(r'\s+', '', title)
                    for r in results:
                        if re.sub(r'\s+', '', r.title) == norm_title:
                            matched = r
                            break
                # 不再 fallback 到 results[0]：避免下载到错误歌曲

            if not matched:
                await update_task_status(task_id, "error", error_msg="未找到匹配歌曲")
                return False

            if not resolved_cover and matched.cover:
                resolved_cover = matched.cover
            if not resolved_artist or resolved_artist == "未知歌手":
                resolved_artist = matched.artist
            if not resolved_album or resolved_album == "未知专辑":
                resolved_album = matched.album

            # 获取格式 URL（nMinfo 不包含 URL，需单独拉）
            fmt_list = await _get_music_formats(matched.id.replace("kuwo_", ""))
            for fmt in fmt_list:
                if fmt.type == format_type and fmt.url:
                    download_url = fmt.url
                    break
            # fallback: 任意可用格式
            if not download_url:
                for fmt in fmt_list:
                    if fmt.url:
                        download_url = fmt.url
                        break

        elif source == "netease":
            from app.search.netease import get_song_detail as netease_song_detail
            from app.config import config

            # 网易云需要 cookie 才能获取 FLAC/Hi-Res 下载链接
            netease_cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")

            # 先拿歌曲详情（含封面）
            detail = await netease_song_detail(music_id)
            if detail:
                if not resolved_cover and detail.cover:
                    resolved_cover = detail.cover
                if not resolved_artist or resolved_artist == "未知歌手":
                    resolved_artist = detail.artist
                if not resolved_album or resolved_album == "未知专辑":
                    resolved_album = detail.album

            # 获取下载链接（需传 cookie 才能拿到高品质）
            br_map = {"flac": 999000, "mp3": 320000}
            br = br_map.get(format_type, 320000)
            download_url = await netease_download_url(music_id, br=br, cookie=netease_cookie)
            if not download_url:
                download_url = await netease_download_url(music_id, br=320000, cookie=netease_cookie)

        if not download_url:
            await update_task_status(task_id, "error", "无法获取下载链接")
            return False

        # ==== 第二步：构建目录 ====
        from app.config import config
        music_path = config.get("music_path", "/music")
        # 清理文件名/目录名中的非法字符，防止路径越界和创建失败
        safe_artist = _sanitize_filename(resolved_artist)
        safe_album = _sanitize_filename(resolved_album)
        safe_title = _sanitize_filename(title)
        artist_dir = Path(music_path) / safe_artist
        album_dir = artist_dir / safe_album
        album_dir.mkdir(parents=True, exist_ok=True)

        # 确定文件扩展名
        ext = format_type if format_type in ("flac", "mp3", "wav") else "mp3"
        track_filename = f"{safe_title}.{ext}"
        track_path = album_dir / track_filename

        # 如果已存在同名文件、大小达标且 ID3 已写入，只补图片
        min_size = _min_size_for_format(format_type)
        id3done_path = track_path.with_suffix(track_path.suffix + ".id3done")
        file_ready = track_path.exists() and track_path.stat().st_size >= min_size
        id3_done = id3done_path.exists()

        if file_ready and id3_done:
            logger.info(f"[Download] 文件已存在: {track_path}")
            if resolved_cover:
                await _download_cover(resolved_cover, artist_dir, album_dir)
            await update_task_status(task_id, "success", file_path=str(track_path))
            return True

        # ==== 第三步：下载音频 ====
        if file_ready:
            # 文件已存在但 ID3 未完成，跳过下载只补 ID3
            logger.info(f"[Download] 音频已存在，跳过下载: {track_path}")
        else:
            logger.info(f"[Download] 下载音频: {download_url[:80]}...")
            success = await _download_file(download_url, track_path, task_id=task_id)
            if not success:
                await update_task_status(task_id, "error", "音频下载失败")
                return False

        # ==== 第四步：下载封面 + 歌手图 ====
        if resolved_cover:
            await _download_cover(resolved_cover, artist_dir, album_dir)

        # ==== 第五步：下载歌词 ====
        lyrics = await _fetch_lyrics(source, music_id, title, resolved_artist)
        if lyrics:
            lrc_path = album_dir / f"{safe_title}.lrc"
            await asyncio.to_thread(lrc_path.write_text, lyrics, encoding="utf-8")

        # ==== 第六步：写入 ID3 标签（用 ffmpeg 写标题/歌手/专辑 + 嵌入封面）====
        cover_img = album_dir / "cover.jpg"
        if cover_img.exists():
            id3_ok = await _write_id3_tags(track_path, title, resolved_artist, resolved_album, cover_img)
        else:
            id3_ok = await _write_id3_tags(track_path, title, resolved_artist, resolved_album)

        # ID3 写入成功后创建标记文件，避免重试时跳过 ID3
        if id3_ok:
            try:
                id3done_path.touch()
            except Exception as e:
                logger.warning(f"[Download] 创建 ID3 标记文件失败: {e}")

        await update_task_status(task_id, "success", file_path=str(track_path))
        logger.info(f"[Download] 完成: {task_id} -> {track_path}")
        # 通知扫描器增量更新（优先使用外部 scanner 引用，避免缓存不同步）
        scanned = False
        if _external_scanner is not None:
            try:
                await _external_scanner.scan_new([str(track_path)])
                scanned = True
            except Exception as e:
                logger.debug(f"[Download] 外部扫描器增量更新失败: {e}")
        if not scanned and _scan_new_callback:
            try:
                await _scan_new_callback([str(track_path)])
            except Exception as e:
                logger.debug(f"[Download] 扫描器增量更新失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[Download] 下载异常: {task_id} err={e}", exc_info=True)
        await update_task_status(task_id, "error", str(e))
        return False


async def download_worker(poll_interval: float = 5.0):
    """后台下载消费者，每 poll_interval 秒查询并处理等待中的任务"""
    global RUNNING
    RUNNING = True
    logger.info("[Download] 下载 Worker 已启动")

    # 启动时重置上次崩溃残留的 loading 任务，避免卡死（阈值 60 分钟，避免误杀大文件任务）
    await reset_stale_loading_tasks(timeout_minutes=60)

    # 并发限制：由 semaphore 真正限流，DB 查询 limit 放大以充分填充并发槽
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    # 周期性重置 stale loading 任务（每 5 分钟一次）
    stale_reset_interval = 300
    last_reset_time = time.time()

    async def process_with_semaphore(task):
        async with sem:
            await _process_task(task)

    while RUNNING:
        try:
            tasks = await get_waiting_tasks(limit=10)
            if tasks:
                logger.info(f"[Download] 处理 {len(tasks)} 个待下载任务")
                coros = [process_with_semaphore(t) for t in tasks]
                await asyncio.gather(*coros, return_exceptions=True)

            # 周期性重置卡死的 loading 任务（每 5 分钟一次，阈值 60 分钟）
            now = time.time()
            if now - last_reset_time >= stale_reset_interval:
                await reset_stale_loading_tasks(timeout_minutes=60)
                last_reset_time = now
        except Exception as e:
            logger.error(f"[Download] Worker 异常: {e}", exc_info=True)

        await asyncio.sleep(poll_interval)


def stop_worker():
    """停止下载 worker"""
    global RUNNING
    RUNNING = False
    logger.info("[Download] 下载 Worker 已停止")


# ===== 歌单同步 =====

async def playlist_sync_worker(sync_interval: int = 1800):
    """歌单同步后台定时任务（增量同步）

    优化：使用 playlist_sync_anchor 锚点避免重复拉取未变化的歌单曲目列表。
    - 先获取歌单详情的 updateTime / trackUpdateTime
    - 若两个锚点与本地一致，则整个跳过（无需拉取曲目列表）
    - 若变化，才拉取曲目列表并 diff 出新增曲目
    """
    global RUNNING
    logger.info(f"[PlaylistSync] 歌单同步已启动, 间隔={sync_interval}s")

    from app.config import config
    from app.download.tracker import (
        get_synced_ids, record_sync,
        get_playlist_sync_anchor, set_playlist_sync_anchor,
    )
    from app.search.netease import get_playlist_tracks, get_playlist_detail
    from app.search.kuwo import search as kuwo_search
    from app.music.scanner import MusicScanner

    # 优先使用外部注入的 scanner（与主进程共享缓存，避免两个实例缓存不同步）；
    # 外部未注入时才本地创建。
    if _external_scanner is not None:
        _scanner = _external_scanner
    else:
        _scanner = MusicScanner(config.get("music_path", "/music"))

    while RUNNING:
        await asyncio.sleep(sync_interval)
        try:
            playlists = config.get("playlist_sync", [])
            if not playlists:
                continue

            for pl in playlists:
                if not pl.get("enabled", True):
                    continue

                source = pl.get("source", "netease")
                pl_id = pl.get("id", "")
                pl_name = pl.get("name", "未知歌单")

                if not pl_id:
                    continue

                # === 增量同步：先检查歌单锚点 ===
                if source == "netease":
                    cookie = config.get("netease_cookie", "") or config.get("netease", {}).get("cookie", "")
                    # 获取歌单元数据（轻量请求）
                    detail = await get_playlist_detail(pl_id, cookie=cookie)
                    remote_update = detail.get("update_time", 0)
                    remote_track_update = detail.get("track_update_time", 0)

                    # 对比本地锚点
                    local_update, local_track_update = await get_playlist_sync_anchor(source, pl_id)
                    if remote_update and remote_update == local_update \
                            and remote_track_update == local_track_update:
                        logger.debug(f"[PlaylistSync] 歌单 [{pl_name}] 锚点未变化，跳过")
                        continue

                    logger.info(f"[PlaylistSync] 歌单 [{pl_name}] 有更新 "
                                f"(updateTime: {local_update}->{remote_update}, "
                                f"trackUpdateTime: {local_track_update}->{remote_track_update})")

                    # 锚点变化，拉取曲目列表
                    tracks = await get_playlist_tracks(pl_id, cookie=cookie)
                else:
                    # 酷我歌单暂不支持（按歌曲名搜索太模糊）
                    continue

                # 已同步 ID
                synced = await get_synced_ids(source, pl_id)

                new_count = 0
                for track in tracks:
                    if track.id in synced:
                        continue

                    try:
                        # 先检查本地是否已有该歌曲（按标题+歌手集合交集匹配）
                        local_songs = _scanner.search(track.title)
                        already_local = False
                        # 将歌手名按分隔符拆分为集合，用集合交集判断（避免子串误匹配）
                        track_artists = {p.strip() for p in re.split(r'[,/、;&]', track.artist or "") if p.strip()}
                        for ls in local_songs:
                            local_artists = {p.strip() for p in re.split(r'[,/、;&]', ls.get("artist") or "") if p.strip()}
                            if track_artists and local_artists and (track_artists & local_artists):
                                already_local = True
                                break

                        if already_local:
                            # 本地已有，直接标记为已同步，不重复下载
                            await record_sync(source, pl_id, track.id)
                            logger.info("[PlaylistSync] 跳过已存在本地的歌曲: %s - %s", track.artist, track.title)
                            continue

                        # 加入下载队列
                        task_id = f"{source}_sync_{pl_id}_{track.id}"
                        flac_priority = config.get("download", {}).get("flac_priority", True)
                        fmt = "flac" if flac_priority else "mp3"

                        added = await add_task(
                            task_id=task_id,
                            source=source,
                            music_id=track.id.replace(f"{source}_", ""),
                            title=track.title,
                            artist=track.artist,
                            album=track.album,
                            cover_url=track.cover or "",
                            format_type=fmt,
                        )
                        # add_task 的 ON CONFLICT 已自动将 error/loading 状态重置为 waiting
                        await record_sync(source, pl_id, track.id)
                        new_count += 1
                    except Exception as e:
                        logger.error(f"[PlaylistSync] 处理单曲失败: {track.artist} - {track.title} err={e}", exc_info=True)
                        continue

                # 无论是否有新曲目，都更新锚点（避免下次重复拉取）
                if remote_update or remote_track_update:
                    await set_playlist_sync_anchor(source, pl_id, remote_update, remote_track_update)

                if new_count > 0:
                    logger.info(f"[PlaylistSync] 歌单 [{pl_name}] 新增 {new_count} 首待下载")
                else:
                    logger.info(f"[PlaylistSync] 歌单 [{pl_name}] 无新歌曲（已更新锚点）")

        except Exception as e:
            logger.error(f"[PlaylistSync] 同步异常: {e}", exc_info=True)
