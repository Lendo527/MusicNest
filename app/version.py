"""版本号定义 - 项目的唯一版本真相源

pyproject.toml 通过 hatchling dynamic version 从本文件读取版本号；
app/main.py 和 build_oci.py 也从本文件读取；
前端 index.html 通过 /api/config API 动态获取版本号。

每次发版只需修改本文件的 __version__ 和下方版本历史注释。

版本历史：
  0.0.37 - 手动深度审阅修复（跨模块联动一致性 + 修复后新引入问题）：
           main: 封面提取ffmpeg超时未kill子进程(孤儿进程修复)；
           main: _init_miot_client重新登录时关闭旧client(防httpx连接池泄漏)；
           main: _init_miot_client清空token_refresh旧回调(防callback累积)；
           main: lifespan shutdown调用kuwo.close_client()(防连接池泄漏)；
           main: _bg_scan_task初始化为None(消除NameError隐患)；
           worker: 移除add_task返回值死代码(ON CONFLICT已处理重置)；
           token_refresh: 新增clear_client_callbacks()函数；
  0.0.36 - 全代码库深度审阅修复（CRITICAL 12 + HIGH 19 + MEDIUM 30+）：
           main: 流式转码finally加proc.kill()防僵尸进程；
           main: 删除歌曲改按filepath定位(消除误删风险)；
           main: scanner._songs全部改用iter_songs()/remove_artist/remove_album带锁接口；
           main: API下一曲SINGLE模式强制切下一首(与语音指令一致)；
           main: 封面提取改asyncio子进程不阻塞事件循环；
           main: rmtree前对真实路径调_is_safe_path校验(安全漏洞)；
           main: _check_miot用asyncio.Lock防并发初始化泄漏；
           main: 全局异常不向客户端泄漏str(exc)；
           main: lifespan存储后台任务引用+关闭时取消；
           main: fire-and-forget任务统一_create_background_task加异常回调；
           main: _alarm_loop外层try/except防静默死亡；
           main: _parse_cn_number支持百位解析；
           main: .cancel()改用_safe_cancel(定义+7处替换)；
           scanner: 读取方法全部加_thread_lock保护+返回快照；
           scanner: scan_new循环体加try/except+并发Semaphore(10)；
           scanner: 缓存schema校验+__init__中_load_cache；
           scanner: _save_cache原子写入+持锁快照；
           scanner: rglob跳过symlink防越界；
           scanner: 新增iter_songs/remove_by_filepath/remove_artist/remove_album；
           scanner: _thread_lock改RLock防嵌套死锁；
           config: _save原子写入(tmp+os.replace)+锁内完成；
           config: __init__中_load/_save包try/except防启动崩溃；
           config: _deep_merge递归合并嵌套配置；
           config: get/get_all返回深拷贝防外部污染；
           kuwo: _python_to_json改用ast.literal_eval(修复含撇号歌名崩溃)；
           kuwo: DURATION解析加try/except+gather加return_exceptions；
           kuwo: 歌手搜索字段大小写fallback；
           kuwo: 封面URL精确替换re.sub；
           kuwo: 新增close_client()供应用关闭调用；
           netease: 移除搜索关键词过严校验(导致0结果)；
           netease: /song/url回退保持type=flac参数；
           netease: _build_quality_formats/_parse_song类型校验；
           netease: _netease_request单client复用遍历网关；
           worker: 下载失败清理partial文件+大小校验(>1KB)；
           worker: netease下载传cookie(FLAC/Hi-Res必需)；
           worker: 启动时重置卡死loading任务；
           worker: kuwo优先query_song_by_id精确查询；
           worker: 歌单同步歌手匹配改集合交集(避免子串误判)；
           worker: 循环内异常容错(单首失败不中断整批)；
           tracker: add_task改UPSERT重置error/loading状态；
           tracker: 新增reset_stale_loading_tasks函数；
           tracker: _get_conn改线程局部连接复用；
           monitor: _poll_all改asyncio.gather并发轮询(修复0.2s间隔失效)；
           monitor: 新增get_last_unhandled_query方法；
           media_watcher: start()首轮状态预热(防重启误拦)；
           media_watcher: stop_all_media改await(消除竞态)；
           media_watcher: 拦截后mark_query_handled(防双轨重复触发)；
           media_watcher: _watch_all_devices改asyncio.gather并发；
           client: _last_own_play_at改per-device dict(修复跨设备误判)；
           client: _try_refresh_token始终从config同步新token；
           client: stop_all_media超时缩短3s+失败计数；
           client: 注册token_refresh回调自动更新token；
           token_refresh: 新增register_client_callback机制；
           token_refresh: _last_relogin_at刷新成功后置位(失败5s退避)；
           player: pause记录暂停时刻+resume补偿_play_start_time(修复提前切歌)；
           player: smart_resume区分status=-1(继续轮询)和status=0(重播)；
           player: _auto_next_after异常转STOPPED(防卡死)；
           auth: _build_cookie_header域名匹配改正确子域判断；
           auth: poll_qr_result 5xx返回failed(防无限轮询)；
           index: playlistPlaySong用缓存playlist(修复播1首专辑显示全库)；
           index: renamePlaylist/playPlaylistSongs改data-*属性(修复XSS)；
           index: openArtist/openAlbum改data-*属性(修复XSS)；
           index: t.format_type/task_id/error_msg防空(防整表崩溃)；
           index: 统计文本统一为"共XX首/个/张"格式；
           index: currentPlayingIdx停止播放后重置(防残留高亮)；
           index: Modal改getOrCreateInstance(防实例堆积)；
           index: _prevDownloadStatus自动修剪(防内存增长)；
           index: deleteSong改按filepath(配合后端)；
           index: escHtml(m.time)防注入
  0.0.35 - 下载模块 11 个 bug 修复：
           worker: 下载失败清理 partial 文件 + 文件大小校验(>1KB)；
           worker: netease 下载链接传 cookie（FLAC/Hi-Res 必需）；
           worker: 启动时重置卡死 loading 任务(reset_stale_loading_tasks)；
           worker: kuwo 优先 query_song_by_id 精确查询+标题归一化匹配；
           worker: 歌单同步歌手匹配改集合交集(避免子串误匹配)；
           worker: add_task 后检查 error/loading 状态并重置；
           worker: 歌单同步每首歌曲 try/except 容错；
           worker: MusicScanner 移到 while 循环外(避免重复初始化)；
           tracker: 新增 reset_stale_loading_tasks 函数；
           tracker: add_task 改 ON CONFLICT UPSERT(重置 error/loading)；
           tracker: _get_conn 改线程局部连接复用(避免频繁 open/close)
  0.0.34 - 第5批12个问题修复：
           播放列表右上角文本可见性修复(text-muted→#b0b6c1)；
           播放列表显示play_state.playlist而非全库(get_state_dict添加playlist字段)；
           kuwo歌手搜索添加artist_id兜底头像URL；
           kuwo专辑搜索字段名增加多种尝试(albumid/id等)；
           歌单监听列表td垂直居中(vertical-align:middle)；
           歌单同步前检查本地是否已有(scanner.search匹配跳过下载)；
           网易云搜索0结果继续下一网关+下载URL添加/song/url回退端点；
           歌手/专辑/歌曲列表统计文本统一为"共XX首/个/张"格式；
           扫描统计数字字号统一(font-size:1.1rem;color:var(--accent))；
           本地歌曲转码改为流式边转边播(asyncio subprocess+StreamingResponse)；
           下一曲在SINGLE模式强制切到下一首(不再返回None)；
           拦截失败副作用修复(下一曲bug修复后不再出现)
  0.0.33 - 第4批9个问题修复：
           播放列表使用filteredCache+_idx高亮匹配(currentPlayingIdx)；
           列表高度调整为calc(100vh-200px)/calc(100vh-260px)靠近播放器；
           kuwo.py search函数JSON解析修复(先json.loads再降级_python_to_json)；
           前后端彻底删除TTS代码(config/main/index.html全部清除)；
           音量控制添加中文数字解析(_parse_cn_number支持"百分之三十"等)；
           TTS移除解决播放响应延迟(在线歌曲从3s降至1s)；
           下载完成添加右下角showToast提示(_prevDownloadStatus状态追踪)；
           scanner.py remove_song Lock类型修复(asyncio.Lock→threading._thread_lock)；
           删除歌曲不再删除专辑封面图片(仅删音频+歌词)
  0.0.32 - 15 个问题批量修复：
           歌手列表播放/删除按钮对齐(align-items-center)+文案改播放全部+stopPropagation；
           点击歌手头像显示该歌手所有歌曲(showArtistSongs 模态框)；
           播放替换列表非增量(playSongFromList 传歌曲列表给后端替换 playlist)；
           统一列表垂直居中(模态框/在线下载 td 加 vertical-align:middle)；
           在线搜索歌手/专辑 id 剥离正则修复(/^(kuwo_artist_|kuwo_album_|kuwo_|netease_)/)；
           歌手名可见性修复(album-card-artist #7f848e→#b0b6c1，专辑详情用 color:#aaa)；
           在线下载播放白龙马2秒停住修复(main.py 补 import httpx，代理端点 NameError)；
           TTS 默认关闭(tts_enabled=False，MiNA API 403 无权限，UBus 被音乐覆盖)；
           歌曲列表/在线下载列表高度改为 calc(100vh - 280px) 靠近播放器
  0.0.31 - TTS 修复：text_to_speech 优先使用 MiNA API TTS 端点
           (/miv1/device/:id/text_to_speech)，失败回退 UBus player_play_tts；
           之前只检查返回值 is not None，未检查 code 字段，设备返回错误码也报成功；
           _safe_tts 根据 text_to_speech 返回值记录成功/失败日志
  0.0.30 - 前端 UI 优化 + 后端修复：
           在线下载搜索结果列表样式对齐音乐库(table-layout:fixed+垂直居中)；
           歌手详情页头像(onerror兜底+后端从PC端API获取)；
           歌手热门歌曲/专辑详情列表样式优化(封面+垂直居中+table-layout:fixed)；
           下载全部提示文案改为"已加入下载队列"；
           下载管理页添加3秒自动轮询；
           删除歌曲误删歌手文件夹修复(_has_audio_files改os.walk递归)；
           TTS改fire-and-forget不阻塞播放(响应慢根因)；
           歌曲列表当前播放高亮(table-active+CSS)；
           歌手列表加播放全部按钮(playArtistSongs)
  0.0.29 - Bug 大修复：调试日志幂等化、路径校验修复(is_relative_to)、
           闹钟12小时制转换、异常处理日志补全、netease search 未定义变量修复、
           scanner asyncio.Lock 替换 threading.Lock；
           全局异常处理中间件 + uvicorn 错误日志 propagate 修复；
           前端修复：视图切换、进度轮询竞态、歌手排序、歌单按钮onclick、XSS 转义
  0.0.28 - 语音拦截重构：双轨检测（轨道1对话轮询0.2s+轨道2播放状态高频轮询），
           时间窗口2s→30s防漏拦，stop_all_media改并发发送抢先劫持，
           元数据查询改fire-and-forget不阻塞播放，修复index.html引号嵌套语法错误
  0.0.27 - 架构优化：Token 自动刷新、smart_resume、SearchProvider ABC、增量同步、网易云歌词；
           Dockerfile 改为三阶段构建（alpine ffmpeg + deps + 运行镜像），体积 893MB→463MB；
           build_oci 分层优化（deps/ffmpeg/app 独立层，NAS 重复构建时前两层 digest 不变可复用）
  0.0.22 - Bug 大修复：SQLite 持久化、StreamingResponse 防 OOM、路径校验、SHUFFLE 去重、
           扫码长轮询超时、监控异常日志、循环导入回调注入、token_refresh 401 自动重试
  0.0.15 - 修复进度条不更新（UBus data.info.play_song_detail 嵌套解析）
  0.0.6  - 睡眠定时/闹钟/TTS 反馈/移动端适配
  0.0.1  - 项目骨架 + 基础扫描 + Web 管理
"""

__version__ = "0.0.37"
