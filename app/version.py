"""版本号定义 - 项目的唯一版本真相源

pyproject.toml 通过 hatchling dynamic version 从本文件读取版本号；
app/main.py 和 build_oci.py 也从本文件读取；
前端 index.html 通过 /api/config API 动态获取版本号。

每次发版只需修改本文件的 __version__ 和下方版本历史注释。

版本历史：
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

__version__ = "0.0.29"
