#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库单向同步：本地 PostgreSQL(主库) ▶ 云端 Supabase(备库)

设计要点（对齐用户约束）：
  - 单向：只读本地、只写云端。全程对本地库仅执行 SELECT，绝不修改主库。
  - 同步失败不影响主库：任何异常仅记录日志并以非零码退出，不触碰本地数据。
  - 云端写入具备事务性：full 模式在单个事务内 TRUNCATE + 重灌，失败自动回滚，
    云端保持上一次的完整快照，不会出现“写一半”的半损坏状态。

模式：
  full   （默认）：镜像式全量刷新。云端先 TRUNCATE 再按本地重灌，云端 == 本地。
  upsert         ：增量式。按主键 INSERT ... ON CONFLICT DO UPDATE，只增改不删。

环境变量：
  LOCAL_DATABASE_URL   本地主库连接串，例：postgresql://score:pwd@db:5432/scoredb?sslmode=disable
  CLOUD_DATABASE_URL   云端 Supabase 连接串（含 sslmode=require）
  SYNC_MODE            full | upsert （默认 full）
  SYNC_SESSIONS        1 则同步 sessions 表（默认 0，登录会话无需上云）
"""
import os
import sys
import time

import psycopg2
import psycopg2.extras

# 父表在前，保证插入时外键可满足
TABLES = ["users", "scores", "pages", "score_jobs", "sessions"]
# 全量模式需要清空的全集（含 sessions，保证云端是干净镜像）
ALL_TABLES = ["users", "scores", "pages", "score_jobs", "sessions"]
PK = {"users": "id", "scores": "id", "pages": "id", "score_jobs": "id", "sessions": "token"}
SERIAL_TABLES = ["users", "scores", "pages"]  # id 为 SERIAL，需要同步序列；score_jobs.id 为 TEXT


def log(msg: str) -> None:
    print(f"[sync_db] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def get_columns(cur, table: str):
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_rows(local_cur, table, cols):
    col_list = ", ".join(f'"{c}"' for c in cols)
    local_cur.execute(f"SELECT {col_list} FROM {table}")
    return local_cur.fetchall()


def reset_sequences(cloud_cur, tables):
    for t in tables:
        if t in SERIAL_TABLES:
            cloud_cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {t}), 1), true)"
            )


def sync_full(local_cur, cloud_conn, cloud_cur, tables):
    # 先把本地全部数据读到内存（业务量级很小，安全）
    data = {}
    cols_map = {}
    for t in tables:
        cols = get_columns(local_cur, t)
        cols_map[t] = cols
        data[t] = fetch_rows(local_cur, t, cols)
        log(f"本地读取 {t}: {len(data[t])} 行")

    # 云端：单事务内清空并重灌（失败整体回滚）
    cloud_cur.execute(
        "TRUNCATE {} RESTART IDENTITY CASCADE".format(", ".join(ALL_TABLES))
    )
    log("云端已 TRUNCATE 全部业务表（事务内）")
    for t in tables:
        cols = cols_map[t]
        rows = data[t]
        if not rows:
            continue
        col_list = ", ".join(f'"{c}"' for c in cols)
        tmpl = "(" + ", ".join(["%s"] * len(cols)) + ")"
        psycopg2.extras.execute_values(
            cloud_cur,
            f"INSERT INTO {t} ({col_list}) VALUES %s",
            rows,
            template=tmpl,
            page_size=500,
        )
        log(f"云端写入 {t}: {len(rows)} 行")
    reset_sequences(cloud_cur, tables)
    cloud_conn.commit()
    log("full 同步提交成功")


def sync_upsert(local_cur, cloud_conn, cloud_cur, tables):
    for t in tables:
        cols = get_columns(local_cur, t)
        rows = fetch_rows(local_cur, t, cols)
        log(f"本地读取 {t}: {len(rows)} 行")
        if not rows:
            continue
        col_list = ", ".join(f'"{c}"' for c in cols)
        tmpl = "(" + ", ".join(["%s"] * len(cols)) + ")"
        pk = PK[t]
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != pk)
        sql = (
            f"INSERT INTO {t} ({col_list}) VALUES %s "
            f"ON CONFLICT (\"{pk}\") DO UPDATE SET {updates}"
        )
        psycopg2.extras.execute_values(cloud_cur, sql, rows, template=tmpl, page_size=500)
        log(f"云端 upsert {t}: {len(rows)} 行")
    reset_sequences(cloud_cur, tables)
    cloud_conn.commit()
    log("upsert 同步提交成功")


def main() -> int:
    local_url = os.environ.get("LOCAL_DATABASE_URL", "")
    cloud_url = os.environ.get("CLOUD_DATABASE_URL", "")
    mode = os.environ.get("SYNC_MODE", "full").strip().lower()
    if not local_url or not cloud_url:
        log("错误：LOCAL_DATABASE_URL / CLOUD_DATABASE_URL 未配置")
        return 2

    tables = list(TABLES)
    if os.environ.get("SYNC_SESSIONS", "0") != "1":
        tables = [t for t in tables if t != "sessions"]

    local_conn = cloud_conn = None
    try:
        local_conn = psycopg2.connect(local_url)
        local_conn.set_session(readonly=True, autocommit=False)  # 硬保证：本地只读
        cloud_conn = psycopg2.connect(cloud_url)
        cloud_conn.autocommit = False
        with local_conn.cursor() as lc, cloud_conn.cursor() as cc:
            log(f"开始同步，mode={mode}, tables={tables}")
            if mode == "upsert":
                sync_upsert(lc, cloud_conn, cc, tables)
            else:
                sync_full(lc, cloud_conn, cc, tables)
        return 0
    except Exception as e:  # noqa: BLE001
        if cloud_conn is not None:
            try:
                cloud_conn.rollback()
                log("云端事务已回滚，云端保持上一次完整快照")
            except Exception:
                pass
        log(f"同步失败（主库未受影响）：{e!r}")
        return 1
    finally:
        for c in (local_conn, cloud_conn):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
