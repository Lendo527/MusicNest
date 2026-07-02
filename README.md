<div align="center">

# MusicNest

<p align="center">
  <img src="app/static/musicnest-logo.svg" alt="MusicNest Logo" width="180" height="180">
</p>

**小米音箱本地音乐播放服务 · 让小爱同学播放你 NAS 上的私人音乐库**

[功能](#-核心功能) · [架构](#-架构总览) · [快速开始](#-快速开始) · [语音指令](#️-语音指令) · [API](#-api-一览) · [更新日志](#-更新日志)

</div>

---

## 📝 项目简介

MusicNest 是一个运行在 NAS / 服务器上的 Docker 化音乐管理服务，把你的本地音乐库（按 `歌手/专辑/歌曲` 多层目录组织）接入小米生态：通过**小爱同学语音指令**或**Web 管理界面**即可在小爱音箱上播放 NAS 上的音乐，并支持**在线搜索下载**（酷我 / 网易云）以补充曲库。

- **后端**：FastAPI + uvicorn + asyncio（Python 3.11+）
- **前端**：单文件 HTML + Bootstrap 5 + 原生 JS（无构建步骤，零依赖）
- **设备侧**：小米 MiOT / MiNA HTTP API（设备控制、播放、UBus 请求）
- **运行**：Docker / docker compose（含静态编译 ffmpeg）

---

## ✨ 核心功能

### 🎵 本地音乐库
- **多层目录扫描**：支持 `Artist/Album/song.mp3`、`Artist - Album/01 - song.flac`、`Disc 1/...` 等常见结构
- **ffprobe 元数据**：自动读取 tag（标题/歌手/专辑/时长），路径作为 fallback
- **歌词关联**：自动匹配同名 `.lrc` / `.txt` 歌词文件
- **封面提取**：从音频文件中提取封面图缓存到 `/data`，无封面回退黑胶唱片 SVG
- **原子缓存**：扫描结果持久化为 `songs_cache.json`（原子写入 + schema 校验），启动秒级恢复
- **增量扫描**：新增歌曲无需全库重扫，`scan_new` 增量合并
- **流式转码**：`wav/flac/ogg/m4a/wma/aac` → `mp3` 实时转码播放，边转边播不占内存

### 🎤 语音控制（小爱同学）
- **双轨拦截引擎**：
  - **轨道 1**：对话轮询（0.2s 间隔）捕获小爱接收的指令
  - **轨道 2**：媒体状态高频轮询兜底（防止轨道 1 漏拦）
- **时间窗口防漏拦**：30s 时间窗内未处理的对话查询会被兜底处理
- **per-device 追踪**：`_last_own_play_at` 按设备记录本服务触发的播放，避免自拦截
- **预热防误拦**：服务重启后首轮只记录状态不触发拦截
- **指令类型**：播放歌单/歌曲、随机/单曲循环/列表循环/顺序模式、音量控制、上下一切歌、停止、**下载当前/指定歌曲**、创建闹钟

### 🔍 在线搜索 & 下载
- **双源搜索**：酷我音乐 + 网易云音乐
- **歌手/专辑详情**：在线浏览歌手热门歌曲和专辑列表
- **下载队列**：SQLite 持久化任务队列，支持并发下载、失败重试、状态追踪
- **最佳音质**：默认优先下载 FLAC（可在配置中切换 mp3），自动回退
- **完整元数据**：自动下载封面图 + 歌词 + ID3 标签写入
- **文件名清理**：自动清理非法字符，防止路径越界
- **歌单同步**：支持网易云歌单自动同步（按间隔拉取并下载缺失歌曲）
- **cookie 配置**：网易云 cookie 支持，解锁 FLAC / Hi-Res 下载

### ⏰ 定时 & 闹钟
- **睡眠定时**：N 分钟后自动停止播放
- **闹钟**：指定时间播放指定歌曲/默认歌单，支持重复周期

### 🎛️ Web 管理界面
- **播放器**：当前歌曲、进度条、音量、播放模式（单曲/单曲循环/列表/列表循环/随机）
- **音乐库浏览**：歌曲列表 / 歌手视图 / 专辑视图，支持搜索筛选
- **歌单管理**：创建/重命名/删除/编辑自定义歌单
- **在线搜索**：搜索 → 试听 → 下载，支持整专辑下载
- **下载管理**：任务列表、状态统计、重试/删除/清空
- **设备管理**：扫码登录小米账号、勾选参与的设备
- **语音指令配置**：可视化编辑关键词、启用/禁用、新增自定义指令
- **歌单同步配置**：添加网易云歌单同步源
- **系统设置**：轮询间隔、调试日志、自动扫描间隔等

### 🔐 安全 & 健壮性
- **路径校验**：所有文件操作通过 `_is_safe_path` 校验，防路径遍历
- **原子写入**：配置和缓存均使用 tmp + `os.replace` 原子替换
- **线程安全**：`RLock` 保护扫描器内部状态，`asyncio.Lock` 保护异步临界区
- **连接池管理**：httpx 客户端复用 + 关闭时统一释放
- **后台任务管理**：`_create_background_task` 统一异常回调，shutdown 时取消所有任务
- **token 自动刷新**：401 触发重新登录，刷新后通知所有 client 更新

---

## 🏗️ 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Web 浏览器 (index.html)                  │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTP API
┌────────────────────────────────▼────────────────────────────────┐
│                         FastAPI (main.py)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
│  │ 播放控制 │  │ 音乐库   │  │ 下载队列 │  │  语音指令    │    │
│  │ /player  │  │ /music   │  │ /download│  │  /voice      │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘    │
└───────┼─────────────┼─────────────┼────────────────┼───────────┘
        │             │             │                │
        ▼             ▼             ▼                ▼
┌───────────────┐ ┌───────────┐ ┌────────────┐ ┌────────────────┐
│ MinaHTTPClient│ │MusicScanner│ │  Worker    │ │ VoiceEngine    │
│ (MiOT/MiNA)   │ │ (ffprobe)  │ │ (下载线程) │ │ (指令匹配)     │
└───────┬───────┘ └─────┬─────┘ └─────┬──────┘ └────────────────┘
        │               │             │
        │         ┌─────▼─────┐  ┌────▼─────┐
        │         │  Scanner  │  │ Tracker  │
        │         │  缓存文件  │  │ (SQLite) │
        │         └───────────┘  └──────────┘
        ▼
┌─────────────────────────────────────────────────────────────────┐
│                小爱音箱 (MiNA / MiOT HTTP API)                   │
│   设备列表 / 播放 / 暂停 / 音量 / UBus 请求 / 媒体状态          │
└─────────────────────────────────────────────────────────────────┘
        ▲
        │ 双轨监控
┌───────┴───────────────────────────────────────────────────────┐
│  ConversationMonitor (0.2s 对话轮询)                          │
│  MediaWatcher        (0.2s 媒体状态高频轮询兜底)               │
└───────────────────────────────────────────────────────────────┘
```

### 双轨监控设计

- **轨道 1（ConversationMonitor）**：轮询小爱的对话记录，捕获"播放 XX"等指令
- **轨道 2（MediaWatcher）**：高频轮询媒体播放状态，检测到外部播放立即拦截
- **协同**：轨道 2 拦截后调用 `mark_query_handled` 通知轨道 1 避免重复处理

---

## 🚀 快速开始

### 1. 准备音乐库

将音乐按 `歌手/专辑/歌曲.mp3` 结构放到 NAS 某个目录，例如 `/volume1/music`。

### 2. 配置 docker-compose.yml

```yaml
services:
  musicnest:
    image: musicnest:latest
    container_name: musicnest
    restart: unless-stopped
    ports:
      - "58092:58092"
    volumes:
      - ./data:/data              # 配置/缓存/数据库/日志（持久化）
      - /volume1/music:/music     # 你的音乐库（只读即可）
    environment:
      - TZ=Asia/Shanghai
```

### 3. 构建并启动

```bash
docker compose up -d --build
```

镜像内置静态编译的 ffmpeg + ffprobe（约 70MB），无需额外安装。

### 4. 首次配置

1. 浏览器访问 `http://NAS_IP:58092`
2. 进入「设备管理」→ 使用**小米账号扫码登录**（获取 MiOT token）
3. 勾选要参与播放的小爱音箱
4. 进入「系统设置」→ 启用「对话监控」+「媒体监控」
5. 对小爱说"**播放歌单**"或"**播放周杰伦的歌**"即可开始

---

## 🗣️ 语音指令

所有指令可在 Web 界面「语音指令」页可视化编辑（关键词、启用/禁用、自定义新增）。

| 指令类型 | 触发关键词示例 | 说明 |
|---------|---------------|------|
| `play_playlist` | 播放歌单 / 放歌单 | 播放自定义歌单 |
| `play_song` | 播放歌曲 / 放歌曲 / 我想听 / 播放 | 搜索本地库播放（含在线 fallback） |
| `set_play_mode` | 随机播放 / 单曲循环 / 列表循环 / 顺序播放 | 切换播放模式 |
| `set_volume` | 设置音量 / 大声一点 / 小声一点 | 绝对/相对音量调节（支持中文数字） |
| `next` | 下一首 / 切歌 / 换一首 | 下一曲 |
| `previous` | 上一首 / 上一曲 | 上一曲 |
| `stop` | 停止播放 / 别播了 / 关机 | 停止播放 |
| `download_current` | 下载当前歌曲 / 下载这首歌 | 下载当前播放歌曲（本地则跳过） |
| `download` | 下载歌曲 / 下载 XX 的 XX | 酷我搜索首个匹配并下载 |
| `create_alarm` | 设置闹钟 每天早上8点 | 创建定时闹钟（可指定歌曲） |

### 下载指令说明

- **"下载当前歌曲"**：检测当前播放是否本地歌曲，非本地则用 `歌手 + 标题` 搜索酷我并下载（含封面+歌词+最佳音质）
- **"下载 周杰伦 青花瓷"**：argument 作为搜索词，酷我搜索首个匹配并下载
- 复用 worker 下载流程：FLAC 优先（可配置）+ 封面 + 歌词 + ID3 标签

---

## 🔌 API 一览

完整 70+ 端点，主要分组：

| 模块 | 端点示例 | 说明 |
|------|---------|------|
| 系统 | `GET /api/status` `GET /api/config` `POST /api/config` | 服务状态、配置读写 |
| 设备 | `GET /api/devices` `POST /api/devices/select` `POST /api/devices/auth` | 设备列表/勾选/扫码登录 |
| 音乐库 | `GET /api/music/songs` `GET /api/music/artists` `GET /api/music/albums` `GET /api/music/scan` | 扫描/浏览本地音乐 |
| 播放控制 | `POST /api/player/play` `POST /api/player/next` `POST /api/player/volume` `GET /api/player/state` | 播放/切歌/音量/状态 |
| 播放列表 | `POST /api/player/playlist` `GET /api/player/progress` `POST /api/player/seek` | 列表/进度/拖动 |
| 流媒体 | `GET /api/music/play/{idx}` `GET /api/music/proxy/{hash}` `GET /api/music/cover/{idx}` | 流式播放/代理/封面 |
| 在线搜索 | `GET /api/search/online` `GET /api/artist/{src}/{id}` `GET /api/album/{src}/{id}` | 酷我/网易云搜索 |
| 下载 | `POST /api/download/song` `POST /api/download/album` `GET /api/download/tasks` | 下载歌曲/专辑/任务管理 |
| 歌单 | `GET /api/playlist` `POST /api/playlist` `POST /api/playlist/{id}` | 本地歌单 CRUD |
| 歌单同步 | `GET /api/playlist-sync` `POST /api/playlist-sync` `POST /api/playlist-sync/{idx}/refresh` | 网易云歌单同步 |
| 定时 | `POST /api/timer/sleep` `POST /api/alarm` `DELETE /api/alarm` | 睡眠定时/闹钟 |
| 语音 | `GET /api/voice/commands` `POST /api/voice/commands` `PUT /api/voice/commands/{idx}` | 指令管理 |
| 监控 | `GET /api/monitor/status` `GET /api/monitor/messages` | 双轨监控状态 |
| 网易云 | `POST /api/netease/verify-cookie` | cookie 校验 |

---

## ⚙️ 配置说明

配置文件持久化到 `/data/config.yaml`，支持 Web 界面修改或直接编辑。

### 关键配置项

```yaml
# 小米账号（扫码登录后自动填充）
miot_token: ""
miot_user_id: ""
miot_device_id: ""
miot_ssecurity: ""

# 音乐库
music_path: "/music"
auto_scan_interval: 0              # 自动扫描间隔（秒），0=禁用

# 监控
conversation_monitor_enabled: false  # 对话轮询（轨道1）
media_watcher_enabled: true           # 媒体状态轮询（轨道2）
media_watcher_interval: 0.2          # 轮询间隔（秒）
poll_interval: 0.2                   # 对话轮询间隔

# 下载
download:
  flac_priority: true               # 优先 FLAC

# 网易云
netease:
  cookie: ""                         # 解锁 FLAC / Hi-Res
  enabled: true

# 歌单同步
playlist_sync: []                    # [{source, id, name, enabled}]
playlist_sync_interval: 1800         # 同步间隔（秒）

# 设备勾选
device_selections: {}                # deviceID -> bool

# 调试日志
debug_logging: true                  # 输出到 /data/debug.log
```

### 数据目录

```
/data
├── config.yaml           # 配置
├── songs_cache.json      # 音乐库缓存
├── downloads.db          # 下载任务数据库（SQLite）
├── debug.log             # 调试日志
└── covers/               # 封面缓存
```

---

## 🛠️ 开发

### 项目结构

```
musicnest/
├── app/
│   ├── main.py              # FastAPI 入口 + 所有 API 端点
│   ├── config.py            # 线程安全配置管理器（原子写入）
│   ├── version.py           # 版本号（唯一真相源）
│   ├── music/
│   │   ├── scanner.py       # 音乐库扫描器（ffprobe + 缓存）
│   │   └── index.py
│   ├── miot/
│   │   ├── auth.py          # 小米账号认证（扫码登录）
│   │   ├── client.py        # MiOT/MiNA HTTP 客户端
│   │   ├── token_refresh.py # Token 自动刷新
│   │   └── hardware.py      # 设备型号判断
│   ├── engine/
│   │   ├── monitor.py       # 对话监控（轨道1）
│   │   ├── media_watcher.py # 媒体监控（轨道2）
│   │   ├── player.py        # 播放引擎
│   │   └── voice.py         # 语音指令匹配
│   ├── search/
│   │   ├── base.py          # SearchProvider ABC
│   │   ├── kuwo.py          # 酷我音乐
│   │   └── netease.py       # 网易云音乐
│   ├── download/
│   │   ├── worker.py        # 下载 worker（封面+歌词+ID3）
│   │   └── tracker.py       # SQLite 任务追踪
│   ├── templates/
│   │   └── index.html       # 单文件前端
│   └── static/
│       ├── musicnest-logo.svg
│       ├── musicnest-logo.png
│       └── vinyl.svg        # 默认封面
├── Dockerfile               # 三阶段构建（ffmpeg + deps + 运行）
├── docker-compose.yml
└── pyproject.toml
```

### 本地开发

```bash
# 安装依赖
pip install -e .

# 启动开发服务器
uvicorn app.main:app --reload --port 58092

# 需要本地 ffmpeg + ffprobe
```

### 版本管理

- 版本号定义在 `app/version.py` 的 `__version__`
- `pyproject.toml` 通过 hatchling dynamic version 从该文件读取
- 前端通过 `/api/config` API 动态获取版本号
- 每次发版只需修改 `__version__` 并在版本历史注释中追加说明

---

## 📦 部署

### Docker（推荐）

```bash
docker compose up -d --build
```

三阶段构建：
1. **Stage 1**（alpine）：下载静态编译 ffmpeg + ffprobe（~70MB）
2. **Stage 2**（python:3.11-slim）：安装 pip 依赖（带层缓存）
3. **Stage 3**（python:3.11-slim）：最终运行镜像

最终镜像约 463MB（含 ffmpeg + Python + 依赖）。

### 升级

```bash
git pull
docker compose up -d --build
```

配置/缓存/数据库持久化在 `./data` 卷，升级不丢失。

---

## 🔧 故障排查

### 日志

- **应用日志**：`docker logs musicnest`
- **调试日志**：`./data/debug.log`（包含 DEBUG 级别全量日志）

### 常见问题

| 现象 | 排查 |
|------|------|
| 扫码登录失败 | 检查 `./data/debug.log` 中 `[Auth]` 相关日志 |
| 小爱不响应语音 | 确认「对话监控」+「媒体监控」已启用，设备已勾选 |
| 下载失败 | 检查网易云 cookie 是否过期，酷我搜索是否可访问 |
| 播放卡顿 | 本地 flac 转码可能较慢，考虑预转 mp3 |
| 进度条不更新 | 检查 `/api/player/progress` 响应，UBus 可能未返回 media_data |
| 重复触发 | 确认 `_last_own_play_at` per-device 追踪生效，预热首轮不拦截 |

---

## 📜 更新日志

详见 [app/version.py](app/version.py) 文件头部的版本历史注释。

### 主要里程碑

- **0.0.39** - 两轮深度审阅：文件名清理 + 路径校验（安全加固）
- **0.0.38** - 下载语音指令（"下载当前歌曲" / "下载 XX"）
- **0.0.36** - 全代码库深度审阅修复（CRITICAL 12 + HIGH 19 + MEDIUM 30+）
- **0.0.28** - 双轨监控架构重构（对话轮询 + 媒体状态兜底）
- **0.0.27** - 架构优化：Token 自动刷新 + Dockerfile 三阶段构建（463MB）
- **0.0.22** - SQLite 持久化 + 流式转码防 OOM
- **0.0.6** - 睡眠定时 / 闹钟 / 移动端适配
- **0.0.1** - 项目骨架 + 基础扫描 + Web 管理

---

## 📄 License

本项目仅供个人学习和自用，不得用于商业用途。请遵守当地版权法律，下载的音乐文件仅供个人聆听。

---

<div align="center">

Made with ❤️ for NAS + 小爱音箱用户

</div>
