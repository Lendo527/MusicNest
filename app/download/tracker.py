"""SQLite 数据库操作 - 下载队列 + 歌单同步记录"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, field

DB_PATH = Path(__file__).parent.parent / "musicnest.db"

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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

            conn.commit()
        finally:
            conn.close()


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
            cursor = conn.execute(
                """INSERT OR IGNORE INTO download_queue
                   (task_id, source, music_id, title, artist, album, cover_url, format_type, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)""",
                (task_id, source, music_id, title, artist, album, cover_url, format_type, now, now),
            )
            conn.commit()
            if cursor.rowcount > 0:
                return DownloadTask(
                    id=cursor.lastrowid,
                    task_id=task_id,
                    source=source,
                    music_id=music_id,
                    title=title,
                    artist=artist,
                    album=album,
                    cover_url=cover_url,
                    format_type=format_type,
                    status="waiting",
                    created_at=now,
                    updated_at=now,
                )
            # 已存在，返回已有记录
            row = conn.execute(
                "SELECT * FROM download_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row:
                return DownloadTask(**dict(row))
            return DownloadTask()
        finally:
            conn.close()


def get_waiting_tasks(limit: int = 2) -> List[DownloadTask]:
    """获取等待中的下载任务"""
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM download_queue WHERE status = 'waiting' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [DownloadTask(**dict(r)) for r in rows]
        finally:
            conn.close()


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
            conn.close()


def get_task_by_id(task_id: str) -> Optional[DownloadTask]:
    """根据 task_id 获取任务"""
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM download_queue WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row:
                return DownloadTask(**dict(row))
            return None
        finally:
            conn.close()


def get_tasks(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> List[DownloadTask]:
    """获取任务列表"""
    with _lock:
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
            conn.close()


def get_download_stats() -> dict:
    """获取下载统计"""
    with _lock:
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
            conn.close()


def delete_task(task_id: str):
    """删除下载任务"""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM download_queue WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()


def clear_finished_tasks():
    """清空已完成的任务"""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM download_queue WHERE status IN ('success', 'error')")
            conn.commit()
        finally:
            conn.close()


# ===== 同步历史 =====

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
            conn.close()


def is_synced(source: str, playlist_id: str, music_id: str) -> bool:
    """检查是否已同步"""
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM sync_history WHERE source=? AND playlist_id=? AND music_id=?",
                (source, playlist_id, music_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def get_synced_ids(source: str, playlist_id: str) -> set:
    """获取已同步的 music_id 集合"""
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT music_id FROM sync_history WHERE source=? AND playlist_id=?",
                (source, playlist_id),
            ).fetchall()
            return {r["music_id"] for r in rows}
        finally:
            conn.close()


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
            conn.close()
