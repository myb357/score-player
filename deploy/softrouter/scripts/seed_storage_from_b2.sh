#!/usr/bin/env bash
# ============================================================================
# 一次性：把云端 Backblaze B2 现有对象回灌到本地 MinIO（初始化种子数据）。
# 方向与日常同步相反（日常是 本地 ▶ 云端），仅用于首次搭建本地主库时补齐历史文件。
#
# 前置：docker compose up -d 已启动 minio，且已执行 scripts/init_minio.sh 创建 bucket。
# 需要在 .env 中提供 B2 只读凭据：B2_KEY_ID / B2_APP_KEY / B2_ENDPOINT / B2_REGION。
#
# 用法：bash scripts/seed_storage_from_b2.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi

: "${B2_KEY_ID:?请在 .env 提供 B2_KEY_ID（源 B2 读凭据）}"
: "${B2_APP_KEY:?请在 .env 提供 B2_APP_KEY}"
B2_ENDPOINT="${B2_ENDPOINT:-s3.ca-east-006.backblazeb2.com}"
B2_REGION="${B2_REGION:-ca-east-006}"
BUCKET="${B2_BUCKET:-score-player}"
MINIO_USER="${MINIO_ROOT_USER:-scoreadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-change-me-minio}"
MINIO_REGION="${MINIO_REGION:-us-east-1}"

# 通过 rclone 官方镜像，用环境变量声明两个 S3 远端，接入 compose 网络以直连 minio。
NET="$(docker inspect sp-minio -f '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}}{{end}}')"

echo "[seed] 从 B2(${BUCKET}) 复制到本地 MinIO(${BUCKET}) ..."
docker run --rm --network "${NET}" \
  -e RCLONE_CONFIG_B2_TYPE=s3 \
  -e RCLONE_CONFIG_B2_PROVIDER=Other \
  -e RCLONE_CONFIG_B2_ACCESS_KEY_ID="${B2_KEY_ID}" \
  -e RCLONE_CONFIG_B2_SECRET_ACCESS_KEY="${B2_APP_KEY}" \
  -e RCLONE_CONFIG_B2_ENDPOINT="https://${B2_ENDPOINT#https://}" \
  -e RCLONE_CONFIG_B2_REGION="${B2_REGION}" \
  -e RCLONE_CONFIG_MINIO_TYPE=s3 \
  -e RCLONE_CONFIG_MINIO_PROVIDER=Minio \
  -e RCLONE_CONFIG_MINIO_ACCESS_KEY_ID="${MINIO_USER}" \
  -e RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY="${MINIO_PASS}" \
  -e RCLONE_CONFIG_MINIO_ENDPOINT="http://minio:9000" \
  -e RCLONE_CONFIG_MINIO_REGION="${MINIO_REGION}" \
  rclone/rclone:latest \
  copy "b2:${BUCKET}" "minio:${BUCKET}" --transfers 8 --checkers 16 --progress

echo "[seed] 完成。"
