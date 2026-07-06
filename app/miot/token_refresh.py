"""小米 serviceToken 自动刷新管理器（纯被动 401 刷新模式）

设计理念：
- 不主动估算 token 有效期，避免因估算错误导致"掉线"或刷屏日志
- 只在 API 调用返回 401 时被动触发刷新
- 60 秒节流，防止并发 401 引发刷新风暴
- 401 刷新失败时提示用户重新登录

小米 serviceToken 实际有效期约 30 天，passToken 约 1 年。
只要 passToken 未过期，401 时就能自动刷新 serviceToken，无需用户干预。
"""

import logging
import time

from app.config import config
from app.miot.auth import MiAuth

logger = logging.getLogger("musicnest.token_refresh")

# 重登录节流（秒）：60 秒内不重复刷新
RELOGIN_THROTTLE_SEC = 60

_last_relogin_at: float = 0.0

# client 更新回调列表（token 刷新成功后通知 client 实例同步）
_client_callbacks: list = []


def register_client_callback(cb) -> None:
    """注册 client 更新回调，token 刷新成功后调用"""
    _client_callbacks.append(cb)


def clear_client_callbacks() -> None:
    """清空所有 client 回调（重新初始化 client 时调用，避免旧回调累积）"""
    _client_callbacks.clear()


def start_refresh_loop(miauth: MiAuth) -> None:
    """兼容接口：纯被动模式下无主动刷新循环，直接返回

    保留此函数是为了兼容 main.py 的调用（登录成功后调用）。
    被动模式下，token 刷新完全依赖 401 触发，无需主动轮询。
    """
    logger.info("[TokenRefresh] 已启用纯被动 401 刷新模式（无主动轮询）")


def stop_refresh_loop() -> None:
    """兼容接口：纯被动模式下无主动刷新循环，直接返回"""
    pass


def record_token_created() -> None:
    """兼容接口：纯被动模式下不记录创建时间，直接返回"""
    pass


async def _do_refresh(miauth: MiAuth) -> bool:
    """执行 token 刷新（使用 passToken）

    Returns:
        True 表示刷新成功，False 表示无法刷新（需用户重新登录）
    """
    global _last_relogin_at

    pass_token = config.get("miot_pass_token", "")
    user_id = config.get("miot_user_id", "")
    ssecurity = config.get("miot_ssecurity", "")

    if not pass_token:
        logger.warning("[TokenRefresh] 无 passToken，无法自动刷新（请重新扫码登录）")
        _last_relogin_at = time.time()
        return False

    try:
        result = await miauth.exchange_token(pass_token, user_id)
        if not result or not result.get("serviceToken"):
            logger.warning("[TokenRefresh] passToken 刷新返回空结果（passToken 可能已过期，请重新登录）")
            _last_relogin_at = time.time()
            return False

        service_token = result["serviceToken"]
        new_ssecurity = result.get("ssecurity", ssecurity)
        config.update({
            "miot_token": service_token,
            "miot_ssecurity": new_ssecurity,
        })
        # 通知所有注册的 client 回调同步新 token
        for cb in _client_callbacks:
            try:
                cb(service_token)
            except Exception as e:
                logger.warning("[TokenRefresh] client 回调失败: %s", e)
        logger.info("[TokenRefresh] passToken 刷新成功，token 已更新并持久化")
        _last_relogin_at = time.time()
        return True

    except Exception as e:
        logger.warning("[TokenRefresh] passToken 刷新异常: %s", e)
        _last_relogin_at = time.time()
        return False


async def handle_token_expired(miauth: MiAuth) -> bool:
    """401 过期回调：尝试刷新 token（含 60s 节流，避免短时间内重复刷新风暴）

    Returns:
        True 表示刷新成功可重试，False 表示无法刷新（需用户重新登录）
    """
    now = time.time()
    elapsed = now - _last_relogin_at
    if elapsed < RELOGIN_THROTTLE_SEC:
        logger.debug(
            "[TokenRefresh] 401 触发的刷新被节流（距上次 %.1fs < %ds）",
            elapsed, RELOGIN_THROTTLE_SEC
        )
        return False

    logger.info("[TokenRefresh] 检测到 401，尝试用 passToken 刷新 serviceToken...")
    ok = await _do_refresh(miauth)
    if not ok:
        logger.error("[TokenRefresh] 无法自动刷新 token，请重新扫码登录")
    return ok
