#!/usr/bin/env bash
# ==============================================================================
# score-player 服务一键迁移脚本
# ==============================================================================
#
# 支持的系统：
#   - iStoreOS / OpenWrt 系系统
#   - 或任何可运行 BusyBox ash + Docker + docker-compose 的 Linux 系统
#
# 不支持的系统：
#   - macOS
#   - 纯 Alpine 且未安装 docker-compose 的环境
#   - Windows
#
# 前置依赖：
#   - docker
#   - docker-compose 或 docker compose 插件
#   - openssl 或 python3 之一，用于解码 Cloudflare Tunnel 凭证
#
# 数据目录前提：
#   - /mnt/nas/score-player-data 需已挂载到持久化存储，例如 NAS。
#   - 若该目录未挂载，脚本会继续创建目录并启动服务，但数据可能仅保存在本机磁盘，
#     不具备预期的持久化能力。
#
# 网络要求：
#   - 运行环境需能访问阿里云 ACR：
#     crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com
#
# Cloudflare Tunnel 说明：
#   - Tunnel 凭证已内嵌在本脚本中，会自动写入 /root/.cloudflared/。
#   - DNS 记录 scoreplayer-myb.top / media.scoreplayer-myb.top 必须已在 Cloudflare 配置，
#     否则公网域名访问不会生效。
#
# 重复执行说明：
#   - 本脚本按幂等方式设计，可重复执行。
#   - 已存在的目录和配置文件会被重新写入。
#   - 已运行的容器会由 docker-compose up -d 自动跳过或按配置更新。
#
# 使用方式：
#   bash migrate.sh
#
# ==============================================================================

set -Eeuo pipefail

log() { echo "[INFO] $*"; }
success() { echo "[OK] $*"; }
fail() { echo "[ERROR] $*" >&2; exit 1; }

trap 'fail "脚本在第 ${LINENO} 行执行失败，请检查上方错误信息。"' ERR

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少必要命令：$1"
}

run_step() {
  local desc="$1"
  shift
  log "$desc"
  "$@"
  success "$desc 完成"
}

log "开始执行 score-player 服务一键迁移脚本"

if [[ "${EUID}" -ne 0 ]]; then
  fail "请使用 root 用户运行本脚本，例如：sudo bash migrate.sh"
fi

require_cmd docker
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
elif docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
else
  fail "缺少 docker-compose 或 docker compose 插件"
fi

if command -v openssl >/dev/null 2>&1; then
  DECODE_METHOD="openssl"
elif command -v python3 >/dev/null 2>&1; then
  DECODE_METHOD="python3"
else
  fail "缺少 openssl 或 python3，无法解码 Cloudflare Tunnel 凭证"
fi

log "步骤 1/9：创建目录结构"
mkdir -p /root/score-player/deploy/softrouter/
mkdir -p /root/score-player/deploy/softrouter/webhook/
mkdir -p /root/.cloudflared/
if [[ ! -d /mnt/nas/score-player-data/ ]]; then
  echo "[WARN] /mnt/nas/score-player-data/ 不存在。请确认 NAS 挂载点是否已正确挂载；脚本将继续执行并创建该目录。" >&2
fi
mkdir -p /mnt/nas/score-player-data/
success "目录结构创建完成"

log "步骤 2/9：写出 .env 文件"
cat > /root/score-player/deploy/softrouter/.env <<'ENV_EOF'
DATA_ROOT=/mnt/nas/score-player-data
PG_USER=score
PG_PASSWORD=Myb!940514
PG_DB=scoredb
PG_PUBLISH=127.0.0.1:5432
MINIO_ROOT_USER=scoreadmin
MINIO_ROOT_PASSWORD=Myb!940514
MINIO_REGION=us-east-1
B2_BUCKET=score-player
MINIO_S3_PUBLISH=0.0.0.0:9002
MINIO_CONSOLE_PUBLISH=127.0.0.1:9001
APP_PUBLISH=9000

# ---- Webhook 自动部署 token（必须与 GitHub 仓库 Secret WEBHOOK_TOKEN 一致，留空请手动填写）----
WEBHOOK_TOKEN=
ENV_EOF
success ".env 文件写出完成：/root/score-player/deploy/softrouter/.env"

log "步骤 3/9：写出 docker-compose.yml 文件"
cat > /root/score-player/deploy/softrouter/docker-compose.yml <<'YAML_EOF'
services:

  db:
    image: docker.m.daocloud.io/postgres:17-alpine
    container_name: sp-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${PG_USER}
      POSTGRES_PASSWORD: ${PG_PASSWORD}
      POSTGRES_DB: ${PG_DB}
    volumes:
      - ${DATA_ROOT}/postgres:/var/lib/postgresql/data
    ports:
      - "${PG_PUBLISH}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${PG_USER} -d ${PG_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [spnet]

  minio:
    image: docker.m.daocloud.io/minio/minio:latest
    container_name: sp-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes:
      - ${DATA_ROOT}/minio:/data
    ports:
      - "${MINIO_S3_PUBLISH}:9000"
      - "${MINIO_CONSOLE_PUBLISH}:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 10s
      retries: 3
    networks: [spnet]

  app:
    image: crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest
    container_name: sp-app
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
      minio:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://${PG_USER}:${PG_PASSWORD}@db:5432/${PG_DB}?sslmode=disable
      B2_ENDPOINT: http://minio:9000
      B2_BUCKET: ${B2_BUCKET}
      B2_KEY_ID: ${MINIO_ROOT_USER}
      B2_APP_KEY: ${MINIO_ROOT_PASSWORD}
      SECRET_KEY: ${SECRET_KEY:-score-player-secret-2026}
      MEDIA_PROXY: "1"
      S3_PUBLIC_ENDPOINT: "https://media.scoreplayer-myb.top"
      COOKIE_SECURE: "0"
    ports:
      - "${APP_PUBLISH:-9000}:8000"
    networks: [spnet]

  cloudflared:
    image: docker.m.daocloud.io/cloudflare/cloudflared:latest
    container_name: sp-cloudflared
    restart: unless-stopped
    command: tunnel --config /home/nonroot/.cloudflared/config.yml run
    volumes:
      - /root/.cloudflared:/home/nonroot/.cloudflared
    network_mode: host

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      REPO_USER: "草书狂澜357"
      REPO_PASS: "Myb!3579510073"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 300 sp-app

  # ---- Webhook 自动部署接收器 ----
  # GitHub Actions 推送镜像后，通过 Cloudflare Tunnel
  # (webhook.scoreplayer-myb.top -> http://172.17.0.1:9003) POST /deploy 触发，
  # 由本容器在软路由本地执行 docker pull + docker-compose up -d --no-deps sp-app。
  #
  # 说明：
  #  - 使用 python:3.11-alpine 基础镜像 + 挂载 server.py，server.py 启动时会
  #    通过 apk 自动安装 docker-cli / docker-cli-compose（基础镜像不自带）。
  #  - 挂载 docker.sock 以便容器内调用宿主机 Docker；挂载 /root/score-player
  #    以便读取部署所用的 docker-compose.yml。
  #  - cloudflared 以 network_mode: host 运行并通过 172.17.0.1 访问宿主机端口，
  #    因此这里必须把 9003 暴露到宿主机的该地址（不能只绑 127.0.0.1，否则
  #    host 网络下的 cloudflared 无法经 172.17.0.1 访问）。与 sp-app 一致地
  #    暴露到宿主机，同时限定在 docker0 网关地址，避免额外暴露到局域网。
  sp-webhook:
    image: python:3.11-alpine
    container_name: sp-webhook
    restart: unless-stopped
    volumes:
      - ./webhook/server.py:/app/server.py:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - /root/score-player:/root/score-player:ro
    working_dir: /app
    command: python server.py
    environment:
      WEBHOOK_TOKEN: ${WEBHOOK_TOKEN}
    ports:
      - "172.17.0.1:9003:9003"
    networks:
      - spnet

networks:
  spnet:
    driver: bridge
YAML_EOF
success "docker-compose.yml 文件写出完成：/root/score-player/deploy/softrouter/docker-compose.yml"

log "步骤 4/9：写出 webhook/server.py 文件"
cat > /root/score-player/deploy/softrouter/webhook/server.py <<'PY_EOF'
#!/usr/bin/env python3
# ============================================================================
# score-player 软路由 Webhook 自动部署接收器
#
#   GitHub Actions 构建并推送镜像完成后，通过 Cloudflare Tunnel 暴露的
#   https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>
#   POST 请求触发本服务，本服务在软路由本地执行：
#       1) docker pull <阿里云 ACR 镜像>
#       2) docker-compose ... up -d --no-deps sp-app
#   从而完成软路由上 sp-app 应用容器的主动更新。
#
# 设计约束：
#   - 仅使用 Python 标准库（http.server），不依赖任何第三方包。
#   - token 从环境变量 WEBHOOK_TOKEN 读取，query param token 校验不通过返回 401。
#   - 所有日志打印到 stdout，便于 `docker logs sp-webhook` 排查。
# ============================================================================
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---- 配置 ----
HOST = "0.0.0.0"
PORT = 9003
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# 与 CI / migrate.sh 保持一致：应用镜像主来源为阿里云 ACR
IMAGE = "crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest"
COMPOSE_FILE = "/root/score-player/deploy/softrouter/docker-compose.yml"
COMPOSE_SERVICE = "sp-app"


def log(msg):
    """统一打印到 stdout 并立即 flush，保证 docker logs 实时可见。"""
    print(f"[webhook] {msg}", flush=True)


def compose_base_cmd():
    """
    探测可用的 compose 命令：
      - 优先使用独立二进制 `docker-compose`（v1）；
      - 回退到 Docker CLI 插件 `docker compose`（v2）。
    这样无论软路由上装的是 v1 还是 v2 都能正常执行。
    """
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def ensure_docker_tooling():
    """
    python:3.11-alpine 基础镜像不自带 docker / docker-compose，
    这里在服务启动时尽力通过 apk 安装 docker-cli 及其 compose 插件。
    失败不阻断服务启动（可能镜像已内置或网络暂时不可用），仅记录日志。
    """
    if shutil.which("docker") and (shutil.which("docker-compose") or shutil.which("docker")):
        # docker 已存在即认为满足基本条件（compose 由 compose_base_cmd 再兜底）
        if shutil.which("docker-compose") or _docker_compose_plugin_ok():
            return
    if not shutil.which("apk"):
        log("WARN: 未检测到 apk，且缺少 docker 工具链，请确保运行环境已内置 docker/docker-compose")
        return
    try:
        log("正在通过 apk 安装 docker-cli / docker-cli-compose ...")
        subprocess.run(
            ["apk", "add", "--no-cache", "docker-cli", "docker-cli-compose"],
            check=True,
            capture_output=True,
            text=True,
        )
        log("docker 工具链安装完成")
    except Exception as e:  # noqa: BLE001 - 最佳努力安装，失败不阻断
        log(f"WARN: 安装 docker 工具链失败（将继续启动，部署时可能报错）：{e}")


def _docker_compose_plugin_ok():
    try:
        subprocess.run(["docker", "compose", "version"], check=True,
                       capture_output=True, text=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_deploy():
    """执行拉取镜像 + 重启 sp-app，返回 (ok: bool, detail: str)。"""
    steps = [
        ["docker", "pull", IMAGE],
        compose_base_cmd() + ["-f", COMPOSE_FILE, "up", "-d", "--no-deps", COMPOSE_SERVICE],
    ]
    outputs = []
    for cmd in steps:
        printable = " ".join(cmd)
        log(f"执行: {printable}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as e:  # noqa: BLE001
            log(f"命令异常: {printable} -> {e}")
            return False, f"command failed: {printable}: {e}"
        if proc.stdout:
            log(proc.stdout.strip())
        if proc.stderr:
            log(proc.stderr.strip())
        if proc.returncode != 0:
            return False, f"command exited {proc.returncode}: {printable}"
        outputs.append(printable)
    return True, "; ".join(outputs)


class DeployHandler(BaseHTTPRequestHandler):
    server_version = "score-player-webhook/1.0"

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 复用统一日志格式，避免 BaseHTTPRequestHandler 默认写 stderr
        log("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "message": "webhook alive"})
            return
        self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/deploy":
            self._json(404, {"status": "error", "message": "not found"})
            return

        # 读取并丢弃请求体（CI 可能带 JSON body），避免连接挂起
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:  # noqa: BLE001
                pass

        token = parse_qs(parsed.query).get("token", [""])[0]
        if not WEBHOOK_TOKEN:
            log("ERROR: 环境变量 WEBHOOK_TOKEN 未配置，拒绝所有部署请求")
            self._json(500, {"status": "error", "message": "WEBHOOK_TOKEN not configured"})
            return
        if token != WEBHOOK_TOKEN:
            log("拒绝: token 校验失败")
            self._json(401, {"status": "error", "message": "invalid token"})
            return

        log("token 校验通过，开始部署 ...")
        ok, detail = run_deploy()
        if ok:
            log("部署触发成功")
            self._json(200, {"status": "ok", "message": "deploy triggered"})
        else:
            log(f"部署失败: {detail}")
            self._json(500, {"status": "error", "message": detail})


def main():
    if not WEBHOOK_TOKEN:
        log("WARN: 启动时未检测到 WEBHOOK_TOKEN 环境变量，部署请求会被拒绝（500）")
    ensure_docker_tooling()
    server = ThreadingHTTPServer((HOST, PORT), DeployHandler)
    log(f"webhook 服务已启动，监听 {HOST}:{PORT}，路由 POST /deploy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，正在关闭 ...")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
PY_EOF
success "webhook/server.py 写出完成：/root/score-player/deploy/softrouter/webhook/server.py"

log "步骤 5/9：写出 Cloudflare Tunnel 凭证文件"
CF_B64='eyJBY2NvdW50VGFnIjoiMzA0MTUyNjgxMzU2ZDk1MzYwOTA4ZDhjYTkwM2ExZWEiLCJUdW5uZWxTZWNyZXQiOiIvV1RCOHEyN3pGVFpRSlYvUG1VSVZ3ZHZCVmZ5SDJIcFhDRFN1RmlndHRBPSIsIlR1bm5lbElEIjoiOTEwODY4NDItM2JiNi00ZmM3LWIxMWYtZDY1YjA4MjRjMzZhIiwiRW5kcG9pbnQiOiIifQ=='
if [[ "${DECODE_METHOD}" == "openssl" ]]; then
  printf '%s' "${CF_B64}" | openssl base64 -d -out /root/.cloudflared/91086842-3bb6-4fc7-b11f-d65b0824c36a.json
else
  python3 - <<'PY_EOF'
import base64
from pathlib import Path
b64 = 'eyJBY2NvdW50VGFnIjoiMzA0MTUyNjgxMzU2ZDk1MzYwOTA4ZDhjYTkwM2ExZWEiLCJUdW5uZWxTZWNyZXQiOiIvV1RCOHEyN3pGVFpRSlYvUG1VSVZ3ZHZCVmZ5SDJIcFhDRFN1RmlndHRBPSIsIlR1bm5lbElEIjoiOTEwODY4NDItM2JiNi00ZmM3LWIxMWYtZDY1YjA4MjRjMzZhIiwiRW5kcG9pbnQiOiIifQ=='
Path('/root/.cloudflared/91086842-3bb6-4fc7-b11f-d65b0824c36a.json').write_bytes(base64.b64decode(b64))
PY_EOF
fi
chmod 600 /root/.cloudflared/91086842-3bb6-4fc7-b11f-d65b0824c36a.json
success "Cloudflare Tunnel 凭证文件写出完成"

log "步骤 6/9：写出 Cloudflare config.yml"
cat > /root/.cloudflared/config.yml <<'YAML_EOF'
tunnel: 91086842-3bb6-4fc7-b11f-d65b0824c36a
credentials-file: /home/nonroot/.cloudflared/91086842-3bb6-4fc7-b11f-d65b0824c36a.json

ingress:
  - hostname: scoreplayer-myb.top
    service: http://172.17.0.1:9000
  - hostname: webhook.scoreplayer-myb.top
    service: http://172.17.0.1:9003
  - hostname: media.scoreplayer-myb.top
    service: http://172.17.0.1:9002
  - service: http_status:404
YAML_EOF
chmod 600 /root/.cloudflared/config.yml
success "Cloudflare config.yml 写出完成：/root/.cloudflared/config.yml"

log "步骤 7/9：登录阿里云 ACR"
echo "Myb!3579510073" | docker login --username="草书狂澜357" --password-stdin crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com
success "阿里云 ACR 登录完成"

log "步骤 8/9：启动服务"
cd /root/score-player/deploy/softrouter
"${COMPOSE_CMD[@]}" up -d
success "服务启动命令执行完成"

log "步骤 9/9：验证容器状态"
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
success "容器状态验证完成"

success "score-player 服务一键迁移流程已执行完成"
