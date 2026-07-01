"""版本号定义 - 项目的唯一版本真相源

pyproject.toml 通过 hatchling dynamic version 从本文件读取版本号；
app/main.py 和 build_oci.py 也从本文件读取；
前端 index.html 通过 /api/config API 动态获取版本号。

每次发版只需修改本文件的 __version__ 和下方版本历史注释。

版本历史：
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

__version__ = "0.0.31"
