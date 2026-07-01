"""小米 serviceToken 自动刷新管理器

参考 songloft-plugin-miot 的防雪崩设计:
- 每 2 小时检查一次 token 有效性
- 剩余有效期 < 3 小时时才刷新，避免无效请求
- 60 秒重登录节流，防止并发刷新风暴
- 401 自动重试回调注入 MinaHTTPClient
"""

import asyncio
import logging
import time
from typing import Optional

from app.config import config
from app.miot.auth import MiAuth

logger = logging.getLogger("musicnest.token_refresh")

# 刷新间隔（秒）：每 2 小时检查一次
TOKEN_REFRESH_INTERVAL_SEC = 2 * 3600
# 刷新阈值（秒）：剩余有效期 < 此值才刷新
TOKEN_REFRESH_THRESHOLD_SEC = 3 * 3600
# serviceToken 标称有效期（秒）
SERVICE_TOKEN_VALID_SEC = 12 * 3600
# 重登录节流（秒）
RELOGIN_THROTTLE_SEC = 60

# 记录 token 创建时间（用于估算有效期）
_token_created_at: float = 0.0
_last_relogin_at: float = 0.0
_refresh_task: Optional[asyncio.Task] = None


def record_token_created() -> None:
    """记录 token 创建时间（登录/刷新成功后调用）"""
    global _token_created_at
    _token_created_at = time.time()
    logger.debug("[TokenRefresh] token 创建时间已记录")


def start_refresh_loop(miauth: MiAuth) -> None:
    """启动 token 刷新定时任务"""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    record_token_created()
    _refresh_task = asyncio.create_task(_refresh_loop(miauth))
    logger.info("[TokenRefresh] token 自动刷新任务已启动 (间隔=%ds)", TOKEN_REFRESH_INTERVAL_SEC)


def stop_refresh_loop() -> None:
    """停止 token 刷新定时任务"""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
    _refresh_task = None


async def _refresh_loop(miauth: MiAuth) -> None:
    """定时检查 token 有效性的循环"""
    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL_SEC)
        try:
            await _check_and_refresh(miauth)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("[TokenRefresh] 刷新检查异常: %s", e, exc_info=True)


async def _check_and_refresh(miauth: MiAuth) -> None:
    """检查 token 是否需要刷新"""
    global _last_relogin_at

    if _token_created_at == 0.0:
        return

    elapsed = time.time() - _token_created_at
    remaining = SERVICE_TOKEN_VALID_SEC - elapsed

    if remaining > TOKEN_REFRESH_THRESHOLD_SEC:
        logger.debug(
            "[TokenRefresh] token 剩余 %ds，大于阈值 %ds，跳过刷新",
            int(remaining), TOKEN_REFRESH_THRESHOLD_SEC
        )
        return

    # 节流：60 秒内不重复刷新
    now = time.time()
    if now - _last_relogin_at < RELOGIN_THROTTLE_SEC:
        logger.warning(
            "[TokenRefresh] 距上次刷新不足 %ds，跳过（节流）",
            RELOGIN_THROTTLE_SEC
        )
        return

    logger.info("[TokenRefresh] token 剩余 %ds，开始刷新...", int(remaining))
    await _do_refresh(miauth)


async def _do_refresh(miauth: MiAuth) -> bool:
    """执行 token 刷新（使用 passToken 降级链）"""
    global _last_relogin_at

    _last_relogin_at = time.time()

    pass_token = config.get("miot_pass_token", "")
    user_id = config.get("miot_user_id", "")
    ssecurity = config.get("miot_ssecurity", "")

    # 降级链：passToken → serviceToken → 无法自动刷新
    new_token = None

    # 方式 1: 用 passToken 刷新（首选）
    if pass_token:
        try:
            result = await miauth.exchange_token(pass_token, user_id)
            if result and result.get("serviceToken"):
                new_token = result
                logger.info("[TokenRefresh] passToken 刷新成功")
        except Exception as e:
            logger.warning("[TokenRefresh] passToken 刷新失败: %s", e)

    # 方式 2: 已有的 serviceToken + ssecurity 可能仍然有效，仅记录探活
    if not new_token and ssecurity:
        logger.info("[TokenRefresh] passToken 刷新失败，保留现有 token（等待 401 自动重试）")
        return False

    if new_token:
        service_token = new_token.get("serviceToken", "")
        new_ssecurity = new_token.get("ssecurity", ssecurity)
        if service_token:
            config.update({
                "miot_token": service_token,
                "miot_ssecurity": new_ssecurity,
            })
            record_token_created()
            logger.info("[TokenRefresh] token 已更新并持久化")
            return True

    logger.warning("[TokenRefresh] 刷新失败，无可用 token")
    return False


async def handle_token_expired(miauth: MiAuth) -> bool:
    """401 过期回调：尝试刷新 token

    Returns:
        True 表示刷新成功可重试，False 表示无法刷新
    """
    logger.info("[TokenRefresh] 检测到 401，尝试刷新 token...")
    ok = await _do_refresh(miauth)
    if ok:
        return True
    # 无法自动刷新，需要用户重新登录
    logger.error("[TokenRefresh] 无法自动刷新 token，请重新扫码登录")
    return False
