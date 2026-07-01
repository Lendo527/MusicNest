# 多阶段构建：分离依赖安装与运行环境，减小最终镜像体积

# ── Stage 1: 下载静态编译 ffmpeg ──
FROM alpine:3.19 AS ffmpeg-builder
# 使用 johnvansickle 静态编译版（~70MB），远小于 apt 的 ffmpeg（461MB 含依赖库）
ARG FFMPEG_VERSION=release
RUN apk add --no-cache curl xz \
    && curl -sSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-${FFMPEG_VERSION}-amd64-static.tar.xz" -o /tmp/ffmpeg.tar.xz \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/ffmpeg-*-static/ffmpeg /tmp/ffmpeg \
    && mv /tmp/ffmpeg-*-static/ffprobe /tmp/ffprobe \
    && chmod +x /tmp/ffmpeg /tmp/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-*-static

# ── Stage 2: 安装 Python 依赖（带缓存挂载，加速重复构建）──
FROM python:3.11-slim AS deps-builder
WORKDIR /build
COPY pyproject.toml .
# 单独安装依赖层，利用 docker 层缓存（pyproject.toml 不变时跳过重装）
RUN pip install --no-cache-dir --user \
    fastapi uvicorn[standard] httpx aiofiles pyyaml jinja2

# ── Stage 3: 最终运行镜像 ──
FROM python:3.11-slim

WORKDIR /app

# 从 stage 1 复制静态 ffmpeg（~70MB，替代 apt 的 461MB）
COPY --from=ffmpeg-builder /tmp/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-builder /tmp/ffprobe /usr/local/bin/ffprobe

# 从 stage 2 复制 pip 依赖（已安装在 /root/.local）
COPY --from=deps-builder /root/.local /root/.local

# 确保 local bin 在 PATH
ENV PATH=/root/.local/bin:$PATH

# 复制应用代码（变化最频繁，放最后一层）
COPY app/ ./app/

RUN mkdir -p /data /music

EXPOSE 58092

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "58092"]
