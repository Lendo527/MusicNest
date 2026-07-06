"""小米账号 QR 码登录 — 基于 longPolling/loginUrl API

参照 songloft-plugin-miot/src/qrcode/qrcode.ts 实现：
1. GET serviceLogin?sid=mijia → 获取 _sign, qs, callback
2. GET longPolling/loginUrl → 获取二维码图片 URL 和轮询 URL
3. 轮询 lp URL 等待用户扫码确认
4. 用 passToken 交换目标服务 (micoapi) 的 serviceToken
"""

import base64
import hashlib
import json as _json
import logging
import re
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("musicnest.auth")

# ===== 常量 =====

ACCOUNT_BASE_URL = "https://account.xiaomi.com"
LONG_POLLING_URL = "https://account.xiaomi.com/longPolling/loginUrl"

QR_LOGIN_SID = "mijia"       # QR 码登录使用 mijia SID
TARGET_SID = "micoapi"       # 目标服务 SID（最终 serviceToken 对应的服务）

MAX_POLL_ATTEMPTS = 20       # 最大轮询次数


# ===== 工具函数 =====

def _generate_device_id() -> str:
    """生成设备 ID（32 字符 hex，uuid4 去横线）"""
    return str(uuid.uuid4()).replace("-", "")


def _format_user_agent(device_id: str) -> str:
    """格式化 User-Agent（iPhone 米家风格）"""
    return f"APP/xiaomi.smartspehouse/5.21.1 (iPhone/15.6) deviceId/{device_id}"


def _strip_json_prefix(body: str) -> str:
    """去掉小米 API 响应的 JSON 前缀 &&&START&&&"""
    return body.replace("&&&START&&&", "").strip()


def _sha1(message: str) -> bytes:
    """SHA1 哈希"""
    return hashlib.sha1(message.encode()).digest()


def _compute_client_sign(nonce: str, ssecurity: str) -> str:
    """计算 clientSign = base64(sha1("nonce={nonce}&{ssecurity}"))"""
    raw = f"nonce={nonce}&{ssecurity}"
    return base64.b64encode(_sha1(raw)).decode()


def _extract_bigint_field(json_str: str, field: str) -> str:
    """从 JSON 字符串中用正则提取大整数字段（避免 JSON.parse 精度丢失）"""
    match = re.search(rf'"{field}"\s*:\s*(\d+)', json_str)
    return match.group(1) if match else ""


def _try_parse_json(text: str) -> dict:
    """尝试解析 JSON，失败返回空 dict"""
    try:
        return _json.loads(text)
    except (_json.JSONDecodeError, ValueError):
        return {}


# ===== MiAuth =====

class MiAuth:
    """小米 OAuth QR 码登录管理器

    使用小米官方的 longPolling/loginUrl API 获取真实二维码图片，
    而非自己拼 URL 塞进二维码。
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._device_id: str = ""
        self._user_agent: str = ""

    def _ensure_client(self) -> httpx.AsyncClient:
        """确保 HTTP 客户端已初始化"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
                max_redirects=10,
                cookies=httpx.Cookies(),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ===== QR 码登录流程 =====

    async def get_qr_code(self) -> Optional[dict]:
        """
        获取二维码（第一步 & 第二步）

        1. GET serviceLogin?sid=mijia&_json=true → 获取 _sign, qs, callback
        2. GET longPolling/loginUrl → 获取二维码图片 URL 和轮询 URL

        Returns:
            {"qrcode_url": "二维码图片URL", "login_url": "...", "lp_url": "...", "device_id": "..."}
            失败返回 None
        """
        client = self._ensure_client()
        self._device_id = _generate_device_id()
        self._user_agent = _format_user_agent(self._device_id)

        # 重置 cookie jar（新的一次登录流程）
        client.cookies = httpx.Cookies()

        logger.debug("[MiAuth] 开始获取二维码: device_id=%s", self._device_id[:16])

        try:
            # ── Step 1: serviceLogin 获取签名参数 ──
            service_login_url = (
                f"{ACCOUNT_BASE_URL}/pass/serviceLogin"
                f"?sid={QR_LOGIN_SID}&_json=true"
            )

            headers = {
                "User-Agent": self._user_agent,
                "Cookie": f"sdkVersion=3.8.6; deviceId={self._device_id}",
            }

            logger.debug("[MiAuth] Step1: GET serviceLogin url=%s", service_login_url)
            resp1 = await client.get(service_login_url, headers=headers)
            logger.debug(
                "[MiAuth] Step1 响应: status=%d, content_type=%s",
                resp1.status_code, resp1.headers.get("content-type", "")
            )
            json_str1 = _strip_json_prefix(resp1.text)
            login_data = _try_parse_json(json_str1)

            if not login_data:
                logger.error("get_qr_code: failed to parse serviceLogin response")
                return None

            sign = str(login_data.get("_sign", ""))
            qs = str(login_data.get("qs", ""))
            callback = str(login_data.get("callback", ""))

            if not sign or not qs or not callback:
                logger.error(
                    "get_qr_code: missing required params - "
                    f"sign={bool(sign)}, qs={bool(qs)}, callback={bool(callback)}"
                )
                return None

            logger.debug(
                "[MiAuth] Step1 完成: sign=%s qs=%s callback=%s",
                sign[:20], qs[:20], callback[:40]
            )

            # ── Step 2: longPolling/loginUrl 获取二维码 URL 和轮询 URL ──
            params = {
                "_qrsize": "240",
                "qs": qs,
                "sid": QR_LOGIN_SID,
                "_sign": sign,
                "callback": callback,
                "_json": "true",
                "_dc": str(int(time.time() * 1000)),
            }
            qr_req_url = f"{LONG_POLLING_URL}?{urlencode(params)}"

            headers2 = {
                "User-Agent": self._user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            }

            # 携带 Step1 的 cookies
            cookie_header = self._build_cookie_header(qr_req_url)
            if cookie_header:
                headers2["Cookie"] = cookie_header

            logger.debug("[MiAuth] Step2: GET longPolling/loginUrl...")
            resp2 = await client.get(qr_req_url, headers=headers2)
            logger.debug("[MiAuth] Step2 响应: status=%d", resp2.status_code)
            json_str2 = _strip_json_prefix(resp2.text)
            qr_data = _try_parse_json(json_str2)

            if not qr_data:
                logger.error("get_qr_code: failed to parse QR code response")
                return None

            code = int(qr_data.get("code", 0) or 0)
            if code != 0:
                desc = str(qr_data.get("desc", "unknown error"))
                logger.error(f"get_qr_code: QR code request failed: code={code}, desc={desc}")
                return None

            qrcode_image_url = str(qr_data.get("qr", ""))
            login_url = str(qr_data.get("loginUrl", ""))
            lp_url = str(qr_data.get("lp", ""))

            if not lp_url:
                logger.error("get_qr_code: missing lp (long polling) URL")
                return None

            logger.info(f"get_qr_code: QR code obtained, lp_url={lp_url[:80]}...")
            logger.debug(
                "[MiAuth] 二维码获取成功: qrcode_url=%s lp_url=%s",
                qrcode_image_url[:80], lp_url[:80]
            )
            return {
                "qrcode_url": qrcode_image_url or login_url,
                "login_url": login_url,
                "lp_url": lp_url,
                "device_id": self._device_id,
            }

        except httpx.TimeoutException:
            logger.error("get_qr_code: timeout")
            return None
        except Exception as e:
            logger.error(f"get_qr_code: error: {e}")
            return None

    async def poll_qr_result(
        self, lp_url: str, device_id: str
    ) -> Optional[dict]:
        """
        单次扫描轮询（前端驱动重试，本函数不做循环）

        对 lp_url 发起 GET 请求（服务端长轮询，约 30s 超时）。

        Args:
            lp_url: longPolling URL
            device_id: 设备 ID

        Returns:
            {"state": "confirmed"|"waiting"|"expired"|"failed",
             "message": "...",
             "passToken": "...", "userId": "...", "cUserId": "..."}
            None 表示需要继续轮询
        """
        client = self._ensure_client()

        if not self._user_agent:
            self._user_agent = _format_user_agent(device_id)

        try:
            headers = {
                "User-Agent": self._user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            }

            cookie_header = self._build_cookie_header(lp_url)
            if cookie_header:
                headers["Cookie"] = cookie_header

            logger.debug("[MiAuth] 轮询 QR 结果: lp_url=%s", lp_url[:80])
            # 长轮询：服务端会阻塞约 30s，客户端 timeout 略大于服务端（35s）
            resp = await client.get(
                lp_url, headers=headers,
                timeout=httpx.Timeout(35.0, connect=10.0),
            )

            status = resp.status_code
            logger.debug("[MiAuth] QR 轮询响应: status=%d", status)
            if status == 403:
                return {"state": "expired", "message": "二维码已过期 (403)"}
            if status >= 500:
                return {"state": "failed", "message": f"服务端错误: {status}"}
            if status >= 400:
                return {"state": "waiting", "message": f"HTTP {status}"}

            body_text = resp.text
            json_str = _strip_json_prefix(body_text)
            poll_data = _try_parse_json(json_str)

            if not poll_data:
                # 空响应 → 等待扫码
                return {"state": "waiting", "message": "等待扫码..."}

            code = int(poll_data.get("code", 0) or 0)
            logger.debug("[MiAuth] QR 轮询: code=%d", code)
            if code != 0:
                desc = str(poll_data.get("desc", "unknown"))
                logger.warning(f"poll_qr_result: code={code}, desc={desc}")
                return {"state": "expired", "message": f"登录失败: {desc}"}

            pass_token = str(poll_data.get("passToken", ""))
            user_id = str(poll_data.get("userId", ""))
            c_user_id = str(poll_data.get("cUserId", ""))

            logger.debug(
                "[MiAuth] QR 轮询数据: passToken=%s userId=%s cUserId=%s",
                bool(pass_token), bool(user_id), bool(c_user_id)
            )

            if pass_token and user_id:
                logger.info(f"poll_qr_result: QR login successful, userId={user_id}")
                return {
                    "state": "confirmed",
                    "message": "扫码成功",
                    "passToken": pass_token,
                    "userId": user_id,
                    "cUserId": c_user_id,
                }

            # passToken/userId 为空 → 继续等待
            return {"state": "waiting", "message": "等待确认..."}

        except httpx.TimeoutException:
            # 长轮询超时 = 正常，返回 waiting
            return {"state": "waiting", "message": "等待扫码..."}
        except Exception as e:
            logger.error(f"poll_qr_result: error: {e}")
            return {"state": "waiting", "message": f"错误: {e}"}

    async def exchange_token(
        self, pass_token: str, user_id: str, c_user_id: str = ""
    ) -> Optional[dict]:
        """
        使用 passToken 交换目标服务的 serviceToken

        参考 MinaAuth.refreshByPassToken / exchangeServiceToken：
        1. 带 passToken cookie 请求 serviceLogin?sid=micoapi
        2. 获取 location URL、ssecurity、nonce
        3. 计算 clientSign，跟随重定向获取 serviceToken

        Args:
            pass_token: 扫码获取的 passToken
            user_id: 扫码获取的 userId
            c_user_id: 扫码获取的 cUserId

        Returns:
            {"serviceToken": "...", "ssecurity": "...", "userId": "..."}
            失败返回 None
        """
        client = self._ensure_client()

        service_login_url = (
            f"{ACCOUNT_BASE_URL}/pass/serviceLogin"
            f"?sid={TARGET_SID}&_json=true"
        )

        try:
            # ── Step 1: 用 passToken 请求 serviceLogin ──
            cookie_parts = [
                f"passToken={pass_token}",
                f"userId={user_id}",
                f"deviceId={self._device_id}",
                "sdkVersion=3.8.6",
            ]
            if c_user_id:
                cookie_parts.append(f"cUserId={c_user_id}")

            headers = {
                "User-Agent": self._user_agent,
                "Cookie": "; ".join(cookie_parts),
            }

            logger.debug("[MiAuth] exchange_token Step1: GET serviceLogin for %s", TARGET_SID)
            resp = await client.get(service_login_url, headers=headers)
            logger.debug("[MiAuth] exchange_token Step1 响应: status=%d", resp.status_code)
            json_str = _strip_json_prefix(resp.text)
            login_data = _try_parse_json(json_str)

            if not login_data:
                logger.error("exchange_token: failed to parse serviceLogin response")
                return None

            code = int(login_data.get("code", 0) or 0)
            if code != 0:
                desc = str(login_data.get("desc", "unknown"))
                logger.error(f"exchange_token: serviceLogin failed: code={code}, desc={desc}")
                return None

            location = str(login_data.get("location", ""))
            ssecurity = str(login_data.get("ssecurity", ""))
            new_user_id = str(login_data.get("userId", "") or user_id)

            logger.debug(
                "[MiAuth] exchange_token: location=%s ssecurity=%s newUserId=%s",
                location[:60], bool(ssecurity), new_user_id
            )

            if not location:
                logger.error("exchange_token: no location URL returned")
                return None

            # 提取 nonce（从原始 JSON 用正则避免 JSON.parse 精度丢失）
            nonce = (
                _extract_bigint_field(json_str, "nonce")
                or str(login_data.get("nonce", ""))
            )

            # 计算 clientSign
            client_sign = _compute_client_sign(nonce, ssecurity)
            location_with_sign = f"{location}&clientSign={client_sign}"

            logger.info(
                f"exchange_token: nonce={nonce[:20]}..., "
                f"clientSign={client_sign[:20]}..."
            )

            # ── Step 2: 跟随重定向获取 serviceToken ──
            headers3 = {
                "User-Agent": self._user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            }

            # 这个 GET 会跟随多次重定向，最终的 Set-Cookie 包含 serviceToken
            logger.debug("[MiAuth] exchange_token Step2: 跟随重定向获取 serviceToken...")
            await client.get(location_with_sign, headers=headers3)

            # 从 cookie jar 中提取 serviceToken
            service_token = self._get_cookie_value("serviceToken")

            logger.debug(
                "[MiAuth] exchange_token: serviceToken=%s",
                bool(service_token)
            )

            if not service_token:
                logger.error("exchange_token: failed to get serviceToken from cookies")
                return None

            logger.info(
                f"exchange_token: successfully obtained {TARGET_SID} serviceToken"
            )
            return {
                "serviceToken": service_token,
                "ssecurity": ssecurity,
                "userId": new_user_id,
            }

        except httpx.TimeoutException:
            logger.error("exchange_token: timeout")
            return None
        except Exception as e:
            logger.error(f"exchange_token: error: {e}")
            return None

    # ===== 密码登录（备用方案，保持兼容） =====

    async def login_with_password(self, username: str, password: str) -> dict:
        """
        使用账号密码登录（备选方案）

        Args:
            username: 小米账号（手机号/邮箱/小米ID）
            password: 密码

        Returns:
            {"ok": True, "userId": "...", "serviceToken": "...", "ssecurity": "..."}
            或 {"ok": False, "error": "...", "msg": "..."}
        """
        client = self._ensure_client()
        self._device_id = _generate_device_id()
        self._user_agent = _format_user_agent(self._device_id)

        pwd_hash = hashlib.md5(password.encode()).hexdigest().upper()

        # Step 1: 获取 sign
        try:
            sign_resp = await client.get(
                f"{ACCOUNT_BASE_URL}/pass/serviceLogin",
                params={"sid": "passport", "_json": "true"},
                headers={"User-Agent": self._user_agent},
            )
            sign_data = _try_parse_json(_strip_json_prefix(sign_resp.text))
            _sign = sign_data.get("_sign", "")
        except Exception:
            return {"ok": False, "error": "network_error", "msg": "无法连接小米登录服务"}

        # Step 2: 提交登录
        try:
            auth_resp = await client.post(
                f"{ACCOUNT_BASE_URL}/pass/serviceLoginAuth2",
                data={
                    "user": username,
                    "hash": pwd_hash,
                    "_sign": _sign,
                    "sid": "passport",
                    "_json": "true",
                },
                headers={
                    "User-Agent": self._user_agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            result = _try_parse_json(_strip_json_prefix(auth_resp.text))
        except Exception as e:
            return {"ok": False, "error": "network_error", "msg": str(e)}

        if result.get("code") == 0:
            # 从响应或 cookie jar 中提取 passToken（用于后续自动刷新）
            pass_token = str(result.get("passToken", "")) or self._get_cookie_value("passToken")
            return {
                "ok": True,
                "userId": str(result.get("userId", "")),
                "serviceToken": result.get("serviceToken", ""),
                "ssecurity": result.get("ssecurity", ""),
                "passToken": pass_token,
            }

        # 需要验证码
        if result.get("notificationUrl"):
            return {"ok": False, "error": "need_captcha", "msg": "需要验证码验证，请改用扫码登录"}

        return {"ok": False, "error": "auth_failed", "msg": result.get("desc", "账号或密码错误")}

    # ===== 内部方法 =====

    def _get_cookie_value(self, name: str) -> str:
        """从 cookie jar 中提取指定名称的 cookie 值"""
        client = self._ensure_client()
        if not client.cookies or not client.cookies.jar:
            return ""

        for cookie in client.cookies.jar:
            if cookie.name == name:
                return cookie.value
        return ""

    def _build_cookie_header(self, url: str) -> str:
        """构建 Cookie header 字符串"""
        client = self._ensure_client()
        if not client.cookies or not client.cookies.jar:
            return ""

        # 提取 URL 的域名
        url_host = url.split("/")[2].split(":")[0] if "://" in url else ""

        cookies = []
        for cookie in client.cookies.jar:
            # 检查 domain 匹配（httpx 的 CookieJar 在 follow_redirects 时会自动
            # 根据 domain/path 设置 cookie；这里只做简单过滤）
            cookie_domain = getattr(cookie, "domain", "")
            if cookie_domain:
                # 确保 cookie 适用于当前 URL 的域名
                d = cookie_domain.lstrip(".")
                if d and url_host and url_host != d and not url_host.endswith("." + d):
                    continue
            cookies.append(f"{cookie.name}={cookie.value}")

        return "; ".join(cookies) if cookies else ""
