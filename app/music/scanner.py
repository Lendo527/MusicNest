"""NAS 音乐库扫描器 - 支持多层文件夹结构 + 歌词关联"""

import asyncio
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 支持的音乐文件扩展名
SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".wma", ".aac"}

# 支持的歌词文件扩展名
LYRICS_EXTENSIONS = {".lrc", ".txt"}

# 文件名清理正则：去掉曲号前缀如 "01 ", "01.", "01-" 等
_TRACK_NUM_RE = re.compile(r"^\d{1,3}[\s.\-_—]+\s*")

# 文件名中的附加信息：如 [flac], (320kbps), (Hires) 等
_EXTRA_INFO_RE = re.compile(r"\s*[\[(][^)\]]*[\]\)]\s*")


def _clean_title(filename: str) -> str:
    """清理文件名：去掉曲号、附加信息，返回干净的标题"""
    title = _TRACK_NUM_RE.sub("", filename)
    title = _EXTRA_INFO_RE.sub("", title)
    return title.strip()


async def _probe_file_async(filepath: Path) -> dict:
    """异步 ffprobe 读取所有元数据，返回 {title, artist, album, duration}"""
    try:
        import subprocess
        import json
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", str(filepath)],
                capture_output=True, text=True, timeout=15
            )
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            tags = data.get("format", {}).get("tags", {})
            fmt = data.get("format", {})
            meta = {}
            for field, tag_keys in [("title", ["title", "Title", "TITLE"]),
                                     ("artist", ["artist", "Artist", "ARTIST"]),
                                     ("album", ["album", "Album", "ALBUM"])]:
                for key in tag_keys:
                    val = tags.get(key)
                    if val:
                        meta[field] = val.strip()
                        break
            # 从 format 中取 duration
            duration_str = fmt.get("duration", "")
            if duration_str:
                try:
                    meta["duration"] = int(float(duration_str))
                except (ValueError, TypeError):
                    pass
            return meta
    except Exception as e:
        logger.debug(f"[Scanner] ffprobe 解析失败: {filepath.name} err={e}")
    return {}


def _find_lyrics(audio_path: Path) -> Optional[str]:
    """查找同名的歌词文件"""
    stem = audio_path.stem
    parent = audio_path.parent
    for ext in LYRICS_EXTENSIONS:
        lrc_path = parent / f"{stem}{ext}"
        if lrc_path.exists():
            return str(lrc_path)
    # 也尝试 cleaned title（去掉曲号后）
    clean_stem = _clean_title(stem)
    if clean_stem and clean_stem != stem:
        for ext in LYRICS_EXTENSIONS:
            lrc_path = parent / f"{clean_stem}{ext}"
            if lrc_path.exists():
                return str(lrc_path)
    return None


class MusicScanner:
    """音乐库扫描器 - 支持多层文件夹结构"""

    # 缓存文件路径（/data 持久卷）
    CACHE_FILE = "/data/songs_cache.json"

    def __init__(self, music_path: str = "/music"):
        self._music_path = music_path
        self._songs: list[dict] = []
        self._scan_time: Optional[float] = None
        self._auto_scan_task: Optional["asyncio.Task"] = None  # type: ignore
        self._auto_scan_interval: int = 0  # 0 = 禁用
        # 使用 asyncio.Lock 避免在 async 函数中阻塞事件循环
        self._lock = asyncio.Lock()  # 直接创建，避免惰性初始化的线程安全问题
        self._thread_lock = threading.RLock()  # 可重入锁，允许嵌套调用 _save_cache
        self._load_cache()  # 启动时立即加载缓存

    def _get_async_lock(self) -> asyncio.Lock:
        """获取 asyncio.Lock"""
        return self._lock

    def _load_cache(self) -> bool:
        """从缓存文件加载歌曲列表。成功返回 True"""
        cache_path = Path(self.CACHE_FILE)
        if not cache_path.exists():
            return False
        import json
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[Scanner] 缓存文件损坏: {e}，将重新扫描")
            self._songs = []
            return False
        except Exception as e:
            logger.warning(f"[Scanner] 缓存加载失败: {e}")
            self._songs = []
            return False

        if not isinstance(data, list):
            logger.warning("[Scanner] 缓存格式异常，将重新扫描")
            self._songs = []
            return False

        # 逐项校验必需字段
        required_keys = {"title", "artist", "filepath"}
        valid_songs = []
        for s in data:
            if isinstance(s, dict) and required_keys.issubset(s.keys()):
                valid_songs.append(s)
            else:
                logger.debug(f"[Scanner] 丢弃无效缓存项: {s}")
        self._songs = valid_songs
        self._scan_time = time.time()
        logger.info(f"[Scanner] 从缓存加载: {len(valid_songs)} 首歌曲")
        return True

    def _save_cache(self) -> None:
        """保存缓存到文件（原子写入）"""
        with self._thread_lock:
            snapshot = list(self._songs)
        import json
        import os
        cache_path = Path(self.CACHE_FILE)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            os.replace(str(tmp_path), str(cache_path))  # 原子替换
            logger.info(f"[Scanner] 缓存已保存: {len(snapshot)} 首歌曲")
        except Exception as e:
            logger.error(f"[Scanner] 缓存保存失败: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    async def scan(self) -> list[dict]:
        """扫描音乐目录，返回歌曲列表"""
        async with self._get_async_lock():
            songs: list[dict] = []
            root = Path(self._music_path)

            if not root.exists():
                self._songs = []
                self._scan_time = time.time()
                return []

            # 第一步：收集所有音乐文件
            audio_files: list[Path] = []
            for filepath in root.rglob("*"):
                if filepath.is_symlink():
                    continue  # 跳过符号链接，防止越界
                if not filepath.is_file():
                    continue
                ext = filepath.suffix.lower()
                if ext in SUPPORTED_EXTENSIONS:
                    audio_files.append(filepath)

            # 第二步：并发解析歌曲信息
            sem = asyncio.Semaphore(10)  # 最多 10 个并发 ffprobe

            async def process_file(filepath):
                async with sem:
                    relative = filepath.relative_to(root)
                    parts = relative.parts

                    filename = filepath.stem
                    title = _clean_title(filename)
                    # 并发 ffprobe 读取标签+时长（一次调用）
                    meta_tags = await _probe_file_async(filepath)
                    if meta_tags.get("title"):
                        title = meta_tags["title"]
                    artist = meta_tags.get("artist", "")
                    album = meta_tags.get("album", "")
                    duration = meta_tags.get("duration", 0)

                    # ===== 多层路径解析（仅当标签未提供 artist/album 时作为 fallback）=====
                    # 支持的结构：
                    #   music/Artist/Album/song.mp3                           → artist=Artist, album=Album, title=song
                    #   music/Artist/Album/01-song.mp3                        → artist=Artist, album=Album, title=song
                    #   music/Artist/Album/Disc 1/01-song.mp3                 → artist=Artist, album=Album, title=song
                    #   music/Artist/Album/song.mp3 + song.lrc                → 关联歌词
                    #   music/Artist - Album/01 - song.mp3                    → artist=Artist, album=Album, title=song
                    #   music/song.mp3                                        → 仅标题
                    #   music/Album/song.mp3                                  → artist=未知, album=Album

                    if len(parts) == 1:
                        # 根目录：music/song.mp3
                        pass

                    elif len(parts) >= 3:
                        # artist/album/song 或更深
                        # 跳过中间的额外子目录（如 Disc 1, CD1 等）
                        if not artist:
                            artist = parts[0]
                        if not album:
                            album = parts[1]

                        # 如果第3层看起来像独立目录（Disc 1, CD 1 等），跳过它
                        extra_dirs = {"disc", "cd", "disk", "volume", "vol", "part", "pt"}
                        for part in parts[2:-1]:
                            p_lower = part.lower()
                            if any(
                                p_lower.startswith(d) and (len(p_lower) == len(d) or p_lower[len(d):].lstrip().isdigit())
                                for d in extra_dirs
                            ):
                                continue  # 跳过 Disc 1 / CD1 这类
                            # 其他中间目录作为专辑名补充
                            if album:
                                album = f"{album} / {part}"
                            else:
                                album = part

                    elif len(parts) == 2:
                        # music/Artist/song.mp3 或 music/Album/song.mp3
                        # 判断是歌手还是专辑（通常歌手的文件夹名不会像曲名）
                        first_part = parts[0]
                        # 如果文件夹名带 " - " 分隔，可能是 artist - album
                        if " - " in first_part:
                            split_aa = first_part.split(" - ", 1)
                            if not artist:
                                artist = split_aa[0].strip()
                            if not album:
                                album = split_aa[1].strip()
                        else:
                            # 无法判断，统一作为 artist
                            if not artist:
                                artist = first_part

                    # 歌词关联
                    lyrics_path = _find_lyrics(filepath)

                    # 构建歌手路径和专辑路径（用于封面查找）
                    artist_path = str(root / parts[0]) if len(parts) >= 2 and parts[0] else ""
                    album_path = ""
                    if len(parts) >= 3 and parts[0] and parts[1]:
                        album_path = str(root / parts[0] / parts[1])
                    elif len(parts) == 2 and " - " in parts[0]:
                        # artist - album 格式
                        split_aa = parts[0].split(" - ", 1)
                        album_path = str(root / parts[0])

                    try:
                        file_size = filepath.stat().st_size if filepath.exists() else 0
                    except (FileNotFoundError, PermissionError):
                        file_size = 0

                    return {
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "duration": duration,
                        "format": filepath.suffix.removeprefix(".").upper(),
                        "filepath": str(filepath),
                        "lyrics_path": lyrics_path,
                        "size": file_size,
                        "artist_path": artist_path,
                        "album_path": album_path,
                    }

            tasks = [process_file(fp) for fp in audio_files]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            songs = [s for s in results if isinstance(s, dict)]

            # 按 artist → album → title 排序
            songs.sort(key=lambda s: (s["artist"].lower(), s["album"].lower(), s["title"].lower()))

            self._songs = songs
            self._scan_time = time.time()
            self._save_cache()
            return songs

    async def scan_new(self, filepaths: list[str]) -> int:
        """增量扫描指定路径，将新文件合并到现有缓存"""
        async with self._get_async_lock():
            root = Path(self._music_path)
            new_songs = []
            with self._thread_lock:
                existing_paths = {s["filepath"] for s in self._songs}
            # 过滤有效文件（跳过符号链接）
            valid_files = []
            for fp in filepaths:
                p = Path(fp)
                if p.is_symlink():
                    continue  # 跳过符号链接，防止越界
                if not p.exists() or not p.is_file():
                    continue
                if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if str(p) in existing_paths:
                    continue
                valid_files.append(p)

            # 并发 ffprobe（参照 scan 的并发结构）
            sem = asyncio.Semaphore(10)

            async def _probe_one(p: Path) -> dict:
                async with sem:
                    relative = p.relative_to(root)
                    parts = relative.parts
                    filename = p.stem
                    title = _clean_title(filename)
                    meta_tags = await _probe_file_async(p)
                    if meta_tags.get("title"):
                        title = meta_tags["title"]
                    artist = meta_tags.get("artist", "")
                    album = meta_tags.get("album", "")
                    duration = meta_tags.get("duration", 0)
                    if len(parts) >= 3:
                        if not artist: artist = parts[0]
                        if not album: album = parts[1]
                    elif len(parts) == 2:
                        if " - " in parts[0]:
                            aa = parts[0].split(" - ", 1)
                            if not artist: artist = aa[0].strip()
                            if not album: album = aa[1].strip()
                        else:
                            if not artist: artist = parts[0]
                    lyrics_path = _find_lyrics(p)
                    artist_path = str(root / parts[0]) if len(parts) >= 2 else ""
                    album_path = str(root / parts[0] / parts[1]) if len(parts) >= 3 else ""
                    try:
                        size = p.stat().st_size if p.exists() else 0
                    except (FileNotFoundError, PermissionError):
                        size = 0
                    return {
                        "title": title, "artist": artist, "album": album,
                        "duration": duration, "format": p.suffix.removeprefix(".").upper(),
                        "filepath": str(p), "lyrics_path": lyrics_path,
                        "size": size,
                        "artist_path": artist_path, "album_path": album_path,
                    }

            tasks = [_probe_one(p) for p in valid_files]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, dict):
                    new_songs.append(r)
                elif isinstance(r, Exception):
                    logger.warning(f"[Scanner] scan_new 处理文件失败: {r}")
            if new_songs:
                with self._thread_lock:
                    self._songs.extend(new_songs)
                    self._songs.sort(key=lambda s: (s["artist"].lower(), s["album"].lower(), s["title"].lower()))
                self._save_cache()
            return len(new_songs)

    def get_index_by_filepath(self, filepath: str) -> Optional[int]:
        """通过 filepath 查找歌曲索引"""
        with self._thread_lock:
            for i, s in enumerate(self._songs):
                if s.get("filepath") == filepath:
                    return i
            return None

    def remove_song(self, index: int) -> Optional[dict]:
        """移除指定索引的歌曲并保存缓存"""
        # 注意：self._lock 是 asyncio.Lock（不能用于同步 with），同步操作用 _thread_lock
        with self._thread_lock:
            if 0 <= index < len(self._songs):
                removed = self._songs.pop(index)
                self._save_cache()
                return removed
            return None

    def iter_songs(self) -> list:
        """返回 songs 的快照副本，供外部安全迭代"""
        with self._thread_lock:
            return list(self._songs)

    def remove_by_filepath(self, filepath: str) -> bool:
        """按文件路径删除歌曲（替代按位置 index 删除）"""
        with self._thread_lock:
            for i, s in enumerate(self._songs):
                if s.get("filepath") == filepath:
                    self._songs.pop(i)
                    self._save_cache()
                    return True
            return False

    def remove_artist(self, artist_name: str) -> int:
        """删除指定歌手的所有歌曲，返回删除数量"""
        with self._thread_lock:
            before = len(self._songs)
            self._songs = [s for s in self._songs if (s.get("artist") or "").strip() != artist_name.strip()]
            removed = before - len(self._songs)
            if removed > 0:
                self._save_cache()
            return removed

    def remove_album(self, album_name: str, artist_name: str = "") -> int:
        """删除指定专辑（可指定歌手）的所有歌曲，返回删除数量"""
        with self._thread_lock:
            before = len(self._songs)
            if artist_name:
                self._songs = [
                    s for s in self._songs
                    if not (
                        (s.get("album") or "").strip() == album_name.strip()
                        and (s.get("artist") or "").strip() == artist_name.strip()
                    )
                ]
            else:
                self._songs = [s for s in self._songs if (s.get("album") or "").strip() != album_name.strip()]
            removed = before - len(self._songs)
            if removed > 0:
                self._save_cache()
            return removed

    def get_stats(self) -> dict:
        """获取音乐库统计"""
        with self._thread_lock:
            songs_snapshot = list(self._songs)
        artists = set()
        albums = set()
        total_size = 0
        for s in songs_snapshot:
            artist = (s.get("artist") or "").strip()
            if artist:
                artists.add(artist)
            # 空 album 也计入，使用 '未知专辑' 保持与前端一致
            album_name = s.get("album") or "未知专辑"
            albums.add((album_name, s.get("artist", "")))
            total_size += s.get("size", 0)

        return {
            "total_songs": len(songs_snapshot),
            "total_artists": len(artists),
            "total_albums": len(albums),
            "total_size": total_size,
            "music_path": self._music_path,
            "last_scan": self._scan_time,
            "auto_scan_interval": self._auto_scan_interval,
        }

    def get_songs(self, limit: int = 500, offset: int = 0) -> list[dict]:
        """获取歌曲列表（分页）"""
        with self._thread_lock:
            return list(self._songs[offset:offset + limit])

    def search(self, keyword: str) -> list[dict]:
        """搜索本地歌曲"""
        if not keyword:
            return []
        kw = keyword.lower()
        with self._thread_lock:
            songs_snapshot = list(self._songs)
        results = []
        for song in songs_snapshot:
            if (
                kw in (song.get("title") or "").lower()
                or kw in (song.get("artist") or "").lower()
                or kw in (song.get("album") or "").lower()
            ):
                results.append(song)
        return results

    # ===== 定时扫描 =====

    @property
    def auto_scan_interval(self) -> int:
        return self._auto_scan_interval

    def set_auto_scan(self, interval_minutes: int) -> None:
        """设置定时扫描间隔（分钟），0=禁用"""
        import asyncio
        self._auto_scan_interval = interval_minutes
        # 如果已有任务在跑，取消
        if self._auto_scan_task is not None and not self._auto_scan_task.done():
            self._auto_scan_task.cancel()
            self._auto_scan_task = None
        if interval_minutes > 0:
            self._auto_scan_task = asyncio.create_task(self._auto_scan_loop())

    async def _auto_scan_loop(self) -> None:
        """定时扫描循环"""
        import asyncio
        while self._auto_scan_interval > 0:
            await asyncio.sleep(self._auto_scan_interval * 60)
            if self._auto_scan_interval > 0:
                try:
                    await self.scan()
                except Exception as e:
                    logger.warning(f"[Scanner] 定时扫描异常: {e}")
