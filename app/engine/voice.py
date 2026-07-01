"""
语音指令匹配引擎 — 规则匹配路径
==================================

从 TypeScript 源码移植: songloft-plugin-miot/src/voicecmd/engine.ts

仅负责将用户语音文本匹配到预定义的语音指令，不执行播放逻辑。
播放执行逻辑在 player.py 中实现。

核心策略：跨优先级最长关键词匹配（包含匹配），关键词后的文本作为 argument。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("musicnest.voice")


# ===== 类型定义 =====

@dataclass
class VoiceCommand:
    """语音指令定义

    Attributes:
        type: 指令类型（play_song / play_playlist / set_play_mode / set_volume / next / previous / stop）
        keywords: 触发关键词列表
        param: 附加参数（如 set_play_mode 的 random/single/loop/order，set_volume 的 absolute/up/down）
        enabled: 是否启用
    """
    type: str
    keywords: list[str]
    param: Optional[str] = None
    enabled: bool = True


@dataclass
class MatchResult:
    """口令匹配结果

    Attributes:
        command: 匹配到的语音指令
        keyword: 实际匹配到的关键词
        argument: 关键词之后的文本（去除首尾空白）
    """
    command: VoiceCommand
    keyword: str
    argument: str


# ===== 优先级映射（数字越小优先级越高）=====

_COMMAND_PRIORITY: dict[str, int] = {
    "play_song": 1,
    "play_playlist": 2,
    "set_play_mode": 3,
    "set_volume": 4,
    "next": 5,
    "previous": 6,
    "stop": 7,
}


# ===== 默认语音指令（12 条规则）=====

def _default_commands() -> list[VoiceCommand]:
    """获取默认语音口令配置（12 条）

    翻译自 Go 源码: plugins/songloft-plugin-xiaomi/config/manager.go GetDefaultVoiceCommands()
    """
    return [
        VoiceCommand(
            type="play_playlist",
            keywords=["播放歌单", "放歌单", "播放列表"],
            enabled=True,
        ),
        VoiceCommand(
            type="play_song",
            keywords=["播放歌曲", "放歌曲", "我想听", "播放"],
            enabled=True,
        ),
        VoiceCommand(
            type="set_play_mode",
            keywords=["随机播放", "随机模式"],
            param="random",
            enabled=True,
        ),
        VoiceCommand(
            type="set_play_mode",
            keywords=["单曲循环", "循环播放这首"],
            param="single",
            enabled=True,
        ),
        VoiceCommand(
            type="set_play_mode",
            keywords=["列表循环", "循环播放"],
            param="loop",
            enabled=True,
        ),
        VoiceCommand(
            type="set_play_mode",
            keywords=["顺序播放"],
            param="order",
            enabled=True,
        ),
        VoiceCommand(
            type="set_volume",
            keywords=["设置音量", "音量调到", "音量", "声音", "声音调到"],
            param="absolute",
            enabled=True,
        ),
        VoiceCommand(
            type="set_volume",
            keywords=["大声一点", "声音大一点", "音量大一点"],
            param="up",
            enabled=True,
        ),
        VoiceCommand(
            type="set_volume",
            keywords=["小声一点", "声音小一点", "音量小一点"],
            param="down",
            enabled=True,
        ),
        VoiceCommand(
            type="next",
            keywords=["下一首", "切歌", "换一首", "下一曲"],
            enabled=True,
        ),
        VoiceCommand(
            type="previous",
            keywords=["上一首", "上一曲"],
            enabled=True,
        ),
        VoiceCommand(
            type="stop",
            keywords=["停止播放", "停止", "别播了", "关掉音乐", "关机"],
            enabled=True,
        ),
        VoiceCommand(
            type="create_alarm",
            keywords=["设置闹钟", "新建闹钟", "添加闹钟"],
            enabled=True,
        ),
    ]


# ===== VoiceEngine =====

class VoiceEngine:
    """语音指令匹配引擎

    接收用户语音文本，按优先级和关键词长度进行跨优先级最长关键词匹配。

    Usage::

        engine = VoiceEngine()
        result = engine.handle_message("播放歌单 我喜欢的音乐")
        if result:
            print(f"Matched: {result.command.type} arg={result.argument}")
    """

    def __init__(
        self,
        commands: Optional[list[VoiceCommand]] = None,
    ) -> None:
        """初始化引擎

        Args:
            commands: 自定义语音指令列表。若为 None，使用默认 12 条规则。
        """
        self._commands: list[VoiceCommand] = list(
            commands if commands is not None else _default_commands()
        )
        self._enabled: bool = True

    # ---- 公开属性 ----

    @property
    def enabled(self) -> bool:
        """引擎是否启用"""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """设置引擎启用状态"""
        self._enabled = value
        state = "启用" if value else "停用"
        logger.info(f"[VoiceEngine] 语音引擎已{state}")

    # ---- 指令管理 ----

    @property
    def commands(self) -> list[VoiceCommand]:
        """返回当前所有指令（只读副本）"""
        return list(self._commands)

    def add_command(self, command: VoiceCommand) -> None:
        """添加一条语音指令"""
        self._commands.append(command)

    def remove_command(self, index: int) -> Optional[VoiceCommand]:
        """移除指定索引的语音指令，返回被移除的指令或 None"""
        if 0 <= index < len(self._commands):
            return self._commands.pop(index)
        return None

    def set_commands(self, commands: list[VoiceCommand]) -> None:
        """整体替换指令列表"""
        self._commands = list(commands)

    # ---- 核心方法 ----

    def handle_message(self, query: str) -> Optional[MatchResult]:
        """处理用户语音消息，返回匹配结果或 None

        跨优先级最长关键词匹配策略：

        1. 遍历所有已启用的语音指令
        2. 对每条指令的所有关键词，在 query 中做包含匹配（str.index / str.find）
        3. 在所有命中里，取关键词**字符数最长**者
        4. 长度相同时，取**优先级高**（数字小）者

        防止短关键词（如"播放"）窃取更长关键词（如"播放歌单"）的匹配。

        Args:
            query: 用户语音文本

        Returns:
            MatchResult 或 None（未匹配到任何指令）
        """
        if not self._enabled:
            logger.debug("[VoiceEngine] 引擎未启用，跳过匹配")
            return None

        if not query or not query.strip():
            return None

        enabled_commands = [
            (cmd, _COMMAND_PRIORITY.get(cmd.type, 99))
            for cmd in self._commands
            if cmd.enabled
        ]

        if not enabled_commands:
            logger.debug("[VoiceEngine] 无已启用的指令")
            return None

        logger.debug(
            "[VoiceEngine] 开始匹配: query=%r, enabled_commands=%d",
            query, len(enabled_commands)
        )

        best_match: Optional[MatchResult] = None
        best_kw_len: int = 0
        best_priority: int = 99

        for cmd, priority in enabled_commands:
            for keyword in cmd.keywords:
                idx = query.find(keyword)
                if idx >= 0:
                    kw_len = len(keyword)  # 字符数（Python len 返回码点计数）
                    logger.debug(
                        "[VoiceEngine] 命中: type=%s keyword=%r idx=%d kw_len=%d priority=%d",
                        cmd.type, keyword, idx, kw_len, priority
                    )
                    if kw_len > best_kw_len or (
                        kw_len == best_kw_len and priority < best_priority
                    ):
                        best_kw_len = kw_len
                        best_priority = priority
                        best_match = MatchResult(
                            command=cmd,
                            keyword=keyword,
                            argument=query[idx + len(keyword):].strip(),
                        )
                else:
                    logger.debug(
                        "[VoiceEngine] 未命中: type=%s keyword=%r",
                        cmd.type, keyword
                    )

        if best_match:
            logger.info(
                "[VoiceEngine] [Rule] → 命中: type=%s keyword=%r argument=%r",
                best_match.command.type,
                best_match.keyword,
                best_match.argument,
            )
            logger.debug(
                "[VoiceEngine] best_match详情: type=%s priority=%d kw_len=%d",
                best_match.command.type, best_priority, best_kw_len
            )
        else:
            pass  # 未匹配到任何指令，不输出日志

        return best_match

    def match_command(self, query: str) -> Optional[MatchResult]:
        """match_command 是 handle_message 的别名（保持与 TS 版命名一致）"""
        return self.handle_message(query)
