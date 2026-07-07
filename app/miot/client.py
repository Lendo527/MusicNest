"""小爱音箱 API 客户端 - 从 songloft-plugin-miot TS 移植"""

import asyncio
import json
import logging
import secrets
import string
import time
from typing import Any, Optional

import httpx

from app.config import config

logger = logging.getLogger(__name__)

MINA_API_BASE = "https://api2.mina.mi.com"
UBUS_PATH = "/remote/ubus"


def _generate_request_id() -> str:
    """生成请求 ID，格式: app_ios_ + 30位随机字符"""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(30))
    return f"app_ios_{suffix}"


def _build_cookies(user_id: str, service_token: str) -> str:
    """构建 API 请求的 Cookie"""
    return f"userId={user_id}; serviceToken={service_token}; channel=MI_APP_STORE"


def _format_user_agent(device_id: str) -> str:
    """格式化 User-Agent"""
    return f"Android-7.1.1-1.0.0-{device_id}"


class MinaHTTPClient:
    """小爱音箱 HTTP 客户端 - 设备控制、播放管理、对话记录"""

    def __init__(self, user_id: str, service_token: str, device_id: str = "", ssecurity: str = ""):
        self._user_id = user_id
        self._service_token = service_token
        self._device_id = device_id or _generate_device_id()
        self._ssecurity = ssecurity
        self._user_agent = _format_user_agent(self._device_id)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
        )
        # 401 过期回调（由 token_refresh 模块注入）
        self._on_token_expired = None
        # 最近一次 musicnest 自己触发的播放时间戳（per-device，供轨道2区分"自己触发"vs"小爱原生播放"）
        # 在 play_url / play_music_url 成功后立即写入
        self._last_own_play_at: dict[str, float] = {}  # device_id -> timestamp
        # 注册 token 刷新回调，token 刷新成功后自动同步
        from app.miot import token_refresh
        token_refresh.register_client_callback(self._on_token_refreshed)

    def mark_own_play(self, device_id: str = "") -> None:
        """标记最近一次播放是 musicnest 自己触发的（供 media_watcher 判断）"""
        if device_id:
            now = time.time()
            # 清理过期项（超过 300 秒），避免 dict 无限增长
            self._last_own_play_at = {k: v for k, v in self._last_own_play_at.items() if now - v < 300}
            self._last_own_play_at[device_id] = now

    def is_own_play_recent(self, device_id: str = "", within_sec: float = 3.0) -> bool:
        """检查指定设备最近 within_sec 秒内是否自己触发过播放"""
        if not device_id:
            # 强制要求 device_id 非空，避免空 device_id 误判
            return False
        ts = self._last_own_play_at.get(device_id, 0.0)
        return (time.time() - ts) < within_sec

    def _on_token_refreshed(self, new_token: str) -> None:
        """token 刷新回调：同步新 token"""
        self._service_token = new_token
        logger.info("[MIoT] token 已通过回调更新")

    async def close(self) -> None:
        await self._client.aclose()

    def update_token(self, service_token: str, ssecurity: str = "") -> None:
        """更新 token"""
        self._service_token = service_token
        if ssecurity:
            self._ssecurity = ssecurity

    def set_token_expired_callback(self, cb) -> None:
        """注入 401 过期回调"""
        self._on_token_expired = cb

    # ===== 设备列表 =====

    async def get_device_list(self) -> list[dict]:
        """获取小米设备列表"""
        url = f"{MINA_API_BASE}/admin/v2/device_list?master=1"
        result = await self._do_get(url)
        if not result or result.get("code") != 0 or not result.get("data"):
            return []
        return [
            {
                "deviceID": d.get("deviceID", ""),
                "name": d.get("name", ""),
                "miotDID": d.get("miotDID", ""),
                "model": d.get("model", ""),
                "hardware": d.get("hardware", ""),
                "alias": d.get("alias", ""),
                "presence": d.get("presence", ""),
            }
            for d in result["data"]
        ]

    # ===== 播放控制 =====

    async def play_url(self, device_id: str, url: str, keep_light: bool = False) -> bool:
        """通过 player_play_url 播放 URL"""
        message = {"url": url, "type": 1 if keep_light else 2, "media": "app_ios"}
        logger.info(f"[MIoT] play_url: device={device_id[:12]}... url={url[:60]}... type={message['type']}")
        logger.debug("[MIoT] play_url 完整参数: device=%s url=%s message=%s", device_id[:12], url, message)
        result = await self._ubus_request(device_id, "player_play_url", "mediaplayer", message)
        logger.info(f"[MIoT] play_url 响应: {str(result)[:200] if result else 'None'}")
        logger.debug("[MIoT] play_url 完整响应: %s", result)
        ok = result is not None
        if ok:
            self.mark_own_play(device_id)  # 标记为 musicnest 自己触发的播放
        return ok

    async def play_music_url(self, device_id: str, audio_url: str, keep_light: bool = False) -> bool:
        """通过 player_play_music 播放 URL（部分设备型号使用）"""
        audio_id = "1582971365183456177"
        cp_id = "355454500"

        music = {
            "payload": {
                "audio_type": "MUSIC",
                "audio_items": [
                    {
                        "item_id": {
                            "audio_id": audio_id,
                            "cp": {
                                "album_id": "-1",
                                "episode_index": 0,
                                "id": cp_id,
                                "name": "xiaowei",
                            },
                        },
                        "stream": {"url": audio_url},
                    }
                ],
                "list_params": {
                    "listId": "-1",
                    "loadmore_offset": 0,
                    "origin": "xiaowei",
                    "type": "MUSIC",
                },
            },
            "play_behavior": "REPLACE_ALL",
        }

        message = {
            "startaudioid": audio_id,
            "music": json.dumps(music),
        }
        logger.debug("[MIoT] play_music_url: device=%s audio_url=%s message=%s", device_id[:12], audio_url[:80], message)
        result = await self._ubus_request(device_id, "player_play_music", "mediaplayer", message)
        logger.debug("[MIoT] play_music_url 响应: %s", result)
        ok = result is not None
        if ok:
            self.mark_own_play(device_id)  # 标记为 musicnest 自己触发的播放
        return ok

    async def player_play(self, device_id: str) -> bool:
        """播放"""
        message = {"action": "play", "media": "app_ios"}
        return await self._ubus_request(device_id, "player_play_operation", "mediaplayer", message) is not None

    async def player_pause(self, device_id: str) -> bool:
        """暂停"""
        message = {"action": "pause", "media": "app_ios"}
        return await self._ubus_request(device_id, "player_play_operation", "mediaplayer", message) is not None

    async def player_stop(self, device_id: str) -> bool:
        """停止（先暂停再停止）"""
        await self.player_pause(device_id)
        message = {"action": "stop", "media": "app_ios"}
        return await self._ubus_request(device_id, "player_play_operation", "mediaplayer", message) is not None

    async def stop_all_media(self, device_id: str) -> None:
        """停止音箱所有媒体通道的播放（并发发送，最快停止）"""
        tasks = [
            asyncio.wait_for(self.player_pause(device_id), timeout=3.0),
            asyncio.wait_for(self._ubus_request(
                device_id, "player_play_operation", "mediaplayer",
                {"action": "stop", "media": "app_ios"}
            ), timeout=3.0),
            asyncio.wait_for(self._ubus_request(
                device_id, "player_play_operation", "mediaplayer",
                {"action": "stop", "media": "1"}
            ), timeout=3.0),
            asyncio.wait_for(self._ubus_request(
                device_id, "player_play_operation", "mediaplayer",
                {"action": "stop", "media": "2"}
            ), timeout=3.0),
            asyncio.wait_for(self._ubus_request(
                device_id, "player_play_operation", "mediaplayer",
                {"action": "stop", "media": ""}
            ), timeout=3.0),
            asyncio.wait_for(self._ubus_request(
                device_id, "player_play_tts", "mediaplayer", {"text": ""}
            ), timeout=3.0),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if isinstance(r, dict) and r.get("code") == 0)
        if success_count == 0:
            logger.warning(f"[MIoT] stop_all_media 全部失败: device_id={device_id[:12]}...")
        else:
            logger.info(f"[MIoT] stop_all_media 成功 {success_count}/{len(results)}: {device_id[:12]}...")

    async def set_volume(self, device_id: str, volume: int) -> bool:
        """设置音量 (0-100)"""
        v = max(0, min(100, volume))
        message = {"volume": v}
        return await self._ubus_request(device_id, "player_set_volume", "mediaplayer", message) is not None

    async def text_to_speech(self, device_id: str, text: str) -> bool:
        """TTS 文字转语音

        优先使用 MiNA API 的 TTS 端点（/miv1/device/:id/text_to_speech），
        失败时回退到 UBus player_play_tts 方法。
        """
        # 方式 1: MiNA API TTS 端点（统一走 _do_post，自动处理 401 重试）
        url = f"{MINA_API_BASE}/miv1/device/{device_id}/text_to_speech"
        result = await self._do_post(url, {"text": text})
        if result is not None and result.get("code", 0) == 0:
            logger.info("[MIoT] TTS (MiNA) 成功: %s", text[:40])
            return True
        if result is not None:
            logger.warning("[MIoT] TTS (MiNA) 失败: code=%s", result.get("code"))
        else:
            logger.warning("[MIoT] TTS (MiNA) 失败: 无响应")

        # 方式 2: UBus player_play_tts（部分设备支持）
        message = {"text": text}
        result = await self._ubus_request(device_id, "player_play_tts", "mediaplayer", message)
        if result is None:
            logger.warning("[MIoT] TTS (UBus) 失败: 无响应")
            return False
        code = result.get("code", -1)
        if code != 0:
            logger.warning("[MIoT] TTS (UBus) 失败: code=%s data=%s", code, result.get("data", ""))
            return False
        logger.info("[MIoT] TTS (UBus) 成功: %s", text[:40])
        return True

    async def get_player_status(self, device_id: str) -> Optional[dict]:
        """获取播放器状态"""
        result = await self._ubus_request(device_id, "player_get_play_status", "mediaplayer", {})
        # 不打印完整响应（MediaWatcher 每 0.2s 轮询一次，会导致日志爆炸）
        return result

    async def seek(self, device_id: str, position: int) -> bool:
        """跳转到指定位置（秒）"""
        message = {"position": position, "media": "app_ios"}
        return await self._ubus_request(device_id, "player_seek", "mediaplayer", message) is not None

    # ===== 对话记录 =====

    async def get_latest_ask(self, device_id: str, hardware: str, limit: int = 2) -> list[dict]:
        """获取最新对话记录（userprofile API → UBus 回退）"""
        # 方法一：userprofile API
        messages = await self._get_latest_ask_via_userprofile(device_id, hardware, limit)
        if messages:
            return messages

        # 方法二：UBus nlp_result_get
        logger.info(f"[MIoT] userprofile API 无结果，尝试 UBus nlp_result_get...")
        messages = await self.get_latest_ask_by_ubus(device_id)
        if messages:
            logger.info(f"[MIoT] UBus nlp_result_get 返回 {len(messages)} 条记录")
        return messages

    async def _get_latest_ask_via_userprofile(self, device_id: str, hardware: str, limit: int = 2) -> list[dict]:
        """通过 userprofile API 获取对话记录（原版 TS 使用的端点）"""
        timestamp = int(time.time() * 1000)
        api_url = (
            f"https://userprofile.mina.mi.com/device_profile/v2/conversation"
            f"?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit={limit}&deviceId={device_id}"
        )

        headers = {
            "User-Agent": self._user_agent,
            "Cookie": f"userId={self._user_id}; serviceToken={self._service_token}; deviceId={device_id}",
        }

        # 不打印请求日志（ConversationMonitor 每 0.2s 调用一次，会导致日志爆炸）
        try:
            resp = await self._client.get(api_url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[MIoT] userprofile API 返回 {resp.status_code}")
                return []
            outer = resp.json()
        except Exception as e:
            logger.warning(f"[MIoT] userprofile API 异常: {e}")
            return []

        # 外层: {code: 0, message: "Success", data: "{...}"}
        if not isinstance(outer, dict) or outer.get("code") != 0:
            logger.debug("[MIoT] userprofile 外层 code != 0: %s", outer.get("code"))
            return []

        # data 是 JSON 字符串，需要再 parse 一次
        data_str = outer.get("data", "")
        if not isinstance(data_str, str):
            logger.debug("[MIoT] userprofile data 不是字符串")
            return []
        try:
            inner = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("[MIoT] userprofile 内层 JSON 解析失败")
            return []

        records = inner.get("records", []) if isinstance(inner, dict) else []
        if not records:
            logger.debug("[MIoT] userprofile 无对话记录")
            return []

        messages = []
        for record in records:
            if not isinstance(record, dict):
                continue
            query = record.get("query", "")
            ts = record.get("time", 0)
            answer_text = ""
            for ans in record.get("answers", []):
                if isinstance(ans, dict) and ans.get("type") == "TTS":
                    tts = ans.get("tts", {})
                    if isinstance(tts, dict):
                        answer_text = tts.get("text", "")
                    break

            messages.append({
                "timestamp_ms": ts,
                "query": query,
                "answer": answer_text,
            })

        # 不在此处打印每条记录的日志（monitor 每 0.2s 轮询一次，会导致日志爆炸）
        # app/monitor.py 负责在发现新消息时打印日志
        return messages

    async def get_latest_ask_by_ubus(self, device_id: str) -> list[dict]:
        """通过 UBus nlp_result_get 获取对话记录（用于不支持 xiaoai API 的设备）"""
        result = await self._ubus_request(device_id, "nlp_result_get", "mibrain", {})
        if not result or not result.get("data"):
            return []

        try:
            info_str = result["data"].get("info", "")
            if not info_str:
                return []
            info_data = json.loads(info_str)
            nlp_results = info_data.get("result", [])

            messages = []
            for item in nlp_results:
                nlp_str = item.get("nlp", "")
                if not nlp_str:
                    continue
                try:
                    nlp = json.loads(nlp_str)
                    timestamp = int(nlp.get("meta", {}).get("timestamp", 0))
                    for ans in nlp.get("response", {}).get("answer", []):
                        messages.append({
                            "timestamp_ms": timestamp,
                            "query": ans.get("intention", {}).get("query", ""),
                            "answer": ans.get("content", {}).get("to_speak", ""),
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
            return messages
        except Exception as e:
            logger.debug("[MIoT] UBus nlp_result_get 解析失败: %s", e)
            return []

    # ===== 内部方法 =====

    async def _ubus_request(self, device_id: str, method: str, path: str, message: dict) -> Optional[dict]:
        """执行 UBus 请求"""
        url = f"{MINA_API_BASE}{UBUS_PATH}"
        request_id = _generate_request_id()

        form_data = {
            "deviceId": device_id,
            "method": method,
            "path": path,
            "message": json.dumps(message),
            "requestId": request_id,
        }

        # 高频轮询方法（player_get_play_status）跳过 DEBUG 日志，避免日志爆炸
        # （MediaWatcher 每 0.2s 调用一次，1秒 5 条请求日志 + 5 条响应日志 = 10 条/秒）
        is_high_freq = method == "player_get_play_status"
        if not is_high_freq:
            logger.debug(
                "[MIoT] _ubus_request: method=%s path=%s device=%s form_data=%s",
                method, path, device_id[:12], {k: str(v)[:100] for k, v in form_data.items()}
            )
        result = await self._do_post(url, form_data)
        if not is_high_freq:
            logger.debug(
                "[MIoT] _ubus_request 响应: method=%s code=%s",
                method, result.get("code") if result else "None"
            )
        # C2: UBus code != 0 时记录 warning（不返回 None，保留 result 让调用方判断）
        if isinstance(result, dict) and result.get("code", 0) != 0:
            logger.warning("[MIoT] UBus %s 返回错误 code=%s data=%s", method, result.get("code"), result.get("data"))
        return result

    async def _do_get(self, url: str) -> Optional[dict]:
        """执行 GET 请求（含 401 自动重试）"""
        headers = {
            "User-Agent": self._user_agent,
            "Cookie": _build_cookies(self._user_id, self._service_token),
        }
        try:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code == 401:
                # 尝试刷新 token 后重试一次
                if await self._try_refresh_token():
                    headers["Cookie"] = _build_cookies(self._user_id, self._service_token)
                    resp = await self._client.get(url, headers=headers)
                    if resp.status_code == 401:
                        logger.warning("[MIoT] 401 重试后仍失败")
                        return None
                    return resp.json() if resp.text else None
                return None
            return resp.json() if resp.text else None
        except Exception as e:
            logger.warning("[MIoT] %s 请求异常: %s", url, e, exc_info=True)
            return None

    async def _do_post(self, url: str, form_data: dict) -> Optional[dict]:
        """执行 POST 请求（form-urlencoded，含 401 自动重试）"""
        headers = {
            "User-Agent": self._user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": _build_cookies(self._user_id, self._service_token),
        }
        try:
            resp = await self._client.post(url, headers=headers, data=form_data)
            if resp.status_code == 401:
                # 尝试刷新 token 后重试一次
                if await self._try_refresh_token():
                    headers["Cookie"] = _build_cookies(self._user_id, self._service_token)
                    resp = await self._client.post(url, headers=headers, data=form_data)
                    if resp.status_code == 401:
                        logger.warning("[MIoT] 401 重试后仍失败")
                        return None
                    return resp.json() if resp.text else None
                return None
            return resp.json() if resp.text else None
        except Exception as e:
            logger.warning("[MIoT] %s 请求异常: %s", url, e, exc_info=True)
            return None

    async def _try_refresh_token(self) -> bool:
        """401 时尝试刷新 token"""
        if self._on_token_expired is None:
            return False
        try:
            ok = await self._on_token_expired()
            # 无论刷新是否成功，都检查 config 中是否有更新的 token（节流期间可能由其他实例刷新）
            config_token = config.get("miot_token", "")
            if config_token and config_token != self._service_token:
                logger.info("[MIoT] 从 config 同步新 token")
                self._service_token = config_token
                # 节流期间 token 已被其他调用刷新，复用结果
                return True
            if ok:
                logger.info("[MIoT] token 已刷新，重试请求")
            return ok
        except Exception as e:
            logger.error("[MIoT] token 刷新异常: %s", e)
            return False


def _generate_device_id() -> str:
    """生成设备 ID"""
    import uuid
    return str(uuid.uuid4()).replace("-", "")
