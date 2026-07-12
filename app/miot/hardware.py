"""设备硬件型号与播放 API 能力映射

当前所有设备型号走相同路径（play_music_url + mp3 转码），两个函数恒返回 True。
保留这两个函数作为未来按型号差异化的扩展点，避免在 main.py 散落硬编码判断。
若后续需要为特定型号（如 L05C/L09A 等）启用不同 API，只需在此处修改映射逻辑。
"""


def needs_music_api(hardware: str) -> bool:
    """该型号是否需要使用 play_music_url（而非 play_url）"""
    return True


def needs_mp3(hardware: str) -> bool:
    """该型号是否需要将非 mp3 源转码为 mp3"""
    return True
