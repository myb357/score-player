#!/usr/bin/env bash
# ============================================================================
# 一次性：从云端 Supabase 导入现有 schema + 数据到本地 PostgreSQL 主库。
#
# 原理：score-player 首次连接本地库时 init_db() 会自动建表（CREATE TABLE IF NOT
#       EXISTS），因此“schema”本可自动生成；本脚本用 pg_dump 把 Supabase 的
#       schema 与数据整体搬到本地，一步到位（含历史数据）。
#
# 前置：
#   - docker compose up -d 已启动 db 服务
#   - 导出 Supabase 连接串（Session Pooler）：
#       export SUPABASE_DATABASE_URL='postgresql://postgres.<ref>:<pwd>@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require'
#
# 用法：SUPABASE_DATABASE_URL='...' bash scripts/import_from_supabase.sh
#
# 说明：pg_dump / psql 均在 db 容器（postgres:16-alpine 自带客户端）内执行，
#       pg_dump 出网连 Supabase，管道直接灌入本地 127.0.0.1 库，无需额外镜像。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi

: "${SUPABASE_DATABASE_URL:?请先 export SUPABASE_DATABASE_URL（Supabase 连接串，含 sslmode=require）}"
PG_USER="${PG_USER:-score}"
PG_PASSWORD="${PG_PASSWORD:-change-me-pg}"
PG_DB="${PG_DB:-scoredb}"

LOCAL_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@127.0.0.1:5432/${PG_DB}?sslmode=disable"

echo "[import] 从 Supabase 导出 schema + 数据 → 灌入本地 ${PG_DB} ..."
echo "[import] 仅迁移 score-player 业务表：users / sessions / scores / pages / score_jobs"

# --clean --if-exists：先 DROP 再 CREATE，保证可重复执行（幂等）。
# -t 精确限定业务表，避免误导 Supabase 内部/扩展对象。
docker compose exec -T db sh -c "
  set -e
  pg_dump --no-owner --no-privileges --clean --if-exists \
    -t public.users -t public.sessions -t public.scores \
    -t public.pages -t public.score_jobs \
    '${SUPABASE_DATABASE_URL}' \
  | psql '${LOCAL_DSN}'
"

echo "[import] 校验本地行数："
docker compose exec -T db psql "${LOCAL_DSN}" -c \
  "SELECT 'users' t, count(*) FROM users UNION ALL SELECT 'scores', count(*) FROM scores UNION ALL SELECT 'pages', count(*) FROM pages UNION ALL SELECT 'score_jobs', count(*) FROM score_jobs;"

echo "[import] 完成。若需要同时把云端对象存储回灌到本地 MinIO，请执行："
echo "         bash scripts/seed_storage_from_b2.sh"
