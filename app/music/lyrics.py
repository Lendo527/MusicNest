"""F3: 歌词同步模块 — 解析 LRC 文件，返回当前播放进度的歌词行

支持标准 LRC 格式：
[mm:ss.xx]歌词内容
[mm:ss.xxx]歌词内容
多时间标签同一行：[00:01.00][00:15.00]重复歌词
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("musicnest.lyrics")

# LRC 时间标签正则：[mm:ss.xx] 或 [mm:ss.xxx]
_LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(lrc_text: str) -> list[tuple[float, str]]:
    """解析 LRC 文本为 (时间秒, 歌词行) 列表

    Returns:
        [(time_sec, lyric_line), ...] 按时间升序排序
    """
    if not lrc_text:
        return []

    lines = []
    for raw_line in lrc_text.splitlines():
        # 跳过空行
        stripped = raw_line.strip()
        if not stripped:
            continue

        # 查找所有时间标签
        times = []
        pos = 0
        while True:
            m = _LRC_TIME_RE.match(stripped, pos)
            if not m:
                break
            mm = int(m.group(1))
            ss = int(m.group(2))
            ms_str = m.group(3) or "0"
            # 补齐到 3 位毫秒
            ms_str = ms_str.ljust(3, "0")
            ms = int(ms_str)
            time_sec = mm * 60 + ss + ms / 1000.0
            times.append(time_sec)
            pos = m.end()

        if not times:
            # 无时间标签的行跳过（可能是 ID 标签如 [ti:xxx] [ar:xxx]）
            continue

        # 提取歌词文本（时间标签后的部分）
        lyric_text = stripped[pos:].strip()
        for t in times:
            lines.append((t, lyric_text))

    # 按时间升序排序
    lines.sort(key=lambda x: x[0])
    return lines


def get_current_lyric(
    lyrics: list[tuple[float, str]],
    current_time: float,
    context_lines: int = 3,
) -> dict:
    """根据当前播放时间返回歌词信息

    Args:
        lyrics: parse_lrc 返回的列表
        current_time: 当前播放秒数
        context_lines: 返回前后几行歌词

    Returns:
        {
            "current_index": int,       # 当前行索引（-1 表示在第一行之前）
            "current_line": str,        # 当前行歌词
            "next_line": str,           # 下一行歌词
            "context": [                # 上下文歌词
                {"index": int, "time": float, "text": str, "is_current": bool}
            ],
            "total_lines": int,
        }
    """
    if not lyrics:
        return {
            "current_index": -1,
            "current_line": "",
            "next_line": "",
            "context": [],
            "total_lines": 0,
        }

    # 找到当前时间对应的歌词行（最后一行 time <= current_time）
    current_idx = -1
    for i, (t, _) in enumerate(lyrics):
        if t <= current_time:
            current_idx = i
        else:
            break

    # 构建上下文
    start = max(0, current_idx - context_lines)
    end = min(len(lyrics), current_idx + context_lines + 1)

    context = []
    for i in range(start, end):
        t, text = lyrics[i]
        context.append({
            "index": i,
            "time": t,
            "text": text,
            "is_current": i == current_idx,
        })

    current_line = lyrics[current_idx][1] if current_idx >= 0 else ""
    next_line = lyrics[current_idx + 1][1] if current_idx + 1 < len(lyrics) else ""

    return {
        "current_index": current_idx,
        "current_line": current_line,
        "next_line": next_line,
        "context": context,
        "total_lines": len(lyrics),
    }


def load_lyrics_file(lrc_path: str) -> Optional[list[tuple[float, str]]]:
    """加载并解析歌词文件

    Args:
        lrc_path: .lrc 文件路径

    Returns:
        parse_lrc 返回的列表，文件不存在或解析失败返回 None
    """
    if not lrc_path:
        return None
    path = Path(lrc_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        # 尝试多种编码
        for encoding in ["utf-8", "gbk", "gb2312", "utf-16"]:
            try:
                text = path.read_text(encoding=encoding)
                return parse_lrc(text)
            except UnicodeDecodeError:
                continue
        # 所有编码都失败，用 utf-8 忽略错误
        text = path.read_text(encoding="utf-8", errors="ignore")
        return parse_lrc(text)
    except Exception as e:
        logger.warning(f"[Lyrics] 加载歌词文件失败: {lrc_path} err={e}")
        return None


def estimate_play_position(play_start_time: float, duration: int, is_playing: bool,
                           pause_elapsed: float = 0.0) -> float:
    """估算当前播放位置（秒）

    Args:
        play_start_time: play_state._play_start_time（monotonic 秒）
        duration: 歌曲总时长（秒）
        is_playing: 是否正在播放
        pause_elapsed: 暂停时已播放的秒数

    Returns:
        当前播放位置（秒），不超过 duration
    """
    if not is_playing or play_start_time <= 0:
        return pause_elapsed
    elapsed = time.monotonic() - play_start_time
    if duration > 0:
        elapsed = min(elapsed, duration)
    return max(0, elapsed)
