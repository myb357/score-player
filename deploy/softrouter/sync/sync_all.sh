#!/usr/bin/env bash
# ============================================================================
# 同步总入口：依次执行「数据库同步」+「对象存储同步」。
#
# 关键约束（用户要求）：
#   - 同步失败不影响主库正常使用，只记录日志。
#   - 因此这里不使用 set -e；每一步的失败都被捕获并写入日志，脚本始终以 0 退出，
#     保证 cron 不会因单次失败而告警/中断后续调度；失败详情看日志与退出码记录。
#
# 日志：统一追加到 ${LOG_DIR}/sync.log，并按大小做简单轮转。
# ============================================================================
cd "$(dirname "$0")"

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/sync.log"

# 简单日志轮转：超过 5MB 时归档
if [ -f "${LOG}" ] && [ "$(wc -c < "${LOG}")" -gt 5242880 ]; then
  mv "${LOG}" "${LOG}.$(date '+%Y%m%d%H%M%S')"
  ls -1t "${LOG_DIR}"/sync.log.* 2>/dev/null | tail -n +6 | xargs -r rm -f
fi

{
  echo "==================================================================="
  echo "[sync_all] START $(date '+%F %T %Z')"

  echo "[sync_all] step 1/2 数据库同步 ..."
  python3 /app/sync_db.py
  db_rc=$?
  echo "[sync_all] 数据库同步退出码=${db_rc}"

  echo "[sync_all] step 2/2 对象存储同步 ..."
  bash /app/sync_storage.sh
  st_rc=$?
  echo "[sync_all] 对象存储同步退出码=${st_rc}"

  if [ "${db_rc}" -eq 0 ] && [ "${st_rc}" -eq 0 ]; then
    echo "[sync_all] DONE 全部成功 $(date '+%F %T')"
  else
    echo "[sync_all] DONE 存在失败（db_rc=${db_rc} st_rc=${st_rc}），主库不受影响，等待下次调度重试"
  fi
} >> "${LOG}" 2>&1

# 始终成功退出，避免 cron 判定失败
exit 0
