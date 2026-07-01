"""音箱硬件 → 播放 API 映射

策略：所有设备统一走 play_music_url（MUSIC 协议）+ 转码 MP3。
- 不分型号白名单，全部走 MUSIC 协议（play_url 对 L05C 等新设备无声）
- 全部转码 MP3（L05C 实测 MUSIC 直推 FLAC 也无反应，MP3 兼容性最好）
"""


def needs_music_api(hardware: str) -> bool:
    return True


def needs_mp3(hardware: str) -> bool:
    return True
