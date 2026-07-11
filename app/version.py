"""版本号定义 - 项目的唯一版本真相源

pyproject.toml 通过 hatchling dynamic version 从本文件读取版本号；
app/main.py 和 build_oci.py 也从本文件读取；
前端 index.html 通过 /api/config API 动态获取版本号。

每次发版只需修改本文件的 __version__ 和下方版本历史注释。

版本历史：
  0.0.75 - 修复本地播放路径play_music_url往返期间小爱版启动(LET IT GO案例):
           问题: v0.0.74只修复了在线路径,但LET IT GO走的是本地播放路径;
                 日志证据: 14:31:20用户说话(answer="请欣赏试听版")
                 -> 14:31:22 stop_all_media完成 -> 14:31:22 play_music_url发出
                 -> 14:31:24 play_music_url响应(2秒空窗!)
                 这2秒内REPLACE_ALL还没生效,小爱版试听版启动;
           根因: 本地播放路径的注释"不需要压制循环"是错误假设,
                 play_music_url的UBus往返同样需要1-2秒,期间没有压制;
           修复: 本地播放路径也用_suppress_native上下文管理器包裹,
                 覆盖"等待stop_all_media + play_music_url往返"整个流程,
                 压制循环持续到play_music_url响应后才停止;
  0.0.74 - 修复play_music_url UBus往返期间小爱版启动(真正的空窗期根因):
           问题: v0.0.73后用户反馈问题依旧,日志分析13:47:29童年老家:
                 13:47:30 play_music_url发出(压制停止) -> 13:47:32 play_music_url响应(2秒!)
                 -> 13:47:33 设备请求proxy URL;
                 这2-3秒内压制已停止,REPLACE_ALL还没生效,小爱版在此期间启动;
           根因: v0.0.73把play_music_url放在with块外(退出后)执行,
                 压制循环在play_music_url发出前就停止了,
                 但play_music_url的UBus请求需1-2秒往返,期间没有压制;
           修复: 把play_music_url移进with块内执行,
                 压制循环持续到play_music_url响应后才停止;
                 stop的UBus响应(0.3-0.5s)比play_music_url(1-2s)快,
                 所以stop会在REPLACE_ALL之前到达音箱,不会停掉我们的播放;
           play_song和set_play_mode两处在线路径都重构为此模式;
  0.0.73 - 修复在线播放2秒延迟(_get_device_hardware缓存过期+finally gather等timeout):
           问题: v0.0.72后用户反馈问题依旧,日志分析发现13:25:51 stop_all_media完成
                 到13:25:53 play_music_url发出之间有2秒空窗期;
           根因1: _get_device_hardware调用_get_device_list(60s TTL缓存),
                 服务启动2分钟后缓存过期,需HTTP请求小米服务器获取设备列表(约2秒),
                 这2秒在with块内执行但压制循环的stop响应已回来,实际压制已停止;
           根因2: finally中await asyncio.gather(*all_stop_tasks)等待所有stop任务完成,
                 某些stop任务可能2秒timeout才完成,延迟play_music_url发出;
           修复1: _get_device_hardware添加永久缓存(_hardware_cache),
                 设备型号不会变,首次查询后永久缓存,避免重复HTTP请求;
           修复2: 在线路径并行获取hardware(hardware_task与search_by_keyword同时启动),
                 搜索完成后await hardware_task获取结果,不额外增加时间;
           修复3: finally的gather改为asyncio.wait(timeout=0.1)+取消未完成任务,
                 不等2秒timeout,play_music_url可提前2秒发出;
  0.0.72 - 修复favicon未使用设计logo + 在线播放空窗期小爱版先行:
           问题1(浏览器标签页图标): index.html无<link rel="icon">标签,
                 浏览器使用默认图标而非设计的musicnest-logo;
           修复1: index.html添加SVG+PNG favicon link,
                 main.py新增/favicon.ico路由返回musicnest-logo.png;
           问题2(放很多NAS没有的歌先放小爱版): _suppress_native_during_search
                 的压制循环在搜索完成时就停止,但之后还有3-5秒空窗期
                 (URL处理+等待stop_task+获取hardware),小爱版在此期间异步启动;
           修复2: 将_suppress_native_during_search重构为@asynccontextmanager
                 _suppress_native上下文管理器,压制循环覆盖"搜索+URL处理+
                 等待stop+获取hardware"整个流程,退出with块后才发送play_music_url;
                 play_song和set_play_mode两处在线路径都重构为此模式;
  0.0.71 - 修复语音指令在线播放竞态+状态污染,netease共享连接池:
           main.py:
             H1 play_song在线路径play_state修改(playlist/current_index/device_id/duration)
                全部移入_play_lock,与set_play_mode/本地播放路径一致防竞态;
                之前仅is_playing在锁内,playlist等4字段在锁外,API并发读取会读到部分更新状态;
             H2 play_song在线路径URL为空时不再污染play_state:
                之前先修改play_state再校验URL,URL为空时playlist已被替换为[无URL歌曲],
                导致/api/player/state返回错误列表(用户感知为"播放列表乱掉");
                修复:先校验URL可用性,确认可用后才修改play_state;
             H3 api_player_play的playlist赋值移入_play_lock:
                之前playlist=songs在锁外,与语音指令并发修改时产生竞态;
           netease.py:
             M1 新增共享httpx客户端(_get_client/close_client),
                _netease_request和verify_cookie复用连接池,
                歌单同步千首歌时避免TCP连接堆积(参照kuwo.py模式);
             main.py lifespan关闭时调用netease.close_client();
  0.0.70 - 修复设备端超时日志爆炸和token失效后疯狂401:
           client.py: UBus code=100(设备端读取超时)日志降级为DEBUG，
                      其他错误截断data至200字符避免Java堆栈污染日志;
           token_refresh.py: 新增_token_invalid标志，passToken刷新失败时置位，
                             is_token_invalid()/reset_token_invalid()供外部查询/重置;
           monitor.py: _poll_loop检查token失效标志，失效时暂停轮询每30秒检查重登录;
           media_watcher.py: _watch_loop同样检查token失效标志暂停轮询;
           main.py: 两处登录成功路径调用reset_token_invalid()恢复轮询;
  0.0.69 - 第二轮深度代码审阅修复:
           main.py:
             H1 _on_voice_message的play_song/next/previous/stop/play_playlist/set_play_mode
                分支修改play_state时加_play_lock，与API端点一致防竞态;
             H4 _alarm_loop修改play_state加_play_lock;
             H5 _smart_resume_playback修改play_state加_play_lock;
             H3 _transcode_status/_transcode_locks新增上限500条目清理
                (_cleanup_transcode_status_index)，防大曲库内存泄漏;
             H2 tracker函数全部改为await调用(16处);
             M1 新增共享_proxy_client连接池(api_music_proxy复用);
             M6 scanner.search全部改asyncio.to_thread(5处，防阻塞事件循环);
             M7 新增封面缓存清理(_cleanup_cover_cache，上限200MB);
             M5 删除未使用的Counter导入和playlist_sync_worker死代码;
             M10 删除_parse_cn_number中200-900的死分支;
             L2 提取_delete_song_by_filepath函数，移除_FakeReq hack;
             L3 os.rename统一为os.replace;
             L4 _parse_alarm_from_query合并重复正则为_apply_period+_try_pattern;
             L5 api_playlist_create删除冗余的第二次config.get;
           config.py:
             M2 set()改防抖保存(_schedule_save 500ms)+flush_save+atexit注册;
           tracker.py:
             H2 新增_async_wrap装饰器，16个公开函数全部async化;
           worker.py:
             H2 tracker调用全部加await(16处);
             M3 _download_file移除逐chunk线程池写入(直接同步write);
             M8 删除netease_download_url函数内重复导入;
           monitor.py:
             M4 mark_query_handled移除break，标记所有匹配项防重复处理;
  0.0.68 - 全代码库深度审阅修复(CRITICAL 7 + HIGH 15 + MEDIUM 30 + LOW 30):
           main.py:
             C1 play_state加asyncio.Lock防并发竞态(播放列表大BUG根因);
             C2 压制循环孤儿stop任务用gather等待全部完成(先播小爱版根因);
             C3 set_play_mode的argument.strip()对None崩溃(循环播放不切换根因);
             C4 ffmpeg/ffprobe子进程异常时kill防孤儿;
             C5 _play_on_device优先用song_index取歌;
             C6 _create_background_task加强引用防GC;
             C7 _pause_elapsed切歌后重置;
             C8 _transcode_locks字典清理;
             C9 mark_query_handled提前调用防重复拦截;
             C10 artist_cover/album_cover路径遍历校验;
             C11 _transcode_status跨await用get防KeyError;
             C12 闹钟用局部变量不覆盖device_id;
             M1-M6 SHUFFLE上一首/循环检测/日志/LRU/stem.replace/字段语义;
           engine:
             C1 OWN_PLAY_WINDOW_SEC从60降到30(只播放小爱版根因);
             H1 is_query_handled窗口对齐30秒;
             H2 _watch_loop异常退避防刷屏;
             M1 连续失败60秒冷却后恢复;
             M2 per-device预热标志;
             player异常时await stop()同步设备;
             voice create_alarm优先级+idx优先匹配;
           miot:
             C1 token_refresh并发刷新加Lock+入口占位(passToken风控根因);
             C2 UBus code!=0添加warning日志;
             C3 mark_own_play清理过期项+空device_id返回False;
             C4 exchange_token清空cookie jar;
             C5 auth添加set_device_id方法;
             H1-H6 节流重试/异常日志/连接池/success_count/json解析;
           search:
             C1 kuwo N_MINFO字段名修复(搜索延迟根因);
             C2 _parse_nminfo支持=分隔符+APE/192K映射;
             C3 netease搜索去掉双重遍历(搜索卡17分钟根因);
             C4 verify_cookie删除误判阶段2/3;
             H1-H6 close_client锁/duration解析/封面URL抽取/search_all超时;
           scanner+config:
             C1 config deepcopy防DEFAULT_CONFIG污染;
             H1 scan与remove竞态修复(已删除歌曲复活根因);
             H2 set/update深拷贝入参;
             M1-M4 返回deepcopy/路径校验/缓存一致性/tmp唯一文件名;
             新增reload_cache()供worker刷新;
           worker+tracker:
             C1 下载原子写入+阈值按格式;
             C2 stale reset周期调用+60分钟阈值;
             H1 删除fallback到results[0](下载错歌根因);
             H2 ID3标记文件防永久跳过;
             H3 set_scanner_ref共享scanner实例(重复下载根因);
             H4-H6 add_task回查/RLock/DB迁移;
           前端:
             H1 进度条拖动去抖(只在释放时seek);
             H2 音量滑块去抖;
             H3 播放列表签名含歌曲内容(等长替换不刷新根因);
             H4 random模式图标映射;
             M1-M7 转码超时/切歌停止轮询/scrollIntoView优化/duration=0/暂停不重置/双重渲染;
             L1-L5 onclick转义/closeMenu清理/播放防抖/变量重命名;
  0.0.67 - 修复转码文件膨胀3倍(封面图) + MediaWatcher误判窗口 + 压制循环请求积压:
           问题1(白龙马先播小爱版): 压制循环每0.3秒发stop_all_media(6个UBus请求),
                 每秒20个请求+2秒响应延迟=请求积压,stop无法及时到达音箱;
                 修复1: _suppress_native_during_search改用轻量stop(只发1个UBus请求),
                       避免请求积压,0.3秒间隔不变;
           问题2(只播放小爱版/LET IT GO): mark_own_play的10秒窗口太短,
                 转码5秒+音箱请求URL延迟14秒=19秒,超过窗口后MediaWatcher误判为原生播放;
                 RECENT_QUERY_WINDOW_SEC=5秒也太短,query过期后MediaWatcher不拦截;
                 修复2: OWN_PLAY_WINDOW_SEC从10秒增加到60秒,
                       RECENT_QUERY_WINDOW_SEC从5秒增加到30秒;
           问题3(停几秒再继续播放): 转码比特率293kbps(目标96k),文件11.4MB(预期3.9MB);
                 根因: FLAC文件包含封面图(embedded artwork),ffmpeg默认把封面图
                       编码进MP3的ID3标签(APIC frame),导致文件膨胀3倍;
                 修复3: ffmpeg命令加-vn跳过视频流+封面图,
                       加-ar 44100 -ac 2明确采样率和声道;
                 缓存失效: 旧缓存文件名{hash}.mp3改为{hash}_v2.mp3,
                       旧错误缓存不会被命中,会重新用正确参数转码;
  0.0.66 - 修复单曲循环进度条不归零 + 转码缓存清理索引 + 压制循环提速:
           问题5(循环播放第二遍进度条不归零): 单曲循环时current_index不变,
                 前端songChanged=false不重启进度轮询;pollProgressApi"只往前推不后退"
                 导致apiPos回到0时localPosition仍停在duration;
                 修复5a(前端): pollProgressApi检测apiPos<localPosition-3时重置localPosition
                       (单曲循环/上一首/手动切歌都能处理);
                 修复5b(前端): localTimer到达duration末尾时立即拉一次API,快速检测循环;
                 修复5c(后端): /api/player/progress检测elapsed>=duration-2且position=0时
                       重置_play_start_time,本地计时归零;
           问题6(清理转码缓存未清理索引): _cleanup_transcode_cache只删文件,
                 _transcode_status字典中对应file_hash条目残留;
                 修复6: 删除文件时从p.stem提取file_hash,pop _transcode_status对应条目;
           问题7(压制循环间隔): 用户要求从0.5秒改成0.3秒;
                 修复7: _suppress_native_during_search的asyncio.sleep(0.5)改为0.3;
  0.0.65 - 修复在线播放先播小爱版 + 播放模式语义 + 转码缓存持久化 + 列表自动滚动:
           问题1(白龙马先播3秒小爱版): play_song/set_play_mode在线路径,stop_all_media后
                 到play_music_url之间有搜索空窗期(1-2秒),小爱版异步启动并播放;
                 修复1: 新增_suppress_native_during_search函数,搜索期间每0.5秒
                       fire-and-forget一次stop_all_media持续压制小爱版;
                       搜索完成后await最后一个stop完成(确保stop在play之前到达音箱),
                       然后立即play_music_url(REPLACE_ALL)接管;
                       play_song和set_play_mode在线路径都用此函数包裹搜索;
           问题2(循环播放let it go web图标不更新): "循环播放"映射到LIST_LOOP(列表循环),
                 但用户期望单曲循环;voice.py中"循环播放"→param="loop";
                 修复2: set_play_mode处理中,若param="loop"且arg非空(带歌曲名),
                       改为param="single"(SINGLE_LOOP);"循环播放"(不带歌曲名)仍为列表循环;
           问题3(转码缓存每次重头转): TRANSCODE_CACHE_DIR在/tmp,容器重启后丢失;
                 修复3: 转码缓存目录从/tmp/musicnest_transcode改到/data/musicnest_transcode
                       (持久化路径,容器重启不丢失);
                 说明: MP3源文件已免转码(_play_on_device和/api/music/play都判断.endswith('.mp3'));
           问题4(播放列表高亮项不自动滚动): renderPlaylistPanelFromState渲染后无scrollIntoView,
                 歌曲多时需手动滑动很久才能看到当前播放;
                 修复4: 渲染后查询li.current元素,scrollIntoView({block:'center'})居中显示;
  0.0.64 - 修复播放列表大BUG(语音指令与web界面不同步) + 起风了播放中断诊断:
           问题1(起风了播放几秒后停止几秒才继续): 转码文件11.4MB/96kbps≈16分钟,
                 日志显示22:35:22和22:35:23两次206请求,疑似比特率异常或Range重连;
                 修复1(诊断): _ensure_transcode_cached添加ffprobe源文件时长探测+
                       转码完成后实际比特率计算日志(>120kbps时WARNING告警),
                       用于下次测试确认转码参数是否正确生效;
           问题2(语音切换播放模式web图标不更新): updatePlayerUI在!hasSong&&!is_playing时
                 early return,mode图标更新代码在该return之后,无歌曲在播时图标不更新;
                 修复2(前端): 新增updateModeIcon()函数,在fetchPlayerState()中无条件调用,
                       不依赖updatePlayerUI执行路径;新增PLAY_MODE_ICONS/PLAY_MODE_TITLES映射;
           问题3(语音控制的播放未更新web播放列表): 双根因,
                 根因3a(前端): 播放列表面板只在点击展开时拉取一次,5秒轮询fetchPlayerState不刷新面板;
                 修复3a(前端): 新增_lastPlaylistSignature变量+renderPlaylistPanelFromState(),
                       fetchPlayerState检测playlist长度/current_index/mode变化且面板已展开时自动刷新;
                 根因3b(后端): set_play_mode在线播放路径完全未更新play_state.playlist/current_index/device_id,
                       导致/api/player/state返回旧播放列表;
                 修复3b(后端): set_play_mode在线路径构造song_dict并完整更新play_state
                       (playlist/current_index/device_id/duration);
           问题4(播放列表整体大BUG): set_play_mode本地播放路径缺少play_state.device_id设置;
                 _play_on_device不调用mark_own_play,导致next/prev/play_playlist等指令期间
                 MediaWatcher可能误拦截自己的播放;
                 修复4a(后端): set_play_mode本地路径补全play_state.device_id;
                 修复4b(后端): 将mark_own_play移入_play_on_device函数内部,统一覆盖所有调用路径
                       (voice command/next/prev/web UI);
                 修复4c(后端): 语音指令处理函数末尾添加全局mark_query_handled,
                       覆盖所有非play_song分支(防止MediaWatcher反查到未处理query重复触发拦截);
           简化: refreshPlaylistPanel复用renderPlaylistPanelFromState避免重复请求;
  0.0.63 - 修复MediaWatcher误判机制(用mark_own_play替代mark_query_handled提前):
           v0.0.62问题: mark_query_handled提前后,MediaWatcher在is_query_handled=True时
                 直接跳过,导致小爱版网易云试听版的异步启动无法被MediaWatcher拦截;
           根因: mark_query_handled只标记"query已被处理",但VoiceCmd处理期间_play_on_device
                 需1-2秒,这期间小爱版可能异步启动,MediaWatcher应作为第二道防线拦截;
           修复: 回滚mark_query_handled到_play_on_device之后调用(原位置),
                 改为在_play_on_device之前调用mark_own_play(标记最近自己触发播放);
                 MediaWatcher的is_own_play_recent检查(10秒窗口)会返回True,跳过拦截;
                 mark_own_play覆盖整个_play_on_device执行时间(1-2秒)+转码等待时间;
  0.0.62 - 修复MediaWatcher重复拦截导致先播小爱版再播我们的版本:
           根因: mark_query_handled 在 _play_on_device 之后才调用,
                 但 UBus 请求需1-2秒,期间 MediaWatcher 检测到原生播放,
                 检查 is_query_handled 返回 False(还没标记),触发重复拦截;
                 拦截回调又调用 VoiceCmd 处理流程,导致重复 stop+play;
           日志证据: 起风了被处理两次(22:10:32 和 22:10:34),两次本地播放;
           修复: mark_query_handled 提前到 _play_on_device/play_music_url 之前调用,
                 MediaWatcher 检查时就知道 query 已被处理,跳过拦截;
  0.0.61 - 回滚边转边播(音箱不支持无Content-Length流式响应) + 降到96k:
           问题1(边转边播失败): L05C音箱不支持无Content-Length的StreamingResponse,
                 5-7秒断开重连,每次重连又启动新转码,.part文件被删除,永远生成不了.mp3缓存;
                 日志证据: 22:11:30到22:12:54,84秒内转码12次,每次都是"边转边播开始"+"首块数据已输出";
           修复1: 回滚到v0.0.59的预转码+FileResponse方案(带Content-Length,支持Range);
                 恢复_play_on_device的fire-and-forget预热;
                 首次转码5-8秒延迟,但播放不会中断;后续缓存命中秒开;
           修复2: 比特率128k→96k,转码速度提升约25%,缓解5-8秒空窗期;
           保留: v0.0.60的set_play_mode stop_task/KeyError修复;
  0.0.60 - 边转边播(流式传输) + 修复set_play_mode的stop_task/KeyError bug:
           修复1(边转边播): 预转码整个文件需5-9秒,这期间音箱没声音,小爱版插进来;
                 改为 ffmpeg -f mp3 pipe:1 输出到stdout,StreamingResponse边读边输出;
                 1秒内首块数据到达音箱,音箱立即开始播放;
                 同时写一份到.part文件,完成后rename到.mp3作为缓存;
                 缓存命中时返回FileResponse(带Content-Length,支持Range);
                 移除_play_on_device的fire-and-forget预热(避免与流式转码冲突);
           修复2(stop_task UnboundLocalError): set_play_mode分支引用了stop_task,
                 但它只在play_song分支定义;改为stop_task2独立变量;
           修复3(KeyError: 0): kw_result["data"]可能是空list或非list,
                 改为isinstance(list) and len>0检查;
  0.0.59 - 修复转码416反复重试(原子写入) + 循环播放歌曲名识别:
           修复1(416根因): ffmpeg创建输出文件但还在写入时,os.path.isfile()返回True但文件不完整;
                 HTTP端点返回不完整文件,音箱Range超出→416反复重试8秒;
                 日志证据: 21:42:12预转码开始, 21:42:13"缓存命中"但416, 21:42:20预转码完成;
                 修复: ffmpeg输出到xxx.mp3.part,完成后os.rename到xxx.mp3;
                 xxx.mp3存在=转码完成,不会读到不完整文件;
           修复2(循环播放XXX): "循环播放LET IT GO"被识别为set_play_mode(循环播放4字符>播放2字符);
                 只切换模式没播放歌曲,用户说"没有声音";
                 修复: set_play_mode处理中,若argument非空(歌曲名),切换模式后播放该歌曲;
                 支持本地和在线两种路径,遵循online_only_voice开关;
  0.0.58 - 新增"只播放在线歌曲"开关 + web转码进度展示:
           功能1(只播放在线歌曲):
             config: 新增online_only_voice字段(默认False);
             main: 语音指令play_song路径检查开关,开启时跳过本地搜索直接走在线;
             说明: 仅影响语音指令,web页面点播不受影响,仍播NAS;
             场景: 避免NAS转码耗时(首次5-6秒),语音指令全走在线秒开;
           功能2(web转码进度展示):
             main: _ensure_transcode_cached记录状态到_transcode_status字典;
             main: 新增/api/music/transcode/status/{song_index}端点;
             main: /api/player/play响应附加needs_transcode和transcode_real_index字段;
             index.html: 播放器加转码状态元素(转码中...Xs);
             index.html: playSong/playSongByList播放后启动轮询,1秒一次;
             index.html: 转码完成/失败/无需转码时停止轮询;
  0.0.57 - 修复本地播放416中断+小爱版接管,转码降到128k:
           根因: play命令发出后音箱立即请求URL,但此时转码尚未完成,
                 音箱得到HTTP 416 Range Not Satisfiable,反复重试8秒直到转码完成;
                 这8秒空窗期小爱版可能接管播放;
           日志证据:
             21:04:03 play响应成功 + 转码开始
             21:04:09 音箱请求URL → 416(转码未完成)
             21:04:11 转码完成(8秒), 音箱请求URL → 206(成功)
           修复1(架构): 转码逻辑抽为_ensure_transcode_cached函数,per-file锁防并发;
                 _play_on_device中fire-and-forget启动转码(不阻塞play,抢占通道防小爱);
                 HTTP端点await _ensure_transcode_cached等待转码完成再返回FileResponse(不416);
           修复2(加速): 转码比特率192k→128k,约30%加速,音箱听不出差别;
  0.0.56 - 修复本地播放2秒中断(416反复重试)和小爱版接管:
  0.0.55 - 转码缓存增加 1GB 大小限制和 LRU 清理:
           问题: 预转码缓存不会自动清理,长期运行会无限增长占满磁盘;
           修复: 写入新缓存后异步检查总大小,超过1GB时按mtime升序删除最久未访问的;
                 缓存命中时os.utime更新mtime(容器通常noatime,atime不可靠);
                 清理逻辑fire-and-forget不阻塞响应;
  0.0.54 - 修复本地播放2秒停止 + 在线播放小爱版2秒重叠:
           问题1(本地播放2秒停止): StreamingResponse无Content-Length,
             L05C音箱HTTP客户端5-6秒后断开重连,导致播放2秒停2秒循环;
             日志证据: 20:46:39转码开始, 20:46:44转码开始(5秒后), 20:46:50转码开始(又6秒后);
           修复1: 转码改为预转码到临时文件(/tmp/musicnest_transcode/{md5}.mp3),
                  用FileResponse返回(带Content-Length和Range支持),音箱可正常缓存和断点续传;
                  首次转码有几秒延迟,但缓存后秒开;转码失败清理临时文件;
           问题2(在线播放小爱版2秒): 压制循环每次await stop_all_media()阻塞1-2秒,
             5次迭代实际耗时5.1秒(非设计的1.5秒),这5秒期间没有play命令,
             小爱版网易云试听版趁机播放;
           修复2: 移除压制循环,改为stop完成后立即play_music_url(REPLACE_ALL),
             让REPLACE_ALL尽早抢占通道,小爱版最多播放1-2秒(play生效延迟);
  0.0.53 - 回滚占位play策略(失败),恢复压制循环:
           日志分析发现:占位play的UBus请求需要2秒生效(发出->响应->音箱请求URL),
                         这2秒内小爱版已经启动并播放,占位play无法阻止小爱版;
           用户确认:白龙马还是先播放小爱版,然后才是我们的在线版;
           压制循环更有效:每0.3秒stop一次,持续1.5秒,不给小爱版启动机会;
           修复:移除占位play和/api/music/silence端点;
                 本地播放:stop+play(无压制循环,2.5秒,本地URL快);
                 在线播放:stop+压制循环(1.5秒)+play(4秒,但能阻止小爱版);
  0.0.52 - 新占位策略: stop+静音play占位+真实play(替代压制循环):
           用户方案: stop_all_media停TTS后,立即用静音URL play_music_url(REPLACE_ALL)占位,
                     占据media通道防止小爱版异步启动,然后搜索歌曲,真实URL REPLACE_ALL替换静音占位;
           优势: 无需压制循环(省1.5秒),通道始终被我们占据;
           实现: 新增/api/music/silence端点返回最小静音WAV(45字节);
                 本地播放: stop+占位play+真实play;
                 在线播放: stop+占位play+kuwo搜索+真实play;
           在线播放优化: 占位play在stop完成后立即发送(不等搜索),消除空窗期;
  0.0.51 - 优化:本地播放移除压制循环(不需要):
           日志分析发现:本地播放play响应后音箱同秒请求URL(局域网快),
                         REPLACE_ALL立即生效,小爱版来不及启动;
           在线播放需要压制循环:代理URL有网络延迟,REPLACE_ALL生效慢;
           修复:本地播放移除压制循环,保持stop+play(约2.5秒);
                 在线播放保留压制循环(约4秒,但消除小爱版);
  0.0.50 - 新增压制循环策略，消除小爱版网易云试听版:
           问题: 小爱收到"播放XX"后TTS被停,但会异步启动网易云试听版,
                 单次stop_all_media停不掉这个异步启动,用户会听到2秒小爱版;
           策略: 首次stop_all_media后,循环每0.3秒stop一次×5次(共1.5秒),
                 持续压制小爱版启动,然后play_music_url(REPLACE_ALL)接管;
           代价: 用户等待时间增加1.5秒(总约2.5秒),但完全听不到小爱版;
           压制范围: 覆盖play命令到音箱实际请求URL的延迟窗口(约1~2秒);
           应用: 本地播放和在线播放路径都加入压制循环;
  0.0.49 - 回滚v0.0.45的延迟重播策略(反而导致播放2秒就停):
           问题: v0.0.45的_replay_after_delay在2.5秒后执行stop+play,
                 把正在播放的我们的歌曲给停了(用户听到播放2秒就停);
             日志证据:
               19:58:30 play_music_url响应(第一次播放启动)
               19:58:31 音箱请求代理URL(开始播放)
               19:58:33 stop_all_media 6/6 ← 延迟重播的stop!把我们的播放停了!
               19:58:34 play_music_url响应(重播)
               19:58:35 音箱请求代理URL(重新播放)
           根因: v0.0.42的await stop_task已确保stop_all_media在play之前完成,
                 小爱原生播放已被停掉,无需延迟重播;
             延迟重播是针对v0.0.42之前fire-and-forget设计的,v0.0.42后已多余;
           修复: 移除_replay_after_delay和_replay_local_after_delay函数及调用;
  0.0.48 - 修复 _get_latest_ask_via_userprofile 请求日志每0.2s刷屏:
           v0.0.43漏删了请求日志(只删了循环内记录日志和响应日志);
           client: 删除_get_latest_ask_via_userprofile的请求DEBUG日志;
  0.0.47 - token刷新改为纯被动401模式（用户要求不主动掉线）:
           token_refresh: 移除主动刷新循环(_refresh_loop/_check_and_refresh);
             移除有效期估算(SERVICE_TOKEN_VALID_SEC/TOKEN_REFRESH_THRESHOLD_SEC/_token_created_at);
             移除主动检查间隔(TOKEN_REFRESH_INTERVAL_SEC);
             只在API返回401时被动触发刷新(handle_token_expired);
             60秒节流防并发刷新风暴;
             401刷新失败时才提示用户重新登录;
             start_refresh_loop/stop_refresh_loop/record_token_created保留为兼容no-op接口;
             小米serviceToken实际有效期约30天,passToken约1年,
             只要passToken未过期,401时就能自动刷新,无需用户干预;
  0.0.46 - 修复小米账号授权过期太快：
           问题1: 密码登录不保存passToken,导致无法自动刷新token;
             auth: login_with_password返回值增加passToken(从响应或cookie jar提取);
             main: 密码登录config.update增加miot_pass_token字段;
           问题2: SERVICE_TOKEN_VALID_SEC=12小时假设太短(实际约30天),
                 每2小时刷屏一次"刷新失败"日志;
             token_refresh: 有效期从12小时改为7天(保守估算);
             token_refresh: 检查间隔从2小时改为12小时;
             token_refresh: 刷新阈值从3小时改为1天;
           问题3: 刷新失败时日志不够详细;
             token_refresh: _do_refresh区分"无passToken"/"返回空结果"/"异常"三种失败情况;
  0.0.45 - 修复小爱原生播放延迟启动导致"两个版本重叠"：
           问题: 小爱收到"播放XX"后会异步播放网易云试听版,
                 stop_all_media停掉了TTS但音乐播放请求在stop之后才触发,
                 导致与我们的播放重叠(用户听到两个版本);
           修复: 在线/本地播放成功后2.5秒执行延迟重播(stop+play),
                 覆盖小爱延迟启动的播放;
             新增 _replay_after_delay (在线)和 _replay_local_after_delay (本地);
             只在仍在播放同一首歌时执行(用户停止/切歌则跳过);
  0.0.44 - 添加转码诊断日志：
           main: ffmpeg stderr从DEVNULL改为PIPE+添加-loglevel error(避免进度信息阻塞管道);
             转码结束后检查returncode,非0非-9(非SIGKILL)时记录WARNING日志和stderr内容;
             用于诊断"起风了"本地FLAC播放时6秒内出现两次转码请求的问题;
             (可能是ffmpeg崩溃导致流提前结束,音箱重连);
  0.0.43 - 降低高频轮询日志噪音：
           client: _get_latest_ask_via_userprofile删除循环内每条记录的DEBUG日志
                 (ConversationMonitor每0.2s轮询,5条记录×5次/秒=25条日志/秒);
             client: _get_latest_ask_via_userprofile删除响应状态码DEBUG日志(高频噪音);
             client: get_player_status删除完整响应DEBUG日志(高频噪音);
             client: _ubus_request对player_get_play_status方法跳过DEBUG日志
                 (MediaWatcher每0.2s轮询,1秒10条日志);
             保留低频方法(play_music_url/player_play_operation等)的DEBUG日志便于排查;
  0.0.42 - 修复本地播放竞态条件导致播放"挂了"：
           main: play_song指令中stop_all_media从fire-and-forget改为await后再play;
                 之前stop命令(6个并发UBus)与play_music_url并发执行,
                 stop可能在play之后到达音箱,把刚启动的播放给停掉(本地路径尤甚);
             本地路径:await stop_task后再_play_on_device(确保stop先到);
             在线路径:play前也await stop_task(防止kuwo搜索过快时竞态);
             stop_task用_create_background_task包装(异常不丢失)+wait_for(3.5s超时兜底);
  0.0.41 - 修复 debug.log 日志级别配置 bug：
           main: _setup_debug_logging 中3个logger名称错误(musicnest.miot/monitor/player不存在);
                 导致 app.miot.client 的 DEBUG 日志(play_music_url/_ubus_request)无法输出;
                 修正为正确的模块路径 logger 名(app.miot.client/app.engine.monitor/app.engine.player);
                 补充缺失的 app.engine.media_watcher 和 musicnest.tracker;
  0.0.40 - 定时/闹钟/播放模式深度代码分析修复：
           main: 闹钟_alarm_loop调用_play_on_device前设置current_index(HIGH);
                 之前_play_on_device内部用current_song()取旧索引,闹钟指定歌曲被忽略;
           main: 闹钟触发时校验song_index边界+playlist为空时加载默认歌单(MEDIUM);
           main: _parse_alarm_from_query第二个正则也捕获歌曲名(MEDIUM);
                 之前"早上8点播放周杰伦"(无"每天")会丢失歌曲信息;
           main: 语音next分支无下一曲时调用stop_playing同步状态(MEDIUM);
                 之前stop_all_media后is_playing仍为True;
           main: 语音previous分支无当前歌曲时调用stop_playing同步状态(MEDIUM);
  0.0.39 - 两轮深度审阅修复（安全+一致性）：
           worker: 新增_sanitize_filename()清理文件名非法字符(HIGH安全);
           worker: artist/album/title目录和文件名全部清理防路径越界;
           worker: 歌词文件名也用清理后的title;
           main: delete端点filepath加_is_safe_path校验(MEDIUM安全);
           main: delete端点lyrics_path也加_is_safe_path校验;
           voice: VoiceCommand注释更新(含新增指令类型);
  0.0.38 - 新增下载语音指令（酷我搜索+复用worker下载流程）：
           voice: 新增download_current/download指令类型+优先级8/9；
           voice: download_current keywords=["下载当前歌曲","下载这首歌","下载当前","下载此歌"]；
           voice: download keywords=["下载歌曲","下载"]（argument作为搜索词）；
           config: DEFAULT_CONFIG加入两条download指令默认配置；
           main: 新增_download_via_kuwo()辅助函数(搜索+add_task入库)；
           main: _on_voice_message加download_current分支(判断本地/在线→构建搜索词)；
           main: _on_voice_message加download分支(argument为搜索词)；
           main: 后台执行避免阻塞语音回调链(_create_background_task)；
           index.html: 语音指令管理下拉框加download_current/download选项；
           复用worker._process_task(下载最佳音质+封面+歌词+ID3标签)；
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

__version__ = "0.0.75"
