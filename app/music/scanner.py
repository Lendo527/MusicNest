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


# O2: 目录文件列表缓存，避免 _find_lyrics 对同目录每个文件都遍历一次（O(n²)）
# 缓存带 TTL：无失效机制的缓存会导致新放入的 .lrc 文件永远匹配不到
_lyrics_dir_cache: dict[str, tuple[float, list]] = {}
_LYRICS_CACHE_MAX = 200  # 最多缓存 200 个目录的文件列表
_LYRICS_CACHE_TTL = 60.0  # 缓存有效期（秒）：新增歌词文件最迟 60 秒后可被关联


def _get_dir_siblings(parent: Path) -> list:
    """获取目录下的文件列表（带 TTL 缓存）"""
    parent_str = str(parent)
    now = time.time()
    cached = _lyrics_dir_cache.get(parent_str)
    if cached is not None and now - cached[0] < _LYRICS_CACHE_TTL:
        return cached[1]
    try:
        siblings = list(parent.iterdir())
    except (PermissionError, FileNotFoundError, OSError):
        siblings = []
    # 超上限时淘汰最旧条目（条目有 TTL，此处仅限制内存占用）
    if len(_lyrics_dir_cache) >= _LYRICS_CACHE_MAX:
        _lyrics_dir_cache.pop(next(iter(_lyrics_dir_cache)))
    _lyrics_dir_cache[parent_str] = (now, siblings)
    return siblings


def _find_lyrics(audio_path: Path) -> Optional[str]:
    """查找同名的歌词文件"""
    stem = audio_path.stem
    parent = audio_path.parent
    # 收集需要匹配的 stem（原名 + 去曲号后的清理名）
    stems = [stem]
    clean_stem = _clean_title(stem)
    if clean_stem and clean_stem != stem:
        stems.append(clean_stem)
    stem_lower_set = {s.lower() for s in stems}

    # O2: 使用缓存的目录文件列表，避免 O(n²) 遍历
    siblings = _get_dir_siblings(parent)
    for sibling in siblings:
        if not sibling.is_file():
            continue
        if sibling.suffix.lower() not in LYRICS_EXTENSIONS:
            continue
        if sibling.stem.lower() in stem_lower_set:
            return str(sibling)
    return None


class MusicScanner:
    """音乐库扫描器 - 支持多层文件夹结构"""

    # 缓存文件路径（/data 持久卷，支持环境变量覆盖以便测试和多实例隔离）
    CACHE_FILE = os.environ.get("SCANNER_CACHE_FILE", "/data/songs_cache.json")

    def __init__(self, music_path: str = "/music"):
        self._music_path = music_path
        self._songs: list[dict] = []
        self._scan_time: Optional[float] = None
        self._auto_scan_task: Optional["asyncio.Task"] = None  # type: ignore
        self._auto_scan_interval: int = 0  # 0 = 禁用
        # 使用 asyncio.Lock 避免在 async 函数中阻塞事件循环
        self._lock = asyncio.Lock()  # 直接创建，避免惰性初始化的线程安全问题
        self._thread_lock = threading.RLock()  # 可重入锁，允许嵌套调用 _save_cache
        self._cache_validated: bool = True  # 文件存在性是否已校验（B13: 启动时惰性）
        self._validation_task: Optional["asyncio.Task"] = None  # B13: 后台惰性校验 task
        self._validation_backoff_until: float = 0.0  # 校验失败后的退避截止时间
        self._load_cache()  # 启动时立即加载缓存

    def _get_async_lock(self) -> asyncio.Lock:
        """获取 asyncio.Lock"""
        return self._lock

    def _ensure_validated(self) -> None:
        """B13: 惰性校验 — 首次读取时触发后台 scan 异步校验文件存在性

        缓存加载后 _cache_validated=False，首次调用 iter_songs/get_songs 等读取方法时
        触发后台 scan（不阻塞当前调用），scan 完成后 _cache_validated=True。
        期间读取返回的是未校验缓存（可能含已删除文件），scan 完成后自动刷新。
        """
        if self._cache_validated or self._validation_task is not None:
            return
        # 失败退避：music_path 持续异常（NAS 掉线/挂载点丢失）时，
        # 不再让每次 API 请求都触发一次全量目录遍历
        if time.time() < self._validation_backoff_until:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环（如同步上下文），跳过
            return
        self._validation_task = loop.create_task(self._lazy_validate())

    async def _lazy_validate(self) -> None:
        """B13: 后台惰性校验 — 执行一次 scan 并标记已校验"""
        try:
            logger.info("[Scanner] 启动后台惰性校验（首次读取触发）")
            await self.scan()
            logger.info("[Scanner] 惰性校验完成")
        except Exception as e:
            logger.warning(f"[Scanner] 惰性校验扫描异常: {e}")
            # 5 分钟内不再自动重试，避免故障期每次读取都触发全量扫描
            self._validation_backoff_until = time.time() + 300.0
        finally:
            self._validation_task = None

    def _is_path_safe(self, filepath: Path) -> bool:
        """检查文件路径 resolve() 后是否仍在 music_path 下（防符号链接越界）。

        L1: is_symlink() 仅检测最终路径组件，无法识别中间目录的符号链接，
        因此统一用 resolve() + relative_to() 做归属校验（与 M2 共用）。
        """
        try:
            resolved = filepath.resolve()
            root_resolved = Path(self._music_path).resolve()
            resolved.relative_to(root_resolved)
            return True
        except (ValueError, OSError):
            return False

    def _collect_audio_files(self, root: Path) -> list[Path]:
        """同步收集目录树下所有音频文件（供 to_thread 调用）"""
        audio_files: list[Path] = []
        # root_resolved 缓存到循环外：_is_path_safe 每次调用都会 resolve 音乐根目录，
        # 万首库下是数万次冗余系统调用（NAS 上更是一次次网络往返）
        root_resolved = Path(self._music_path).resolve()
        for filepath in root.rglob("*"):
            if filepath.is_symlink():
                continue  # 跳过符号链接，防止越界
            if not filepath.is_file():
                continue
            # L1: resolve() 后必须仍在 music_path 下，防止中间目录符号链接越界
            try:
                resolved = filepath.resolve()
                resolved.relative_to(root_resolved)
            except (ValueError, OSError):
                continue
            ext = filepath.suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                audio_files.append(filepath)
        return audio_files

    def _load_cache(self) -> bool:
        """从缓存文件加载歌曲列表。成功返回 True

        B13: 文件存在性检查改为惰性 — 大库启动时不阻塞，首次 scan 时再校验。
        """
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

        # L2: 支持 schema 版本号，兼容旧版纯列表格式
        if isinstance(data, list):
            # 旧版格式：纯列表（无版本号）
            logger.info("[Scanner] 检测到旧版缓存格式（无版本号），将进行迁移")
            songs_data = data
        elif isinstance(data, dict):
            version = data.get("version", 0)
            if version != 1:
                logger.warning(f"[Scanner] 缓存版本不匹配 (期望 1, 实际 {version})，将重新扫描")
                self._songs = []
                return False
            songs_data = data.get("songs", [])
            if not isinstance(songs_data, list):
                logger.warning("[Scanner] 缓存 songs 字段格式异常，将重新扫描")
                self._songs = []
                return False
        else:
            logger.warning("[Scanner] 缓存格式异常，将重新扫描")
            self._songs = []
            return False

        # 逐项校验必需字段
        required_keys = {"title", "artist", "filepath"}
        valid_songs = []
        for s in songs_data:
            if isinstance(s, dict) and required_keys.issubset(s.keys()):
                valid_songs.append(s)
            else:
                logger.debug(f"[Scanner] 丢弃无效缓存项: {s}")

        # B13: 文件存在性检查改为惰性 — 大库（1000+歌曲）同步检查会阻塞启动数秒。
        # 启动时只加载缓存不校验文件，首次 scan() 会全量重扫自然过滤不存在的文件。
        # 标记需要惰性校验，get_songs/search 等读取时按需过滤
        self._songs = valid_songs
        self._cache_validated = False  # 标记未校验文件存在性

        # 不在此设置 _scan_time：仅从缓存加载不代表执行过扫描，
        # _scan_time 应只在 scan()/scan_new() 实际扫描后设置
        logger.info(f"[Scanner] 从缓存加载: {len(valid_songs)} 首歌曲（文件存在性将在首次扫描时校验）")
        return True

    def _write_cache_with_snapshot(self, snapshot: list[dict]) -> None:
        """将给定快照写入缓存文件（原子写入）。调用方负责固定 snapshot。"""
        import json
        cache_path = Path(self.CACHE_FILE)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # M4: 使用唯一临时文件名，避免多实例并发写入时冲突
        tmp_path = cache_path.parent / f"songs_cache.{os.getpid()}.{int(time.time()*1000)}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                # L2: 缓存结构带 schema 版本号
                json.dump({"version": 1, "songs": snapshot}, f, ensure_ascii=False, indent=1)
            os.replace(str(tmp_path), str(cache_path))  # 原子替换
            logger.info(f"[Scanner] 缓存已保存: {len(snapshot)} 首歌曲")
        except Exception as e:
            logger.error(f"[Scanner] 缓存保存失败: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
        # M4: 清理可能残留的旧 tmp 文件（只清理超过 5 分钟的，避免影响并发写入）
        try:
            now = time.time()
            for stale in cache_path.parent.glob("songs_cache.*.tmp"):
                try:
                    if stale.stat().st_mtime < now - 300:
                        stale.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _save_cache(self) -> None:
        """保存缓存到文件（原子写入）"""
        with self._thread_lock:
            snapshot = list(self._songs)
        self._write_cache_with_snapshot(snapshot)

    def reload_cache(self):
        """重新从磁盘加载缓存，供其他模块（如 worker）刷新数据。

        H3: 多实例共享同一缓存文件时，外部实例写入后调用此方法刷新本实例内存。
        """
        with self._thread_lock:
            self._load_cache()

    async def _build_song_metadata(self, filepath: Path, root: Path) -> dict:
        """构建歌曲元数据字典（scan 和 scan_new 共用，确保路径解析逻辑一致）

        支持的结构：
            music/Artist/Album/song.mp3                           → artist=Artist, album=Album
            music/Artist/Album/01-song.mp3                        → artist=Artist, album=Album
            music/Artist/Album/Disc 1/01-song.mp3                 → artist=Artist, album=Album
            music/Artist - Album/01 - song.mp3                    → artist=Artist, album=Album
            music/song.mp3                                        → 仅标题
            music/Album/song.mp3                                  → artist=未知, album=Album
        """
        relative = filepath.relative_to(root)
        parts = relative.parts

        filename = filepath.stem
        title = _clean_title(filename)
        # ffprobe 读取标签+时长（一次调用）
        meta_tags = await _probe_file_async(filepath)
        if meta_tags.get("title"):
            title = meta_tags["title"]
        artist = meta_tags.get("artist", "")
        album = meta_tags.get("album", "")
        duration = meta_tags.get("duration", 0)

        # ===== 多层路径解析（仅当标签未提供 artist/album 时作为 fallback）=====
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
            # rglob + 每文件多次 stat 是同步 IO（NAS 上每次 stat 都是一次网络往返），
            # 放入线程执行避免阻塞事件循环（万首库同步遍历会卡住 0.2s 轮询数秒）
            audio_files = await asyncio.to_thread(self._collect_audio_files, root)

            # 第二步：并发解析歌曲信息
            sem = asyncio.Semaphore(10)  # 最多 10 个并发 ffprobe

            async def process_file(filepath):
                async with sem:
                    return await self._build_song_metadata(filepath, root)

            tasks = [process_file(fp) for fp in audio_files]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            songs = [s for s in results if isinstance(s, dict)]

            # 按 artist → album → title 排序
            songs.sort(key=lambda s: (s["artist"].lower(), s["album"].lower(), s["title"].lower()))

            # H2: 赋值与取快照打包进同一 _thread_lock 临界区，
            # 避免与 remove_* 之间的 TOCTOU 竞态（remove_* 可能在赋值与写缓存之间改写 self._songs）
            with self._thread_lock:
                self._songs = songs
                self._scan_time = time.time()
                self._cache_validated = True  # scan 已全量校验文件存在性
                snapshot = list(self._songs)
            # 锁外写文件，snapshot 已固定；json 序列化大库可达数 MB，放入线程避免阻塞事件循环
            await asyncio.to_thread(self._write_cache_with_snapshot, snapshot)
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
                # M2/L1: 校验路径归属，resolve() 后必须仍在 music_path 下，防止越界
                if not self._is_path_safe(p):
                    continue
                if str(p) in existing_paths:
                    continue
                valid_files.append(p)

            # 并发 ffprobe（参照 scan 的并发结构）
            sem = asyncio.Semaphore(10)

            async def _probe_one(p: Path) -> dict:
                async with sem:
                    return await self._build_song_metadata(p, root)

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
                    self._scan_time = time.time()  # 增量扫描也更新扫描时间
                await asyncio.to_thread(self._save_cache)
            return len(new_songs)

    def get_index_by_filepath(self, filepath: str) -> Optional[int]:
        """通过 filepath 查找歌曲索引"""
        self._ensure_validated()  # B13: 惰性校验
        with self._thread_lock:
            for i, s in enumerate(self._songs):
                if s.get("filepath") == filepath:
                    return i
            return None

    def get_song_by_filepath(self, filepath: str) -> Optional[dict]:
        """通过 filepath 获取单首歌的副本（浅拷贝）

        供删除等接口使用：get_songs(limit=5000)[idx] 在库超 5000 首时越界，
        且索引在多个 await 点之间可能失效（TOCTOU），按 filepath 取无此问题。
        """
        self._ensure_validated()  # B13: 惰性校验
        with self._thread_lock:
            for s in self._songs:
                if s.get("filepath") == filepath:
                    return dict(s)
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

    def iter_songs(self) -> list[dict]:
        """返回 songs 的快照副本，供外部安全迭代"""
        self._ensure_validated()  # B13: 惰性校验
        with self._thread_lock:
            # M1: 歌曲为纯平 dict（值均为 str/int），浅拷贝即可隔离外部修改，
            # deepcopy 比浅拷贝慢一个数量级，万首库全量拷贝会阻塞调用方
            return [dict(s) for s in self._songs]

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
        # M5: 不指定歌手时拒绝跨歌手删除同名专辑，避免误删
        if not artist_name or not artist_name.strip():
            logger.warning("[Scanner] remove_album 拒绝在未指定歌手时跨歌手删除同名专辑: %s", album_name)
            return 0
        with self._thread_lock:
            before = len(self._songs)
            self._songs = [
                s for s in self._songs
                if not (
                    (s.get("album") or "").strip() == album_name.strip()
                    and (s.get("artist") or "").strip() == artist_name.strip()
                )
            ]
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
        total_duration = 0
        format_dist = {}  # F11: 格式分布统计
        for s in songs_snapshot:
            artist = (s.get("artist") or "").strip()
            if artist:
                artists.add(artist)
            # L4: 统计专辑数时排除空专辑（原"未知专辑"），避免总数虚高
            album_name = (s.get("album") or "").strip()
            if album_name:
                albums.add((album_name, s.get("artist", "")))
            total_size += s.get("size", 0)
            total_duration += s.get("duration", 0)
            fmt = (s.get("format") or "UNKNOWN").upper()
            format_dist[fmt] = format_dist.get(fmt, 0) + 1

        return {
            "total_songs": len(songs_snapshot),
            "total_artists": len(artists),
            "total_albums": len(albums),
            "total_size": total_size,
            "total_duration": total_duration,
            "format_distribution": format_dist,
            "music_path": self._music_path,
            "last_scan": self._scan_time,
            "auto_scan_interval": self._auto_scan_interval,
        }

    def get_songs(self, limit: int = 500, offset: int = 0) -> list[dict]:
        """获取歌曲列表（分页）"""
        self._ensure_validated()  # B13: 惰性校验
        with self._thread_lock:
            # M1: 浅拷贝即可隔离外部修改（值均为 str/int），避免 deepcopy 性能开销
            return [dict(s) for s in self._songs[offset:offset + limit]]

    def search(self, keyword: str) -> list[dict]:
        """搜索本地歌曲"""
        if not keyword:
            return []
        self._ensure_validated()  # B13: 惰性校验
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
                # M1: 浅拷贝即可隔离外部修改（值均为 str/int）
                results.append(dict(song))
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
