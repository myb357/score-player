#!/usr/bin/env bash
# ============================================================================
# 初始化本地 MinIO：创建 score-player bucket。
# 幂等：可重复执行；bucket 已存在时不会报错。
# 用法：bash scripts/init_minio.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# 载入 .env（若存在）
if [ -f .env ]; then set -a; . ./.env; set +a; fi

MINIO_USER="${MINIO_ROOT_USER:-scoreadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-change-me-minio}"
BUCKET="${B2_BUCKET:-score-player}"

echo "[init_minio] 通过容器内 mc 创建 bucket: ${BUCKET}"

# 直接复用 minio 容器内自带的 mc；用 compose exec 运行，网络内可直连 http://minio:9000
docker compose exec -T minio sh -c "
  set -e
  mc alias set local http://127.0.0.1:9000 '${MINIO_USER}' '${MINIO_PASS}' >/dev/null 2>&1
  mc mb --ignore-existing local/'${BUCKET}'
  mc anonymous set none local/'${BUCKET}' >/dev/null 2>&1 || true
  echo '[init_minio] 当前 buckets:'
  mc ls local
"

echo "[init_minio] 完成。"
