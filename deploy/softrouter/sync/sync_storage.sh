#!/usr/bin/env bash
# ============================================================================
# 对象存储单向同步：本地 MinIO(主) ▶ 云端 Backblaze B2(备)
# 使用 rclone sync（增量：只传输有差异的对象，删除云端多余对象以保持镜像一致）。
# 若不希望删除云端多余对象，把 RCLONE_OP 设为 copy。
#
# 读取 /app/rclone.conf 中的两个远端：minio(源) 与 b2(目的)。
# 通过环境变量 RCLONE_OP 控制：sync(默认,镜像) | copy(只增改不删)
# ============================================================================
set -uo pipefail

BUCKET="${B2_BUCKET:-score-player}"
OP="${RCLONE_OP:-sync}"
CONF="${RCLONE_CONFIG:-/app/rclone.conf}"

echo "[sync_storage] $(date '+%F %T') rclone ${OP} minio:${BUCKET} -> b2:${BUCKET}"

rclone --config "${CONF}" "${OP}" \
  "minio:${BUCKET}" "b2:${BUCKET}" \
  --transfers 8 --checkers 16 \
  --s3-no-check-bucket \
  --stats 30s --stats-one-line

rc=$?
if [ "${rc}" -ne 0 ]; then
  echo "[sync_storage] $(date '+%F %T') rclone 退出码=${rc}（失败，仅记录，不影响本地主库）"
fi
exit "${rc}"
