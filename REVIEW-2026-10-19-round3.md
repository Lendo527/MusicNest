# MusicNest 第三轮独立审阅报告

- **日期**：2026-10-19
- **审阅基线**：`9381b470cb97c0e8e82279525a6a7da27525e666`（HEAD，v0.0.90）
- **审阅范围**：全库 Python 后端（~20 模块）+ 前端单页 `app/templates/index.html`
- **审阅方式**：逐条回代码核实 v0.0.90 changelog 落地情况 + 8 项专项清单 + 安全/并发/网络健壮性/前端质量补扫
- **分级**：S1/S2（严重）、M1/M2（中等）、L1/L2（轻微），每条含 `文件:行号 / 根因 / 影响 / 修复建议`

---

## 一、v0.0.90 changelog 逐条核对结论

| changelog 声明 | 结论 | 核实位置 |
|---|---|---|
| /api/lyrics/file 加 `_is_safe_path`（路径遍历） | ✅ 已落地 | main.py:3650-3676 双重校验 |
| GET/POST /api/config 返回前脱敏凭据 | ✅ 已落地 | main.py:3948-3965 `_redact_config`，GET/POST 均套用 |
| auth.py `poll_qr_result` 校验 lp_url 域名（SSRF） | ✅ 已落地 | auth.py:284-286 `_is_xiaomi_login_url` |
| config.py 写入后 chmod 0600 | ✅ 已落地 | config.py:147-150 |
| token_refresh 连续 3 次才判定失效 | ✅ 已落地 | token_refresh.py:35-36、98-114 |
| auth.py `login_with_password` passToken=None 修复 | ✅ 已落地 | auth.py:548-549（`or ""` 后 `str()`） |
| auth.py poll_qr_result GET 纳入 `_cookie_lock` | ✅ 已落地 | auth.py:146/297/384/505 |
| monitor/media_watcher/main 新增 `try_claim_query` 原子认领 | ✅ 已落地 | monitor.py:100-117、media_watcher.py:337、main.py:905 |
| media_watcher 状态机 status→0 转回 IDLE / 拦截冷却时间戳 / is_running 校验 done() | ✅ 已落地 | media_watcher.py:275-276、351/366、110-112 |
| monitor is_running 校验 done() | ✅ 已落地 | monitor.py:53 |
| worker per-path 锁 / 60s 心跳 / _process_task 返回 False / 歌单锚点 | ✅ 已落地 | worker.py:544、227-232、422-423；tracker.py:195-197 |
| main logout/_do_restart_monitor 持 `_miot_init_lock` | ✅ 已落地 | main.py:1926、1984-1988、2178-2180 |
| `_enrich_playlist_metadata` 锁内校验歌单身份 | ✅ 已落地 | main.py:1490、调用点 1170 |
| 中文数字支持「百」/ 晚上 12 点→0 点 | ✅ 已落地 | main.py:773、1679 |
| 智能睡眠定时支持循环模式 | ✅ 已落地 | main.py:747 起的 `_parse_cn_number` 与定时器分支 |
| aggregate 酷我仅在有 URL 时 +20 分 | ✅ 已落地 | aggregate.py:89-90 |
| scanner rglob+stat 入线程 / deepcopy→浅拷贝 / get_song_by_filepath | ✅ 已落地 | scanner.py:434、625、516 |
| main 删除/磁盘/封面清理线程化 + 封面临时文件原子替换 | ✅ 已落地 | main.py:2897-2913、2657-2659、2666-2698、2290 |
| scanner 歌词目录缓存 60s TTL + 惰性校验退避 | ✅ 已落地（退避为逻辑，未逐行） | scanner.py:76-95 |
| lyrics mtime/size 缓存 | ✅ 已落地 | lyrics.py:137-167 |
| worker 歌单同步本地搜索入线程 | ✅ 已落地 | worker.py:750 |
| client userprofile 连续失败 5 次冷却 5 分钟 | ✅ 已落地 | client.py:294-308 |

**结论：v0.0.90 changelog 所列修复均已真实落地，未发现「写已修但补丁静默未命中」的情况。** 但下述问题（见第二、三节）不在 changelog 覆盖范围内，属遗留或新暴露。

---

## 二、已核对无误（确认不是 bug 的既有设计）

- `_is_safe_path` 用 `Path.is_relative_to()`（main.py:2852-2866），非字符串前缀匹配，`/music_evil` 前缀攻击不可绕过。
- 歌词文件端点双重 `_is_safe_path`（filepath + 解析出的 lrc_path），符号链接逃逸被 `resolve()` 阻断（main.py:3666、3675）。
- 配置脱敏覆盖 `miot_token/ssecurity/pass_token/_auth_lp_url/_auth_device_id/netease.cookie`，GET 与 POST 返回均脱敏。
- aggregate.py:78/194/131 的 `asyncio.gather` 无 `return_exceptions`，但 `_safe_kuwo_search`/`_safe_netease_search`/`_fetch_netease_url` 内部均 try/except 兜底，单源失败不会拖垮聚合（已核对无误）。
- sqlite 线程局部连接 + RLock + WAL（tracker.py:17-42），写操作持锁、读操作无锁，无跨线程共享连接问题。
- 播放只走酷我 MP3（`mobi.kuwo.cn`），下载聚合全源由用户自选——符合领域不变量，未判为 bug。
- 音量语音路径 clamp（main.py:1390/1392/1394），且 `client.set_volume` 内部 `max(0, min(100, volume))`（client.py:233）——API 传入越界音量也最终被钳制（已核对无误）。
- ffmpeg 封面提取用 `asyncio.create_subprocess_exec` + `wait_for(10s)` + 超时 kill（main.py:2668-2689），不阻塞事件循环、不产生孤儿进程。
- 下载流式写入（`aiter_bytes` + 256KB 缓冲 + `to_thread`），原子 `.part`→`os.replace`，断点续传 Range（worker.py:169-247）。

---

## 三、问题清单

### S1（严重）

#### S1-1 封面 URL 未转义直接拼进 `img src`，第三方可控字段注入 XSS
- **位置**：`app/templates/index.html:3645`、`3672`（`<img src="' + coverSrc + '" ...>`）；`coverSrc = s.cover || '/static/vinyl.svg'` 未过 `escAttr`。
- **根因**：`s.cover` 来自网易云/酷我网关响应，后端透传（netease.py:232 `al.get("picUrl")`、aggregate.py:117 `cover_url: r.cover`）无 `isinstance(str)`/URL 校验；前端 title/artist/album 都转了义，唯独封面 `src` 属性裸拼。
- **影响**：恶意/被篡改的网关返回 `cover = 'x" onerror="fetch(...)" "'` 即突破属性边界执行任意 JS（窃取会话、劫持页面）。叠加无鉴权，攻击面大。
- **修复**：前端 `src="' + escAttr(coverSrc) + '"`；后端对 cover 做 `isinstance(str)` 与 `http(s)://` 前缀校验。

#### S1-2 `/api/config` POST 无键白名单，`config.update(body)` 可篡改 `music_path`
- **位置**：`app/main.py:3981`（`config.update(body)`，body 由前端任意提交）。
- **根因**：POST 端无允许键白名单，也未校验危险键。`music_path` 是 `_is_safe_path`（main.py:2852-2866）的根目录基准。
- **影响**：POST `{"music_path": "/"}` 后，`_is_safe_path` 对任意路径返回 True，删除/扫描接口即可操作整盘文件（任意读 .lrc/.txt、任意删音频/目录），等效于路径遍历 + 任意文件删除。
- **修复**：POST 端维护白名单（允许键），`music_path`/`miot_*`/`netease.cookie` 等危险键单独走受控流程或直接拒绝经通用 `update` 写入。

### S2（严重）

#### S2-1 全 API 无鉴权
- **位置**：`app/main.py:171-182`（唯一中间件仅记录 5xx，无任何鉴权）；Docker 部署将 58092 暴露到局域网。
- **根因**：设计上未加认证（changelog 用「未授权 GET」表述，说明无鉴权是既定事实）。
- **影响**：S1-1、S1-2、S2-2 全部可被局域网内任意客户端直接利用。
- **修复**：至少加一层 token/局域网白名单，或明确绑定 127.0.0.1 并文档化反代鉴权。

#### S2-2 下载接口 `cover_url` 由 body 直传、无域名校验（SSRF）
- **位置**：`app/main.py:4179/4197`、`4222/4249`（`cover_url = body.get("cover_url")` 直接入库）；`app/download/worker.py:250-274`、`552`、`570`（`_download_cover` → `_download_file` 用 httpx 下载，`follow_redirects=True`，无域名校验）。
- **根因**：`cover_url` 是用户可控输入，服务端据此发起任意 GET 并把响应写盘为 cover.jpg。
- **影响**：SSRF——探测内网端口、抓取云元数据（169.254.169.254）等，内容落盘。
- **修复**：`cover_url` 只允许 `http(s)://` 且域名白名单（网易云/酷我 CDN），或改为服务端根据 music_id 重查封面而非信任 body。

#### S2-3 `get_latest_ask` UBus 回退路径仍打 INFO，正常轮询刷屏
- **位置**：`app/miot/client.py:291`（`logger.info(f"[MIoT] UBus nlp_result_get 返回 {len(messages)} 条记录")`）。
- **根因**：v0.0.90 只修了 userprofile「连续失败→冷却」的刷屏，但 UBus 成功分支的 INFO 仍在；代码注释（client.py:287）自述「userprofile 无结果是常态」，即多数设备每次轮询都会落到 UBus 且返回记录。
- **影响**：违反「正常轮询路径禁止 INFO」硬性红线。DEFAULT_POLL_INTERVAL=0.2s → **5 条/秒 = 约 18000 条/小时/设备**。
- **修复**：第 291 行降为 `logger.debug`（新消息的 INFO 已由 monitor.py:301 统一输出）。

### M1（中等）

#### M1-1 首次拉取为空时未置 `_initialized`，启动后首条真实语音指令被当历史吞掉
- **位置**：`app/engine/monitor.py:244-245`（`if not messages: return` 在 248 行 `_initialized` 判断**之前**）。
- **根因**：设备无历史时首次 poll 返回空列表，直接 return，`_initialized[did]` 保持 False；下一条真实指令到达时被当作「首次拉取历史」跳过（251 行置 True 并吞掉）。
- **影响**：启动后第一条真实语音指令静默丢失（语义层；轨道2 拦截可能兜底，但语义层自身有损）。
- **修复**：空结果分支也要置 `_initialized[did] = True`（或把 `_initialized` 判断移到空判断之前）。

### M2（中等）

#### M2-1 代理端点 `follow_redirects=True` 且无目标域名白名单
- **位置**：`app/main.py:2587-2588`（`client.build_request("GET", kuwo_url, ...)` + `send(..., follow_redirects=True)`）。
- **根因**：代理 URL 来自 `_online_urls`（网关搜索结果），非直接用户可控，但未校验域名，且重定向可跳任意主机。
- **影响**：防御纵深缺失——若网关 URL 被污染或存在开放重定向，代理可被打成 SSRF 跳板。
- **修复**：目标 URL 域名校验 + 禁用自动重定向或限制重定向目标域名。

#### M2-2 seek 位置无边界 clamp
- **位置**：`app/main.py:3695`（`api_player_seek` 的 `position` 直传）、`1020`（语音快进 `pos = info["position"] + seconds` 无上界）、`app/miot/client.py:273-275`（`seek` 原样透传 `position`）。
- **根因**：仅 `seek_backward` 做了 `max(0, ...)`，快进与 API 端无上界/类型校验。
- **影响**：负值或超大 position 下发给音箱，可能被拒或行为异常（无安全后果，属健壮性）。
- **修复**：`position = max(0, min(int(position), duration))`，并做 int 类型校验。

### L2（轻微）

#### L2-1 token_refresh 注释与实现语义矛盾
- **位置**：`app/miot/token_refresh.py:161`（注释「计入连续失败但立即置 invalid」，实际 `_mark_refresh_failed` 需连续 3 次才置 invalid）。
- **根因**：v0.0.90 改 3 次阈值后注释未同步。
- **影响**：误导维护者，无运行时影响。
- **修复**：改为「计入连续失败，达到阈值才判定失效」。

#### L2-2 api_player_volume 未显式 clamp（靠 client 兜底）
- **位置**：`app/main.py:3710`（`volume = body.get("volume", 50)` 未 clamp）。
- **根因**：`client.set_volume`（client.py:233）内部有 clamp，实际安全，但与语音路径（1390）不一致。
- **影响**：无安全后果，仅一致性提示。
- **修复**：端点处统一 `max(0, min(100, volume))`，避免未来 client 改动破坏兜底。

---

## 四、汇总

| 级别 | 数量 | 主题 |
|---|---|---|
| S1 | 2 | 封面 XSS、config 键无白名单→任意文件删除 |
| S2 | 3 | 全 API 无鉴权、cover_url SSRF、get_latest_ask INFO 刷屏 |
| M1 | 1 | 首次拉取为空吞首条语音指令 |
| M2 | 2 | 代理重定向无域名白名单、seek 无边界 clamp |
| L2 | 2 | token_refresh 注释 stale、音量 API 未显式 clamp |

**最优先修复顺序**：S1-2（config 白名单）→ S1-1（封面 XSS）→ S2-2（cover_url SSRF）→ S2-3（UBus INFO）→ S2-1（鉴权）→ M1-1 → M2-2 → M2-1 → L2。

**特别说明（应上级要求单独回答）**：`get_latest_ask` 的 INFO 刷屏问题**仍在**，但仅存于 UBus 回退路径（client.py:291）。userprofile 主路径已全部降为 DEBUG。当 userprofile 返回空（代码注释自述是「常态」）或进入 5 分钟冷却期时，每次 0.2s 轮询命中该 INFO，约 **5 条/秒 = 18000 条/小时/设备**。
