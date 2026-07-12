"""SQLite 数据库操作 - 下载队列 + 歌单同步记录"""

import atexit
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, field
import asyncio
import functools

DB_PATH = Path(os.environ.get("DB_PATH", "/data/musicnest.db"))

# RLock 可重入；WAL 模式下读不阻塞写，因此读函数不再持锁，仅写函数持锁
_lock = threading.RLock()

# 线程局部连接：每个线程复用同一个 sqlite 连接，避免频繁 open/close
_thread_local = threading.local()

logger = logging.getLogger("musicnest.tracker")


def _async_wrap(func):
    """将同步函数包装为异步函数，通过 asyncio.to_thread 避免阻塞事件循环"""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _thread_local.conn = conn
    return conn


def _close_thread_local_conn() -> None:
    """关闭当前线程的局部数据库连接（atexit 注册，进程退出时调用）

    O8: 关闭前执行 wal_checkpoint(PASSIVE)，将 WAL 日志合并到主数据库，
    防止长期运行 WAL 文件膨胀。
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None


atexit.register(_close_thread_local_conn)


@_async_wrap
def init_db():
    """初始化数据库表"""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS download_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL,
                    music_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    cover_url TEXT DEFAULT '',
                    format_type TEXT NOT NULL DEFAULT 'flac',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    error_msg TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_dq_status ON download_queue(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_dq_task_id ON download_queue(task_id)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    music_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'synced',
                    created_at REAL NOT NULL,
                    UNIQUE(source, playlist_id, music_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sh_source_pl ON sync_history(source, playlist_id)
            """)
            # 歌单增量同步锚点表：存储各歌单的 updateTime / trackUpdateTime
            conn.execute("""
                CREATE TABLE IF NOT EXISTS playlist_sync_anchor (
                    source TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    update_time INTEGER DEFAULT 0,
                    track_update_time INTEGER DEFAULT 0,
                    synced_at REAL NOT NULL,
                    PRIMARY KEY (source, playlist_id)
                )
            """)

            # ==== 数据库迁移：基于 PRAGMA user_version 的递进式升级 ====
            # 当前 schema 版本：1。未来新增列时递增并在此追加 ALTER TABLE。
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version < 1:
                # 版本 1：初始迁移点（表结构已由上方 CREATE TABLE IF NOT EXISTS 建立）
                # 示例（未来加列时）：
                #   if current_version < 2:
                #       conn.execute("ALTER TABLE download_queue ADD COLUMN new_col TEXT DEFAULT ''")
                conn.execute("PRAGMA user_version = 1")

            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@dataclass
class DownloadTask:
    id: int = 0
    task_id: str = ""
    source: str = ""
    music_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    format_type: str = "flac"
    status: str = "waiting"
    error_msg: str = ""
    file_path: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "source": self.source,
            "music_id": self.music_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "cover_url": self.cover_url,
            "format_type": self.format_type,
            "status": self.status,
            "error_msg": self.error_msg,
            "file_path": self.file_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@_async_wrap
def add_task(
    task_id: str,
    source: str,
    music_id: str,
    title: str,
    artist: str,
    album: str,
    cover_url: str = "",
    format_type: str = "flac",
) -> DownloadTask:
    """添加下载任务到队列"""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO download_queue
                   (task_id, source, music_id, title, artist, album, cover_url, format_type, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   status='waiting', error_msg='', file_path='', updated_at=excluded.updated_at
                   WHERE download_queue.status = 'error'""",
                (task_id, source, music_id, title, artist, album, cover_url, format_type, now, now),
            )
            conn.commit()
            # 统一通过回查构造返回值，避免 ON CONFLICT 触发 UPDATE 时
            # lastrowid 不指向当前行导致的 id 不正确问题
            row = conn.execute(
                "SELECT * FROM download_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row:
                return DownloadTask(**dict(row))
            return DownloadTask()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def get_waiting_tasks(limit: int = 10) -> List[DownloadTask]:
    """获取等待中的下载任务"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM download_queue WHERE status = 'waiting' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [DownloadTask(**dict(r)) for r in rows]
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def update_task_status(task_id: str, status: str, error_msg: str = "", file_path: str = ""):
    """更新任务状态"""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE download_queue SET status=?, error_msg=?, file_path=?, updated_at=?
                   WHERE task_id=?""",
                (status, error_msg, file_path, now, task_id),
            )
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def get_task_by_id(task_id: str) -> Optional[DownloadTask]:
    """根据 task_id 获取任务"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row:
            return DownloadTask(**dict(row))
        return None
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def update_task_format(task_id: str, format_type: str):
    """更新任务格式（B12: 格式降级时持久化，避免重试时再次尝试原始格式）"""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE download_queue SET format_type=?, updated_at=? WHERE task_id=?",
                (format_type, now, task_id),
            )
            conn.commit()
        finally:
            pass


@_async_wrap
def get_tasks(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> List[DownloadTask]:
    """获取任务列表"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM download_queue WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM download_queue ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [DownloadTask(**dict(r)) for r in rows]
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def get_download_stats() -> dict:
    """获取下载统计"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        waiting = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_queue WHERE status = 'waiting'"
        ).fetchone()["cnt"]
        loading = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_queue WHERE status = 'loading'"
        ).fetchone()["cnt"]
        success = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_queue WHERE status = 'success'"
        ).fetchone()["cnt"]
        error = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_queue WHERE status = 'error'"
        ).fetchone()["cnt"]
        return {
            "waiting": waiting,
            "loading": loading,
            "success": success,
            "error": error,
        }
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def delete_task(task_id: str):
    """删除下载任务"""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM download_queue WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def clear_finished_tasks():
    """清空已完成的任务"""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM download_queue WHERE status IN ('success', 'error')")
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def reset_stale_loading_tasks(timeout_minutes: int = 30) -> int:
    """重置卡在 loading 状态超过指定时长的任务为 waiting

    Args:
        timeout_minutes: 超时分钟数，默认 30 分钟

    Returns:
        重置的任务数量
    """
    now = time.time()
    threshold = now - timeout_minutes * 60
    with _lock:
        conn = _get_conn()
        try:
            cursor = conn.execute(
                "UPDATE download_queue SET status='waiting', error_msg='', updated_at=? "
                "WHERE status='loading' AND updated_at < ?",
                (now, threshold),
            )
            conn.commit()
            count = cursor.rowcount
            if count > 0:
                logger.warning(f"[Tracker] 重置 {count} 个卡死的 loading 任务为 waiting")
            return count
        finally:
            pass  # 线程局部连接复用，不主动关闭


# ===== 同步历史 =====

@_async_wrap
def record_sync(source: str, playlist_id: str, music_id: str):
    """记录同步"""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO sync_history
                   (source, playlist_id, music_id, status, created_at)
                   VALUES (?, ?, ?, 'synced', ?)""",
                (source, playlist_id, music_id, now),
            )
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def is_synced(source: str, playlist_id: str, music_id: str) -> bool:
    """检查是否已同步"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM sync_history WHERE source=? AND playlist_id=? AND music_id=?",
            (source, playlist_id, music_id),
        ).fetchone()
        return row is not None
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def get_synced_ids(source: str, playlist_id: str) -> set:
    """获取已同步的 music_id 集合"""
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT music_id FROM sync_history WHERE source=? AND playlist_id=?",
            (source, playlist_id),
        ).fetchall()
        return {r["music_id"] for r in rows}
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def clear_sync_history(source: str = "", playlist_id: str = ""):
    """清除同步记录"""
    with _lock:
        conn = _get_conn()
        try:
            if source and playlist_id:
                conn.execute(
                    "DELETE FROM sync_history WHERE source=? AND playlist_id=?",
                    (source, playlist_id),
                )
            elif source:
                conn.execute("DELETE FROM sync_history WHERE source=?", (source,))
            else:
                conn.execute("DELETE FROM sync_history")
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def get_playlist_sync_anchor(source: str, playlist_id: str) -> tuple[int, int]:
    """获取歌单同步锚点

    Returns:
        (update_time, track_update_time)，均为毫秒时间戳；无记录返回 (0, 0)
    """
    # 读操作：WAL 模式下读不阻塞写，无需持锁
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT update_time, track_update_time FROM playlist_sync_anchor "
            "WHERE source=? AND playlist_id=?",
            (source, playlist_id),
        ).fetchone()
        if row:
            return int(row["update_time"]), int(row["track_update_time"])
        return 0, 0
    finally:
        pass  # 线程局部连接复用，不主动关闭


@_async_wrap
def set_playlist_sync_anchor(source: str, playlist_id: str,
                              update_time: int, track_update_time: int) -> None:
    """更新歌单同步锚点"""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO playlist_sync_anchor
                   (source, playlist_id, update_time, track_update_time, synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source, playlist_id) DO UPDATE SET
                   update_time=excluded.update_time,
                   track_update_time=excluded.track_update_time,
                   synced_at=excluded.synced_at""",
                (source, playlist_id, int(update_time), int(track_update_time), now),
            )
            conn.commit()
        finally:
            pass  # 线程局部连接复用，不主动关闭
