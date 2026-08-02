# ============================================================================
# 同步任务容器镜像：内置 python(psycopg2) + rclone + supercronic(容器友好 cron)
# 在软路由上以容器方式常驻，按 crontab 定时执行 本地 ▶ 云端 单向同步。
# ============================================================================
FROM python:3.11-slim

ARG SUPERCRONIC_VERSION=v0.2.29
ARG RCLONE_VERSION=current

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # --- psycopg2 ---
    && pip install --no-cache-dir "psycopg2-binary==2.9.9" \
    # --- rclone ---
    && curl -fsSL "https://downloads.rclone.org/rclone-${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip \
    && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/rclone \
    && rm -f /tmp/rclone.zip \
    # --- supercronic ---
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY sync_db.py sync_storage.sh sync_all.sh crontab /app/
RUN chmod +x /app/sync_storage.sh /app/sync_all.sh

ENV TZ=Asia/Shanghai
RUN mkdir -p /app/logs

# supercronic 读取 /app/crontab 并按表调度；日志同时输出到容器 stdout 与 /app/logs
CMD ["supercronic", "-passthrough-logs", "/app/crontab"]
