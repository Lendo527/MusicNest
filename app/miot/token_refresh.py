"""小米 serviceToken 自动刷新管理器（纯被动 401 刷新模式）

设计理念：
- 不主动估算 token 有效期，避免因估算错误导致"掉线"或刷屏日志
- 只在 API 调用返回 401 时被动触发刷新
- 60 秒节流，防止并发 401 引发刷新风暴
- 401 刷新失败时提示用户重新登录

小米 serviceToken 实际有效期约 30 天，passToken 约 1 年。
只要 passToken 未过期，401 时就能自动刷新 serviceToken，无需用户干预。
"""

import asyncio
import logging
import time

from app.config import config
from app.miot.auth import MiAuth

logger = logging.getLogger("musicnest.token_refresh")

# 重登录节流（秒）：60 秒内不重复刷新
RELOGIN_THROTTLE_SEC = 60

_last_relogin_at: float = 0.0

# token 是否已确认失效（passToken 过期等无法自动刷新的情况）
# 置位后 monitor/media_watcher 暂停轮询，避免疯狂 401
_token_invalid: bool = False

# 连续刷新失败计数：达到阈值才判定 token 失效
# 单次网络超时/抖动不应锁死自动刷新（passToken 有效期约 1 年，
# 一次 exchange_token 超时返回 None 就置 invalid 会导致必须手动重登录）；
# passToken 真正过期时连续失败 3 次后停止重试，避免无效请求刷屏
_consecutive_failures: int = 0
TOKEN_INVALID_THRESHOLD = 3

# 并发刷新串行化锁：确保同一时间只有一个刷新在进行，后续 401 复用结果
_refresh_lock: asyncio.Lock = asyncio.Lock()

# client 更新回调列表（token 刷新成功后通知 client 实例同步）
_client_callbacks: list = []


def is_token_invalid() -> bool:
    """检查 token 是否已确认失效（需用户重新登录）"""
    return _token_invalid


def reset_token_invalid() -> None:
    """重置 token 失效状态（用户重新登录后调用）"""
    global _token_invalid, _consecutive_failures
    _token_invalid = False
    _consecutive_failures = 0


def register_client_callback(cb) -> None:
    """注册 client 更新回调，token 刷新成功后调用"""
    if cb not in _client_callbacks:
        _client_callbacks.append(cb)


def unregister_client_callback(cb) -> None:
    """注销 client 更新回调（client.close() 时调用，防止旧回调累积）"""
    try:
        _client_callbacks.remove(cb)
    except ValueError:
        pass


def clear_client_callbacks() -> None:
    """清空所有 client 回调（重新初始化 client 时调用，避免旧回调累积）"""
    global _last_relogin_at
    _client_callbacks.clear()
    # 重置节流时间戳，允许新 client 立即触发刷新
    _last_relogin_at = 0.0


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


def _mark_refresh_failed(reason: str) -> None:
    """记录一次刷新失败：未达阈值时 10 秒后允许重试，达到阈值才判定 token 失效"""
    global _last_relogin_at, _token_invalid, _consecutive_failures
    _consecutive_failures += 1
    # 设为 now - 50，让 60 秒节流变成 10 秒后可重试
    _last_relogin_at = time.time() - 50.0
    if _consecutive_failures >= TOKEN_INVALID_THRESHOLD:
        _token_invalid = True
        logger.warning(
            "[TokenRefresh] 连续 %d 次刷新失败（%s），判定 token 失效，请重新登录",
            _consecutive_failures, reason
        )
    else:
        logger.warning(
            "[TokenRefresh] 刷新失败（%s），第 %d/%d 次，10 秒后自动重试",
            reason, _consecutive_failures, TOKEN_INVALID_THRESHOLD
        )


async def _do_refresh(miauth: MiAuth) -> bool:
    """执行 token 刷新（使用 passToken）

    Returns:
        True 表示刷新成功，False 表示无法刷新（需用户重新登录）
    """
    global _last_relogin_at, _token_invalid, _consecutive_failures

    pass_token = config.get("miot_pass_token", "")
    user_id = config.get("miot_user_id", "")
    ssecurity = config.get("miot_ssecurity", "")

    if not pass_token:
        # 无 passToken 属于确定性失效（未登录/已登出），直接置位
        logger.warning("[TokenRefresh] 无 passToken，无法自动刷新（请重新扫码登录）")
        _last_relogin_at = time.time() - 50.0
        _token_invalid = True
        return False

    try:
        result = await miauth.exchange_token(pass_token, user_id)
        if not result or not result.get("serviceToken"):
            _mark_refresh_failed("passToken 刷新返回空结果")
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
        _token_invalid = False
        _consecutive_failures = 0
        return True

    except Exception as e:
        # 网络超时等瞬时异常：计入连续失败但立即置 invalid，10 秒后可重试
        _mark_refresh_failed(f"刷新异常: {e}")
        return False


async def handle_token_expired(miauth: MiAuth) -> bool:
    """401 过期回调：尝试刷新 token（含 60s 节流 + 并发串行化，避免刷新风暴）

    Returns:
        True 表示刷新成功可重试，False 表示无法刷新（需用户重新登录）
    """
    global _last_relogin_at

    # token 已确认失效（passToken 过期等），直接返回，不再尝试刷新
    # 避免无效刷新占用锁，阻塞其他调用
    if _token_invalid:
        return False

    # 记录调用前的 token，用于判断是否已被其他并发调用刷新
    pre_token = config.get("miot_token", "")

    now = time.time()
    elapsed = now - _last_relogin_at
    if elapsed < RELOGIN_THROTTLE_SEC:
        logger.debug(
            "[TokenRefresh] 401 触发的刷新被节流（距上次 %.1fs < %ds），等待锁复用结果",
            elapsed, RELOGIN_THROTTLE_SEC
        )
        # 节流期间不主动刷新，但等待锁释放后复用其他调用的刷新结果
        async with _refresh_lock:
            current_token = config.get("miot_token", "")
            if current_token and current_token != pre_token:
                logger.info("[TokenRefresh] 节流期间复用其他调用的刷新结果")
                return True
            return False

    # 串行化刷新：第一个 401 拿到锁执行刷新，后续 401 等锁释放后复用结果
    async with _refresh_lock:
        # 拿到锁后检查：等锁期间 token 是否已被其他调用刷新
        current_token = config.get("miot_token", "")
        if current_token and current_token != pre_token:
            logger.info("[TokenRefresh] 复用其他调用的刷新结果")
            return True

        # 占位防并发刷新：锁内设置节流时间戳，防止锁外已通过节流检查的
        # 后续 401 在锁内重复执行 _do_refresh
        _last_relogin_at = time.time()

        logger.info("[TokenRefresh] 检测到 401，尝试用 passToken 刷新 serviceToken...")
        try:
            ok = await _do_refresh(miauth)
            if not ok:
                logger.error("[TokenRefresh] 无法自动刷新 token，请重新扫码登录")
            return ok
        except asyncio.CancelledError:
            # 被 wait_for 等超时取消时重置节流，允许下次 401 立即重试，
            # 避免 stop_all_media 的 3s timeout 取消刷新后导致 60s 假死
            _last_relogin_at = 0.0
            raise
