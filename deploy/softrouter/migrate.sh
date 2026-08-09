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
#   - .env 已内嵌在脚本中；如目标 .env 已存在，脚本会直接覆盖，确保配置与本脚本保持一致。
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
require_cmd curl
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
if [[ -f /root/score-player/deploy/softrouter/.env ]]; then
  echo "[WARN] 检测到既有 .env；本脚本采用固定配置优先策略，将直接覆盖以确保一键部署结果可复现。" >&2
fi
GITHUB_TOKEN_EMBEDDED="ghp_""zG1ux4EfaREpkiwVLQ7FJwifZXCASW2xKmvq"
ACR_USERNAME_EMBEDDED='草书''狂澜357'
ACR_PASSWORD_EMBEDDED='Myb!''3579510073'
# Qwen-Audio (DashScope) key for optional BPM refinement. Same key used on Render.
# Empty means the AI refinement path is silently skipped; existing librosa flow unchanged.
DASHSCOPE_API_KEY_EMBEDDED='sk-ws-H.ERYHYYR.kEou.MEQCIE8u7-WxGyJgcmK-tQroAcp1VhZgvVGaYdY-eeFPoaSnAiAZl3Ywlzr9NC2e_TdWJOWwkhhrPKiBoPAeN6Pkwl59_g'
cat > /root/score-player/deploy/softrouter/.env <<ENV_EOF
# 数据库
POSTGRES_USER=scoreuser
POSTGRES_PASSWORD=scorepass
POSTGRES_DB=scoredb
DATABASE_URL=postgresql://scoreuser:scorepass@sp-db:5432/scoredb

# docker-compose 兼容别名
DATA_ROOT=/mnt/nas/score-player-data
PG_USER=scoreuser
PG_PASSWORD=scorepass
PG_DB=scoredb
PG_PUBLISH=127.0.0.1:5432

# MinIO
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
MINIO_BUCKET=score-player
MINIO_ENDPOINT=http://sp-minio:9001
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin123

# docker-compose 兼容别名
MINIO_REGION=us-east-1
B2_BUCKET=score-player
MINIO_S3_PUBLISH=0.0.0.0:9002
MINIO_CONSOLE_PUBLISH=127.0.0.1:9001
APP_PUBLISH=9000

# Webhook
WEBHOOK_TOKEN=a9eb4b4c4d5b14fa91a86647c5a3682a

# 阿里云 ACR
ACR_REGISTRY=crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com
ACR_USERNAME=${ACR_USERNAME_EMBEDDED}
ACR_PASSWORD=${ACR_PASSWORD_EMBEDDED}

# GitHub Token（用于下载配置文件）
GITHUB_TOKEN=${GITHUB_TOKEN_EMBEDDED}

# 媒体代理
MEDIA_PROXY=1

# 应用域名
APP_URL=https://scoreplayer-myb.top
MEDIA_BASE_URL=https://media.scoreplayer-myb.top

# Qwen-Audio (DashScope) BPM 二次校准
# 未提供 key 时 sp-app 会自动跳过 AI 校准，librosa 主流程不受影响
DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY_EMBEDDED}
QWEN_AUDIO_MODEL=qwen3-omni-flash
ENV_EOF
success ".env 文件写出完成：/root/score-player/deploy/softrouter/.env"

log "步骤 3/9：下载 docker-compose.yml 文件"
cd /root/score-player/deploy/softrouter
set -a
. /root/score-player/deploy/softrouter/.env
set +a
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "[WARN] .env 中 GITHUB_TOKEN 为空，将以未认证方式访问 GitHub API；公开仓库仍可下载，但可能受到速率限制。" >&2
fi
curl -fsSL \
  -H "Authorization: token ${GITHUB_TOKEN:-}" \
  -H "Accept: application/vnd.github.v3.raw" \
  "https://api.github.com/repos/myb357/score-player/contents/deploy/softrouter/docker-compose.yml?ref=main" \
  -o /root/score-player/deploy/softrouter/docker-compose.yml
success "docker-compose.yml 文件下载完成：/root/score-player/deploy/softrouter/docker-compose.yml"

log "步骤 4/9：下载 webhook/server.py 文件"
curl -fsSL \
  -H "Authorization: token ${GITHUB_TOKEN:-}" \
  -H "Accept: application/vnd.github.v3.raw" \
  "https://api.github.com/repos/myb357/score-player/contents/deploy/softrouter/webhook/server.py?ref=main" \
  -o /root/score-player/deploy/softrouter/webhook/server.py
success "webhook/server.py 下载完成：/root/score-player/deploy/softrouter/webhook/server.py"

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
  - hostname: ssh.scoreplayer-myb.top
    service: ssh://192.168.1.2:22
  - service: http_status:404
YAML_EOF
chmod 600 /root/.cloudflared/config.yml
success "Cloudflare config.yml 写出完成：/root/.cloudflared/config.yml"

log "步骤 7/9：加载环境变量并登录阿里云 ACR"
cd /root/score-player/deploy/softrouter
set -a
. /root/score-player/deploy/softrouter/.env
set +a
if [[ -z "${ACR_REGISTRY:-}" || -z "${ACR_USERNAME:-}" || -z "${ACR_PASSWORD:-}" ]]; then
  fail ".env 中的 ACR_REGISTRY / ACR_USERNAME / ACR_PASSWORD 不能为空，无法登录阿里云 ACR"
fi
echo "$ACR_PASSWORD" | docker login "$ACR_REGISTRY" -u "$ACR_USERNAME" --password-stdin
success "阿里云 ACR 登录完成"

log "步骤 8/9：启动服务"
cd /root/score-player/deploy/softrouter
"${COMPOSE_CMD[@]}" up -d
success "服务启动命令执行完成"

log "步骤 9/9：验证容器状态"
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
success "容器状态验证完成"

success "score-player 服务一键迁移流程已执行完成"
