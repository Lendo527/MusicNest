#!/usr/bin/env python3
"""
OCI Image Builder v4 — 本地构建版。
从 docker save 导出的 python:3.11-slim.tar 导入基础层，带缓存。

流程：
  1. 从 CACHE_DIR/python311-slim.tar 导入基础层 + 构建 rootfs
  2. pip install 依赖 (离线 wheels 优先)
  3. 复制 app 代码
  4. 只打包 app code + data + music 到 app layer (不打包 OS)
  5. 输出 docker-load 兼容的 .tar
"""

import hashlib, gzip, json, os, shutil, subprocess, sys, tarfile, tempfile, time
from pathlib import Path

IMAGE_VERSION = "0.0.28"
# 版本历史：
#   0.0.28 - 语音拦截重构：双轨检测（轨道1对话轮询0.2s+轨道2播放状态高频轮询），
#           时间窗口2s→30s防漏拦，stop_all_media改并发发送抢先劫持，
#           元数据查询改fire-and-forget不阻塞播放，修复index.html第4090行引号嵌套语法错误
#   0.0.27 - 架构优化：Token 自动刷新、smart_resume、SearchProvider ABC、增量同步、网易云歌词；
#           Dockerfile 改为三阶段构建（alpine ffmpeg + deps + 运行镜像），体积 893MB→463MB；
#           build_oci 分层优化（deps/ffmpeg/app 独立层，NAS 重复构建时前两层 digest 不变可复用）
#   0.0.22 - Bug 大修复：SQLite 持久化、StreamingResponse 防 OOM、路径校验、SHUFFLE 去重、
#           扫码长轮询超时、监控异常日志、循环导入回调注入、token_refresh 401 自动重试
#   0.0.15 - 修复进度条不更新（UBus data.info.play_song_detail 嵌套解析）
#   0.0.6  - 睡眠定时/闹钟/TTS 反馈/移动端适配
#   0.0.1  - 项目骨架 + 基础扫描 + Web 管理
WORKDIR = Path("/opt/data/profiles/coder/workspace/musicnest")
OUTPUT = Path("/opt/data/cache/docker-images") / f"musicnest-v{IMAGE_VERSION}.tar"
DOCKER_TAG = f"musicnest:{IMAGE_VERSION}"
CACHE_DIR = Path("/opt/data/cache/docker-env")
BASE_TAR = CACHE_DIR / "python311-slim.tar"

DEPENDENCIES = ["fastapi", "uvicorn[standard]", "httpx", "aiofiles", "pyyaml", "jinja2"]

# ── Helpers ────────────────────────────────────────────────────────

def sha256_of(data_or_path):
    """计算 SHA256，支持 str(path) / Path / bytes"""
    if isinstance(data_or_path, (str, Path)):
        h = hashlib.sha256()
        with open(data_or_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    return hashlib.sha256(data_or_path).hexdigest()


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def write_json(path, data):
    with open(path, "wb") as f:
        f.write(json.dumps(data, indent=2).encode())


# ── Main ───────────────────────────────────────────────────────────

def main():
    tmp = Path(tempfile.mkdtemp(prefix="oci_build_"))
    layers_dir = tmp / "layers"
    rootfs = tmp / "rootfs"
    blobs_dir = tmp / "blobs" / "sha256"
    layers_dir.mkdir(parents=True)
    blobs_dir.mkdir(parents=True)

    # ── Step 1: 检查缓存 / 从 docker save tar 导入 ─────────────
    cache_valid = False
    cache_layers = CACHE_DIR / "layers"
    cache_rootfs = CACHE_DIR / "rootfs"
    cache_config = CACHE_DIR / "config.json"
    cache_key_file = CACHE_DIR / "manifest_hash.txt"

    # 检查缓存是否有效
    if cache_layers.is_dir() and cache_rootfs.is_dir() and cache_config.exists():
        cached_layer_files = sorted([
            f for f in cache_layers.iterdir()
            if f.name.endswith((".tar.gz", ".tar")) and f.name != "app_layer.tar.gz"
        ])
        if cached_layer_files:
            # 验证缓存一致性：hash layer 文件名
            h = hashlib.sha256("".join(f.name for f in cached_layer_files).encode()).hexdigest()
            saved = cache_key_file.read_text().strip() if cache_key_file.exists() else ""
            if h == saved:
                print("[1/5] ✅ 缓存命中！直接使用缓存的基础层", flush=True)
                for f in cached_layer_files:
                    shutil.copy2(f, layers_dir / f.name)
                # 恢复 rootfs (用 rsync 或 copytree)
                if cache_rootfs.exists():
                    for item in cache_rootfs.iterdir():
                        dst = rootfs / item.name
                        if item.is_dir():
                            shutil.copytree(str(item), str(dst), symlinks=True, dirs_exist_ok=True)
                        else:
                            shutil.copy2(str(item), str(dst))
                config_data = cache_config.read_bytes()
                base_config = json.loads(config_data)
                base_history = base_config.get("history", [])
                print(f"    恢复 {len(cached_layer_files)} 层 + config ({len(base_history)} history)", flush=True)
                cache_valid = True

    if not cache_valid:
        # 从 docker save tar 导入
        if not BASE_TAR.exists():
            print("❌ 找不到基础镜像: python311-slim.tar", flush=True)
            print("   请先执行: docker pull python:3.11-slim && docker save python:3.11-slim -o /opt/data/cache/docker-env/python311-slim.tar", flush=True)
            sys.exit(1)

        print("[1/5] 从 docker save tar 导入基础镜像...", flush=True)
        with tarfile.open(BASE_TAR) as tf:
            members = {m.name: m for m in tf.getmembers()}
            manifest_list = json.loads(tf.extractfile("manifest.json").read())
            img_manifest = manifest_list[0]
            layer_names = img_manifest.get("Layers", [])
            config_name = img_manifest.get("Config", "config.json")

            print(f"    {len(layer_names)} 层", flush=True)
            for ln in layer_names:
                fname = ln.replace("blobs/sha256/", "") + ".tar.gz"
                print(f"      提取 {Path(ln).name}...", flush=True)
                data = tf.extractfile(ln).read()
                (layers_dir / fname).write_bytes(data)

            print(f"    提取 config ({config_name})...", flush=True)
            config_data = tf.extractfile(config_name).read()
            base_config = json.loads(config_data)
            base_history = base_config.get("history", [])

            print("    构建 rootfs...", flush=True)
            for ln in layer_names:
                fname = ln.replace("blobs/sha256/", "") + ".tar.gz"
                src_gz = layers_dir / fname
                if src_gz.exists():
                    with tarfile.open(src_gz, "r:gz") as subtf:
                        subtf.extractall(str(rootfs))

        # 写入缓存
        print("    缓存基础层...", flush=True)
        cache_layers.mkdir(parents=True, exist_ok=True)
        for f in layers_dir.iterdir():
            if f.name.endswith((".tar.gz", ".tar")):
                shutil.copy2(f, cache_layers / f.name)
        if cache_rootfs.exists():
            shutil.rmtree(str(cache_rootfs))
        shutil.copytree(str(rootfs), str(cache_rootfs), symlinks=True)
        cache_config.write_bytes(config_data)
        # 计算缓存 key
        cached_files = sorted([f for f in cache_layers.iterdir() if f.name.endswith((".tar.gz", ".tar")) and f.name != "app_layer.tar.gz"])
        h = hashlib.sha256("".join(f.name for f in cached_files).encode()).hexdigest()
        cache_key_file.write_text(h)
        print(f"    缓存已保存 (key={h[:12]}...)", flush=True)

    # 构建 downloaded_layers (供 OCI manifest 使用)
    downloaded_layers = []
    for f in sorted(layers_dir.iterdir()):
        if f.name.endswith((".tar.gz", ".tar")) and f.name != "app_layer.tar.gz":
            digest = "sha256:" + f.name.replace(".tar.gz", "")
            downloaded_layers.append({
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": digest,
                "size": f.stat().st_size,
            })

    # ── Step 2: pip 安装依赖 ──────────────────────────────────
    print("[2/5] 安装 Python 依赖...", flush=True)

    site_packages = rootfs / "usr" / "local" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    rootfs_python = rootfs / "usr" / "local" / "bin" / "python3"
    if not rootfs_python.exists():
        rootfs_python = rootfs / "usr" / "bin" / "python3"

    env = os.environ.copy()
    env.update({
        "PATH": str(rootfs / "usr" / "local" / "bin") + ":" + str(rootfs / "usr" / "bin") + ":" + env.get("PATH", ""),
        "LD_LIBRARY_PATH": str(rootfs / "usr" / "local" / "lib") + ":" + str(rootfs / "usr" / "lib") + ":" + str(rootfs / "lib"),
    })
    # 清空代理以确保直连
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env.pop(k, None)

    pip_wheels_dir = CACHE_DIR / "pip-wheels"
    wheel_files = list(pip_wheels_dir.glob("*.whl"))
    pip_cmd = [str(rootfs_python), "-m", "pip", "install"]
    if wheel_files:
        pip_cmd += ["--only-binary=:all:", f"--find-links={pip_wheels_dir}"]
        print(f"    使用离线 wheels 加速 ({len(wheel_files)} 个)", flush=True)
    deps_clean = [d.split("[")[0] for d in DEPENDENCIES]
    pip_cmd += deps_clean

    result = subprocess.run(pip_cmd, env=env, capture_output=True, timeout=300)
    if result.returncode != 0:
        print(f"    pip 失败 (exit {result.returncode})", flush=True)
        print(f"    stderr: {result.stderr.decode(errors='replace')[-500:]}", flush=True)
        raise RuntimeError("pip install failed")
    print("    pip 安装完成", flush=True)

    # ── Step 3: ffmpeg + app 代码 ─────────────────────────────
    print("[3/5] 安装 ffmpeg...", flush=True)
    ffmpeg_cache = CACHE_DIR / "ffmpeg-release-amd64-static.tar.xz"
    try:
        if ffmpeg_cache.exists():
            ffmpeg_tarball = ffmpeg_cache
            print(f"    使用缓存 ffmpeg ({ffmpeg_cache.stat().st_size/1024/1024:.0f} MB)", flush=True)
        else:
            ffmpeg_url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
            print(f"    下载 ffmpeg...", flush=True)
            subprocess.run(["curl", "-sSL", "-o", str(tmp / "ffmpeg.tar.xz"), ffmpeg_url], check=True, capture_output=True)
            ffmpeg_tarball = tmp / "ffmpeg.tar.xz"
            shutil.copy2(ffmpeg_tarball, ffmpeg_cache)
        subprocess.run(["tar", "-xJf", str(ffmpeg_tarball), "-C", str(tmp)], check=True, capture_output=True)
        ffmpeg_dir = None
        for item in tmp.iterdir():
            if item.is_dir() and item.name.startswith("ffmpeg-"):
                ffmpeg_dir = item
                break
        if ffmpeg_dir:
            (rootfs / "usr" / "bin").mkdir(parents=True, exist_ok=True)
            shutil.copy(ffmpeg_dir / "ffmpeg", rootfs / "usr" / "bin" / "ffmpeg")
            shutil.copy(ffmpeg_dir / "ffprobe", rootfs / "usr" / "bin" / "ffprobe")
            (rootfs / "usr" / "bin" / "ffmpeg").chmod(0o755)
            (rootfs / "usr" / "bin" / "ffprobe").chmod(0o755)
            print("    ffmpeg 安装完成", flush=True)
    except Exception as e:
        print(f"    ffmpeg 安装跳过 (非致命): {e}", flush=True)

    # 修复 shebang (pip 可能写了 /tmp 路径)
    print("    修正 bin 脚本 shebang...", flush=True)
    bin_dir = rootfs / "usr" / "local" / "bin"
    if bin_dir.exists():
        for script in bin_dir.iterdir():
            if script.is_file() and not script.is_symlink():
                try:
                    raw = script.read_bytes()
                    if raw.startswith(b"#!") and b"/tmp/" in raw.split(b"\n")[0]:
                        rest = b"\n".join(raw.split(b"\n")[1:])
                        script.write_bytes(b"#!/usr/local/bin/python3\n" + rest)
                        script.chmod(0o755)
                except Exception:
                    pass

    print("    复制 app 代码...", flush=True)
    dst_app = rootfs / "app" / "app"
    if dst_app.exists():
        shutil.rmtree(dst_app)
    shutil.copytree(str(WORKDIR / "app"), str(dst_app))
    (rootfs / "data").mkdir(exist_ok=True)
    (rootfs / "music").mkdir(exist_ok=True)

    # ── Step 4: 构建分层（deps / ffmpeg / app 三层分离） ─────
    # 拆分目的：deps 和 ffmpeg 变化极少，app 代码变化频繁。
    # 分层后 NAS 重复构建时，前两层内容不变 → digest 不变 → 可被 registry/容器复用，
    # 只需重传 app 层，大幅减少构建产物体积和网络传输。
    print("[4/5] 构建分层 (deps / ffmpeg / app)...", flush=True)

    def _build_layer(layer_name: str, add_fn):
        """构建单个 OCI layer：打包 → gzip → 计算 digest/size → 重命名

        Args:
            layer_name: 层名（用于日志），如 "deps"
            add_fn: callable(tarfile.TarFile)，负责向 tar 添加该层文件
        Returns:
            (digest, size, diff_id) 三元组
        """
        uncompressed = layers_dir / f"{layer_name}.tar"
        with tarfile.open(uncompressed, "w") as tf:
            add_fn(tf)
        diff_id = "sha256:" + sha256_of(uncompressed)
        compressed = layers_dir / f"{layer_name}.tar.gz"
        with open(uncompressed, "rb") as src, gzip.open(compressed, "wb") as dst:
            shutil.copyfileobj(src, dst)
        digest = "sha256:" + sha256_of(compressed)
        size = compressed.stat().st_size
        named = layers_dir / f"{digest.replace('sha256:', '')}.tar.gz"
        if compressed != named:
            shutil.move(str(compressed), str(named))
        # 清理未压缩中间文件
        uncompressed.unlink(missing_ok=True)
        print(f"    {layer_name} layer: {digest[:20]}... ({size/1024/1024:.1f} MB)", flush=True)
        return digest, size, diff_id

    # deps layer: pip 安装的 site-packages + bin 脚本（变化极少）
    def _add_deps(tf):
        sp = rootfs / "usr" / "local" / "lib" / "python3.11" / "site-packages"
        if sp.exists():
            tf.add(str(sp), "usr/local/lib/python3.11/site-packages")
        bin_dir = rootfs / "usr" / "local" / "bin"
        if bin_dir.exists():
            for item in bin_dir.iterdir():
                if item.is_file() and not item.is_symlink():
                    tf.add(str(item), f"usr/local/bin/{item.name}")

    deps_digest, deps_size, deps_diff_id = _build_layer("deps", _add_deps)

    # ffmpeg layer: ffmpeg/ffprobe 二进制（几乎不变）
    def _add_ffmpeg(tf):
        for bin_name in ["ffmpeg", "ffprobe"]:
            bin_path = rootfs / "usr" / "bin" / bin_name
            if bin_path.exists():
                tf.add(str(bin_path), f"usr/bin/{bin_name}")

    ffmpeg_digest, ffmpeg_size, ffmpeg_diff_id = _build_layer("ffmpeg", _add_ffmpeg)

    # app layer: app 代码 + 空数据目录（变化频繁）
    def _add_app(tf):
        if (rootfs / "app" / "app").exists():
            tf.add(str(rootfs / "app" / "app"), "app/app")
        for d in ["data", "music"]:
            dp = str(rootfs / d)
            if os.path.isdir(dp):
                tf.add(dp, d)

    app_digest, app_size, app_diff_id = _build_layer("app", _add_app)

    # 计算基础层 diff_ids
    print("    计算基础层 diff_ids...", flush=True)
    base_diff_ids = []
    for layer in downloaded_layers:
        fname = layers_dir / f"{layer['digest'].replace('sha256:', '')}.tar.gz"
        with gzip.open(fname, "rb") as gz:
            raw_data = gz.read()
        base_diff_ids.append("sha256:" + sha256_of(raw_data))

    # ── Step 5: 组装 OCI 镜像 ─────────────────────────────────
    print("[5/5] 组装 OCI 镜像...", flush=True)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # 自定义层：deps / ffmpeg / app 三个独立层
    custom_layers = [
        {"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
         "digest": deps_digest, "size": deps_size, "diff_id": deps_diff_id,
         "created_by": "pip install dependencies"},
        {"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
         "digest": ffmpeg_digest, "size": ffmpeg_size, "diff_id": ffmpeg_diff_id,
         "created_by": "copy ffmpeg binaries"},
        {"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
         "digest": app_digest, "size": app_size, "diff_id": app_diff_id,
         "created_by": "copy app code"},
    ]

    config = {
        "created": now_iso,
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": ["PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
            "WorkingDir": "/app",
            "Cmd": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "58092"],
            "ExposedPorts": {"58092/tcp": {}},
        },
        "rootfs": {
            "type": "layers",
            "diff_ids": base_diff_ids + [l["diff_id"] for l in custom_layers],
        },
        "history": base_history + [
            {"created": now_iso, "created_by": l["created_by"]} for l in custom_layers
        ],
    }
    config_raw = json.dumps(config, indent=2).encode()
    config_hash = "sha256:" + sha256_of(config_raw)

    # all_layers 用于 manifest: 基础层 + 自定义层（去掉 diff_id/created_by 等非 manifest 字段）
    all_layers = downloaded_layers + [
        {"mediaType": l["mediaType"], "digest": l["digest"], "size": l["size"]}
        for l in custom_layers
    ]

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(config_raw),
            "digest": config_hash,
        },
        "layers": all_layers,
    }

    # 写入 blobs
    (blobs_dir / config_hash.replace("sha256:", "")).write_bytes(config_raw)
    for layer in all_layers:
        fname = layers_dir / f"{layer['digest'].replace('sha256:', '')}.tar.gz"
        if fname.exists():
            shutil.copy(fname, blobs_dir / layer['digest'].replace("sha256:", ""))

    # 写入 manifest
    manifest_path = tmp / "manifest.json"
    write_json(manifest_path, [{
        "Config": f"blobs/sha256/{config_hash.replace('sha256:', '')}",
        "RepoTags": [DOCKER_TAG],
        "Layers": [f"blobs/sha256/{l['digest'].replace('sha256:', '')}" for l in all_layers],
    }])

    # 打包最终产物
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        os.remove(OUTPUT)
    with tarfile.open(OUTPUT, "w") as out_tf:
        # blobs 目录
        for f in blobs_dir.iterdir():
            out_tf.add(str(f), f"blobs/sha256/{f.name}")
        # manifest
        out_tf.add(str(manifest_path), "manifest.json")

    total_mb = OUTPUT.stat().st_size / 1024 / 1024
    print(f"\n✅ 构建完成: {OUTPUT}")
    print(f"   大小: {total_mb:.1f} MB")
    print(f"   标签: {DOCKER_TAG}")
    print(f"   加载: docker load -i {OUTPUT}", flush=True)

    # 清理
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
