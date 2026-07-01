"""歌曲索引 + 三级模糊搜索

从 TypeScript 源码 songloft-plugin-miot/src/indexing/manager.ts 移植。
"""

from dataclasses import dataclass, field
from typing import Optional


# ===== 类型定义 =====

@dataclass
class SongEntry:
    """索引中的歌曲条目"""
    title: str
    artist: str
    album: str
    filepath: str
    title_lower: str = ""   # 预处理小写
    artist_lower: str = ""  # 预处理小写

    def __post_init__(self):
        if not self.title_lower:
            self.title_lower = self.title.lower()
        if not self.artist_lower:
            self.artist_lower = self.artist.lower()


@dataclass
class ScoredSong:
    """带评分的搜索结果"""
    song: SongEntry
    score: float
    match_type: str  # 'exact', 'substring', 'fuzzy'


# ===== 模糊搜索算法 =====

def _char_count(s: str) -> int:
    """Unicode 字符数（不是字节数）"""
    return len(s)


def levenshtein_distance(a: str, b: str) -> int:
    """编辑距离（Levenshtein Distance），支持 Unicode

    使用两行滚动数组优化空间，与 TS 版本行为一致。
    """
    ra = list(a)
    rb = list(b)
    la = len(ra)
    lb = len(rb)

    if la == 0:
        return lb
    if lb == 0:
        return la

    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)

    for i in range(1, la + 1):
        curr[0] = i
        for j in range(1, lb + 1):
            cost = 0 if ra[i - 1] == rb[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,   # 删除
                prev[j] + 1,       # 插入
                prev[j - 1] + cost,  # 替换
            )
        # 交换行
        prev, curr = curr, prev

    return prev[lb]


def similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度 (0.0 ~ 1.0)

    similarity = 1 - distance / max(len(a), len(b))
    """
    al = list(a.lower())
    bl = list(b.lower())
    max_len = max(len(al), len(bl))
    if max_len == 0:
        return 1.0
    dist = levenshtein_distance(a.lower(), b.lower())
    return 1.0 - dist / max_len


def fuzzy_score(keyword: str, candidate: str) -> float:
    """三级模糊搜索评分

    1. 精确匹配（忽略大小写）→ 得分 100
    2. 子串包含匹配：
       - 候选项包含关键词：50 + 1/字符长度
       - 关键词包含候选项：40 + 1/字符长度
    3. Levenshtein 编辑距离 → similarity > 0.5 时得分 similarity × 30

    Returns:
        得分，0 表示不匹配
    """
    if not keyword or not candidate:
        return 0.0

    keyword_lower = keyword.lower()
    candidate_lower = candidate.lower()

    # 第一级：精确匹配
    if candidate_lower == keyword_lower:
        return 100.0

    # 第二级：包含匹配
    if keyword_lower in candidate_lower:
        rune_len = _char_count(candidate)
        return 50.0 + 1.0 / rune_len if rune_len > 0 else 50.0

    # 第二级变体：关键词包含候选项
    if candidate_lower in keyword_lower:
        rune_len = _char_count(candidate)
        return 40.0 + 1.0 / rune_len if rune_len > 0 else 40.0

    # 第三级：编辑距离模糊匹配
    sim = similarity(keyword, candidate)
    if sim > 0.5:
        return sim * 30.0

    return 0.0


# 最低匹配分数阈值 — 低于此分数的模糊匹配视为无效
_MIN_MATCH_SCORE = 40.0


def _score_song_match(query: str, song_title: str, song_artist: str) -> tuple[float, str]:
    """计算歌曲综合匹配得分，联合评估标题和歌手

    防误匹配逻辑：解决"林俊杰的她说"误匹配已入库歌手"林俊杰"的其他歌曲
    （如"小酒窝"）的问题。当查询词仅匹配歌手而标题完全未命中，且查询词明显
    长于歌手名时（说明用户同时指定了歌名），判定为未命中，返回 0 分。

    Returns:
        (score, match_type)
    """
    title_score = fuzzy_score(query, song_title)
    artist_score = fuzzy_score(query, song_artist)

    # 标题高质量命中 — 直接返回
    if title_score >= _MIN_MATCH_SCORE:
        return title_score, _classify_match_type(query, song_title)

    # 防误匹配：仅匹配到歌手但标题完全未命中
    if artist_score >= _MIN_MATCH_SCORE and title_score == 0:
        query_len = _char_count(query)
        artist_len = _char_count(song_artist)
        if query_len > artist_len + 1:
            return 0.0, ""

    # 取标题和歌手中的较高分
    if title_score >= artist_score:
        return title_score, _classify_match_type(query, song_title) if title_score > 0 else ""
    else:
        return artist_score, _classify_match_type(query, song_artist) if artist_score > 0 else ""


def _classify_match_type(keyword: str, candidate: str) -> str:
    """根据得分方式判断匹配类型"""
    if not keyword or not candidate:
        return ""
    kw = keyword.lower()
    ca = candidate.lower()
    if ca == kw:
        return "exact"
    if kw in ca or ca in kw:
        return "substring"
    return "fuzzy"


# ===== 索引类 =====

class SongIndex:
    """歌曲索引 - 构建索引并提供三级模糊搜索"""

    def __init__(self):
        self.songs: list[SongEntry] = []

    def build(self, songs: list[dict]) -> None:
        """从 scanner.py 输出的歌曲列表构建索引

        Args:
            songs: 歌曲字典列表，每项包含 title, artist, album, filepath
        """
        self.songs = [
            SongEntry(
                title=s.get("title", ""),
                artist=s.get("artist", ""),
                album=s.get("album", ""),
                filepath=s.get("filepath", ""),
            )
            for s in songs
        ]

    def search(self, keyword: str) -> list[ScoredSong]:
        """模糊搜索歌曲（匹配标题或歌手）

        Args:
            keyword: 搜索关键词

        Returns:
            按得分降序排列的匹配结果列表
        """
        if not keyword or not keyword.strip():
            return []

        query = keyword.strip()
        scored: list[ScoredSong] = []

        for song in self.songs:
            score, match_type = _score_song_match(query, song.title, song.artist)
            if score > 0:
                scored.append(ScoredSong(
                    song=song,
                    score=score,
                    match_type=match_type,
                ))

        # 按得分降序排列
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored

    def find_best(self, keyword: str) -> Optional[ScoredSong]:
        """取最高分的匹配结果

        Args:
            keyword: 搜索关键词

        Returns:
            得分最高的匹配结果，无结果返回 None
        """
        results = self.search(keyword)
        return results[0] if results else None

    def search_top_n(self, keyword: str, n: int = 5) -> list[ScoredSong]:
        """取前 N 个匹配结果

        Args:
            keyword: 搜索关键词
            n: 返回结果数量上限

        Returns:
            前 N 个匹配结果
        """
        results = self.search(keyword)
        return results[:n]

    def __len__(self) -> int:
        return len(self.songs)

    def __bool__(self) -> bool:
        return len(self.songs) > 0
