#!/usr/bin/env bash
# ============================================================================
# 只读一致性巡检：本地 PostgreSQL / MinIO 与云端 Supabase / B2 对账。
#
# 约束：
#   - 只读检查，不修改本地或云端数据。
#   - CLOUD_DATABASE_URL 未配置时跳过云端数据库检查，但仍检查本地库与对象存储。
#   - 输出追加到 ${LOG_DIR}/consistency.log。
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")"
LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/consistency.log"

DB_SQL="select chr(117)||chr(115)||chr(101)||chr(114)||chr(115), count(*) from users union all select chr(115)||chr(99)||chr(111)||chr(114)||chr(101)||chr(115), count(*) from scores union all select chr(112)||chr(97)||chr(103)||chr(101)||chr(115), count(*) from pages union all select chr(115)||chr(99)||chr(111)||chr(114)||chr(101)||chr(95)||chr(106)||chr(111)||chr(98)||chr(115), count(*) from score_jobs union all select chr(115)||chr(101)||chr(115)||chr(115)||chr(105)||chr(111)||chr(110)||chr(115), count(*) from sessions order by 1;"

{
  echo "==================================================================="
  echo "[check] START $(date '+%Y-%m-%d %H:%M:%S')"

  echo "[check] local database counts"
  docker exec sp-postgres psql -U score -d scoredb -Atc "${DB_SQL}" 2>&1 || true

  echo "[check] local object storage size"
  docker-compose -f docker-compose.sync.yml run --rm sync \
    rclone --config /app/rclone.conf size "minio:${B2_BUCKET:-score-player}" 2>&1 || true

  echo "[check] cloud object storage size"
  docker-compose -f docker-compose.sync.yml run --rm sync \
    rclone --config /app/rclone.conf size "b2:${B2_BUCKET:-score-player}" 2>&1 || true

  if grep -q '^CLOUD_DATABASE_URL=.' .env.sync 2>/dev/null; then
    echo "[check] cloud database counts"
    docker-compose -f docker-compose.sync.yml run --rm -e DB_SQL="${DB_SQL}" sync python3 - <<'PY'
import os
import psycopg2

url = os.environ.get("CLOUD_DATABASE_URL", "")
sql = os.environ.get("DB_SQL")
conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute(sql)
for row in cur.fetchall():
    print(f"{row[0]}|{row[1]}")
conn.close()
PY
  else
    echo "[check] cloud database skipped: CLOUD_DATABASE_URL is not configured"
  fi

  echo "[check] DONE $(date '+%Y-%m-%d %H:%M:%S')"
} >> "${LOG}" 2>&1

cat "${LOG}" | tail -80
