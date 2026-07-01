"""后台下载消费者 - asyncio 事件循环"""

import asyncio
import hashlib
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from app.download.tracker import (
    get_waiting_tasks,
    update_task_status,
    get_task_by_id,
    add_task,
)
from app.search.kuwo import search as kuwo_search
from app.search.netease import get_download_url as netease_download_url

logger = logging.getLogger("musicnest.download")

RUNNING = False

# 扫描器增量更新回调（由 main.py 注入，避免循环导入）
_scan_new_callback = None


def set_scan_callback(cb) -> None:
    """注入扫描器增量更新回调"""
    global _scan_new_callback
    _scan_new_callback = cb


async def _download_file(url: str, dest: Path, task_id: str = "", timeout: float = 120.0) -> bool:
    """下载文件到目标路径，含进度日志"""
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
                loop = asyncio.get_running_loop()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        await loop.run_in_executor(None, f.write, chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = int(downloaded * 100 / total)
                            if pct >= last_log_pct + 10:
                                logger.info("[Download] 下载进度: %s - %d%% (%d/%d KB)", task_id[:8] if task_id else "?", pct, downloaded // 1024, total // 1024)
                                last_log_pct = pct
                return True
    except Exception as e:
        logger.warning(f"[Download] 下载文件失败: {url[:80]}... err={e}")
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
        await _download_file(cover_url, artist_img_path)
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


async def _write_id3_tags(track_path: Path, title: str, artist: str, album: str, cover_path: Optional[Path] = None):
    """用 ffmpeg 写入音频元数据（标题、歌手、专辑、嵌入封面图）"""
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

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.warning(f"[ID3] ffmpeg 超时: title={title}, artist={artist}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return

        if process.returncode == 0:
            os.replace(tmp_path, str(track_path))
            logger.info(f"[ID3] 标签写入成功: title={title}, artist={artist}, album={album}")
        else:
            stderr_text = stderr.decode(errors="replace") if stderr else ""
            logger.warning(f"[ID3] ffmpeg 写入失败: {stderr_text[:200]}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as e:
        logger.warning(f"[ID3] 标签写入异常: {e}")


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
    update_task_status(task_id, "loading")

    try:
        # ==== 第一步：获取歌曲详情（含封面、专辑ID、歌手ID）====
        download_url = None
        resolved_cover = cover_url
        resolved_artist = artist
        resolved_album = album

        if source == "kuwo":
            # 搜一遍获取完整信息（含封面和音质链接）
            results = await kuwo_search(title, limit=5)
            matched = None
            for r in results:
                rid = r.id.replace("kuwo_", "")
                if rid == music_id or r.title == title:
                    matched = r
                    break
            if not matched and results:
                matched = results[0]

            if matched:
                # sqmusic 风格：再用 querySongById 补调详情（更准确的信息）
                from app.search.kuwo import query_song_by_id, _get_music_formats
                detail = await query_song_by_id(matched.id)
                if detail:
                    matched = detail

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
            from app.search.netease import get_download_url as netease_download_url

            # 先拿歌曲详情（含封面）
            detail = await netease_song_detail(music_id)
            if detail:
                if not resolved_cover and detail.cover:
                    resolved_cover = detail.cover
                if not resolved_artist or resolved_artist == "未知歌手":
                    resolved_artist = detail.artist
                if not resolved_album or resolved_album == "未知专辑":
                    resolved_album = detail.album

            # 获取下载链接
            br_map = {"flac": 999000, "mp3": 320000}
            br = br_map.get(format_type, 320000)
            download_url = await netease_download_url(music_id, br=br)
            if not download_url:
                download_url = await netease_download_url(music_id, br=320000)

        if not download_url:
            update_task_status(task_id, "error", "无法获取下载链接")
            return False

        # ==== 第二步：构建目录 ====
        from app.config import config
        music_path = config.get("music_path", "/music")
        artist_dir = Path(music_path) / resolved_artist
        album_dir = artist_dir / resolved_album
        album_dir.mkdir(parents=True, exist_ok=True)

        # 确定文件扩展名
        ext = format_type if format_type in ("flac", "mp3", "wav") else "mp3"
        track_filename = f"{title}.{ext}"
        track_path = album_dir / track_filename

        # 如果已存在同名文件，只补图片
        if track_path.exists():
            logger.info(f"[Download] 文件已存在: {track_path}")
            if resolved_cover:
                await _download_cover(resolved_cover, artist_dir, album_dir)
            update_task_status(task_id, "success", file_path=str(track_path))
            return True

        # ==== 第三步：下载音频 ====
        logger.info(f"[Download] 下载音频: {download_url[:80]}...")
        success = await _download_file(download_url, track_path, task_id=task_id)
        if not success:
            update_task_status(task_id, "error", "音频下载失败")
            return False

        # ==== 第四步：下载封面 + 歌手图 ====
        if resolved_cover:
            await _download_cover(resolved_cover, artist_dir, album_dir)

        # ==== 第五步：下载歌词 ====
        lyrics = await _fetch_lyrics(source, music_id, title, resolved_artist)
        if lyrics:
            lrc_path = album_dir / f"{title}.lrc"
            lrc_path.write_text(lyrics, encoding="utf-8")

        # ==== 第六步：写入 ID3 标签（用 ffmpeg 写标题/歌手/专辑 + 嵌入封面）====
        cover_img = album_dir / "cover.jpg"
        if cover_img.exists():
            await _write_id3_tags(track_path, title, resolved_artist, resolved_album, cover_img)
        else:
            await _write_id3_tags(track_path, title, resolved_artist, resolved_album)

        update_task_status(task_id, "success", file_path=str(track_path))
        logger.info(f"[Download] 完成: {task_id} -> {track_path}")
        # 通知扫描器增量更新（通过回调，避免循环导入）
        if _scan_new_callback:
            try:
                await _scan_new_callback([str(track_path)])
            except Exception as e:
                logger.debug(f"[Download] 扫描器增量更新失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[Download] 下载异常: {task_id} err={e}", exc_info=True)
        update_task_status(task_id, "error", str(e))
        return False


async def download_worker(poll_interval: float = 5.0):
    """后台下载消费者，每 poll_interval 秒查询并处理等待中的任务"""
    global RUNNING
    RUNNING = True
    logger.info("[Download] 下载 Worker 已启动")

    # 并发限制
    sem = asyncio.Semaphore(2)

    async def process_with_semaphore(task):
        async with sem:
            await _process_task(task)

    while RUNNING:
        try:
            tasks = get_waiting_tasks(limit=2)
            if tasks:
                logger.info(f"[Download] 处理 {len(tasks)} 个待下载任务")
                coros = [process_with_semaphore(t) for t in tasks]
                await asyncio.gather(*coros, return_exceptions=True)
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
                    local_update, local_track_update = get_playlist_sync_anchor(source, pl_id)
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
                synced = get_synced_ids(source, pl_id)

                # 本地扫描器（用于检查歌曲是否已存在）
                _scanner = MusicScanner(config.get("music_path", "/music"))

                new_count = 0
                for track in tracks:
                    if track.id in synced:
                        continue

                    # 先检查本地是否已有该歌曲（按标题+歌手匹配）
                    local_songs = _scanner.search(track.title)
                    already_local = False
                    for ls in local_songs:
                        local_artist = (ls.get("artist") or "").strip()
                        track_artist = (track.artist or "").strip()
                        # 标题匹配 + 歌手名包含匹配（本地可能多了分隔符等）
                        if local_artist and track_artist and (
                            local_artist in track_artist or track_artist in local_artist
                        ):
                            already_local = True
                            break

                    if already_local:
                        # 本地已有，直接标记为已同步，不重复下载
                        record_sync(source, pl_id, track.id)
                        logger.info(f"[PlaylistSync] 跳过已存在本地的歌曲: {track.artist} - {track.title}")
                        continue

                    # 加入下载队列
                    task_id = f"{source}_sync_{pl_id}_{track.id}"
                    flac_priority = config.get("download", {}).get("flac_priority", True)
                    fmt = "flac" if flac_priority else "mp3"

                    add_task(
                        task_id=task_id,
                        source=source,
                        music_id=track.id.replace(f"{source}_", ""),
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        cover_url=track.cover or "",
                        format_type=fmt,
                    )
                    record_sync(source, pl_id, track.id)
                    new_count += 1

                # 无论是否有新曲目，都更新锚点（避免下次重复拉取）
                if remote_update or remote_track_update:
                    set_playlist_sync_anchor(source, pl_id, remote_update, remote_track_update)

                if new_count > 0:
                    logger.info(f"[PlaylistSync] 歌单 [{pl_name}] 新增 {new_count} 首待下载")
                else:
                    logger.info(f"[PlaylistSync] 歌单 [{pl_name}] 无新歌曲（已更新锚点）")

        except Exception as e:
            logger.error(f"[PlaylistSync] 同步异常: {e}", exc_info=True)
