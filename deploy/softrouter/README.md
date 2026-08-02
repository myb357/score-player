# score-player 双活架构：软路由本地主库 + Render 云端备库

本目录提供在 **软路由（iStoreOS）** 上把 score-player 落地为「本地主库」，并把数据单向同步到
**Render 云端备库** 的全部基础设施与脚本。App 端（Android APK）会优先访问软路由，失败时自动切换到 Render。

```
                         ┌──────────────────────── 软路由 iStoreOS（主）────────────────────────┐
   Android APK           │  docker compose:                                                     │
   优先 ─────▶ Tailscale ─┼─▶ score-player :9000 ──▶ PostgreSQL(db) :5432   [主数据库]           │
   Funnel               │       │            └──▶ MinIO :9000            [主对象存储/外挂磁盘]  │
   失败↓兜底             │       │                                                               │
                         │   sync 容器(supercronic cron) ── 单向 ──▶                            │
                         └───────────────────────────────────────────────┼──────────────────────┘
                                                                          │  本地 ▶ 云端
   Android APK                                    ┌──────────── Render 云端（备）─────────────▼──┐
   兜底 ─────────────────────────────────────────▶│ score-player.onrender.com                     │
                                                  │   ├─▶ Supabase PostgreSQL   [备数据库]        │
                                                  │   └─▶ Backblaze B2          [备对象存储]      │
                                                  └───────────────────────────────────────────────┘
```

- **同步方向**：严格单向，本地 ▶ 云端。本地是主库，同步任务只读本地、只写云端。
- **失败隔离**：同步失败只记录日志、不影响本地主库；云端写入具备事务性，失败自动回滚。
- **App 故障切换**：仅在离线 APK（`file://`）生效，Web 端不受影响。

---

## 目录结构

```
deploy/softrouter/
├── docker-compose.yml            # 主库栈：PostgreSQL + MinIO + score-player
├── .env.softrouter.example       # 主库栈环境变量模板
├── scripts/
│   ├── init_minio.sh             # 首次创建 MinIO bucket
│   ├── import_from_supabase.sh   # 首次：从 Supabase 导入 schema + 数据到本地
│   └── seed_storage_from_b2.sh   # 首次：从 B2 回灌对象到本地 MinIO
└── sync/
    ├── Dockerfile                # 同步容器镜像(python+psycopg2+rclone+supercronic)
    ├── docker-compose.sync.yml   # 同步容器编排
    ├── sync_db.py                # 数据库同步：本地 ▶ Supabase（full/upsert，事务）
    ├── sync_storage.sh           # 对象同步：本地 MinIO ▶ B2（rclone）
    ├── sync_all.sh               # 总入口：DB+存储，失败仅记日志、始终退 0
    ├── crontab                   # cron 表（默认每小时第 15 分钟）
    ├── rclone.conf.example       # rclone 两个 S3 远端模板
    └── .env.sync.example         # 同步容器环境变量模板
```

---

## 第一部分：软路由本地基础设施

> 前提：iStoreOS 已装 Docker 20.10.22，且 `docker compose`（compose v2）可用。
> 若只有 `docker-compose`（v1）请把命令中的 `docker compose` 换成 `docker-compose`。

### 步骤 0：准备外挂磁盘目录（存储空间有限，务必用外挂磁盘，禁用内存/tmpfs）

```bash
# 假设外接磁盘挂载在 /mnt/sda1，创建数据根目录
mkdir -p /mnt/sda1/score-player-data
```

### 步骤 1：下发配置并启动主库栈

```bash
# 把本目录拷贝到软路由，例如 /root/score-player/deploy/softrouter
cd /root/score-player/deploy/softrouter

cp .env.softrouter.example .env
vi .env      # 修改 DATA_ROOT、PG_PASSWORD、MINIO_ROOT_PASSWORD 等

docker compose up -d
docker compose ps
```

`docker-compose.yml` 已包含三个服务，均使用**外挂目录 bind mount** 持久化：

| 服务 | 镜像 | 持久化 | 说明 |
|---|---|---|---|
| `db` | `postgres:16-alpine` | `${DATA_ROOT}/postgres` | 主数据库 |
| `minio` | `minio/minio:latest` | `${DATA_ROOT}/minio` | 主对象存储（S3 兼容） |
| `score-player` | `ghcr.io/myb357/score-player:latest` | 无状态 | 连本地 db + minio，发布 9000 |

**score-player 连接本地库的关键环境变量**（compose 已写好，无需手改）：

```
DATABASE_URL=postgresql://score:<pwd>@db:5432/scoredb?sslmode=disable   # 本地无 SSL，必须 disable
B2_ENDPOINT=http://minio:9000     # 指向本地 MinIO
B2_KEY_ID / B2_APP_KEY            # = MinIO 的 root user / password
B2_BUCKET=score-player
B2_REGION=us-east-1               # 与 MinIO region 一致（B2 的自动推导正则对 MinIO 不生效，须显式给）
MEDIA_PROXY=1                     # 关键：本地 MinIO 预签名 URL 内网不可达，改由 app 回源转发
```

### 步骤 2：创建 MinIO bucket

```bash
bash scripts/init_minio.sh
```

### 步骤 3：从云端导入现有数据（首次迁移，可选）

score-player 首次连本地库时 `init_db()` 会自动建表（`CREATE TABLE IF NOT EXISTS`）——即 schema
可自动生成。若要连同**历史数据**一起从 Supabase 搬到本地，执行：

```bash
# 导出 Supabase 连接串（含 sslmode=require），再运行导入
export SUPABASE_DATABASE_URL='postgresql://postgres.<ref>:<pwd>@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require'
bash scripts/import_from_supabase.sh          # pg_dump(Supabase) | psql(本地)，含 5 张业务表

# 把云端 B2 的对象文件回灌到本地 MinIO（需 .env 里配好 B2 只读凭据）
bash scripts/seed_storage_from_b2.sh
```

### 步骤 4：验证

```bash
curl -fsS http://127.0.0.1:9000/api/v1/ping        # 期望 pong
curl -fsS https://istoreos.tail11098d.ts.net/api/v1/ping   # 经 Tailscale Funnel 公网验证
docker compose logs -f score-player
```

### 附：等价的原始 `docker run` 命令（不使用 compose 时）

```bash
docker network create spnet

# PostgreSQL（外挂 volume 持久化）
docker run -d --name sp-postgres --restart unless-stopped --network spnet \
  -e POSTGRES_USER=score -e POSTGRES_PASSWORD=change-me-pg -e POSTGRES_DB=scoredb \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v /mnt/sda1/score-player-data/postgres:/var/lib/postgresql/data \
  -p 127.0.0.1:5432:5432 postgres:16-alpine

# MinIO（外挂 volume 持久化，S3 兼容）
docker run -d --name sp-minio --restart unless-stopped --network spnet \
  -e MINIO_ROOT_USER=scoreadmin -e MINIO_ROOT_PASSWORD=change-me-minio \
  -e MINIO_REGION=us-east-1 \
  -v /mnt/sda1/score-player-data/minio:/data \
  -p 127.0.0.1:9002:9000 -p 127.0.0.1:9001:9001 \
  minio/minio:latest server /data --console-address ":9001"

# score-player 重建（连本地 PostgreSQL + MinIO）
docker rm -f score-player 2>/dev/null || true
docker run -d --name sp-app --restart unless-stopped --network spnet \
  -e SCORE_DATA_DIR=/tmp/score_app_data \
  -e DATABASE_URL='postgresql://score:change-me-pg@sp-postgres:5432/scoredb?sslmode=disable' \
  -e B2_ENDPOINT=http://sp-minio:9000 \
  -e B2_KEY_ID=scoreadmin -e B2_APP_KEY=change-me-minio \
  -e B2_BUCKET=score-player -e B2_REGION=us-east-1 \
  -e MEDIA_PROXY=1 -e PORT=8000 \
  -p 9000:8000 ghcr.io/myb357/score-player:latest
```
> 注意：原始命令用容器名 `sp-postgres`/`sp-minio` 作为主机名互联；compose 方式用服务名 `db`/`minio`。

---

## 第二部分：同步任务（本地 ▶ 云端）

同步容器与主库栈同网段运行，用 supercronic 按 cron 定时执行两件事：
1. **数据库**：`sync_db.py` 把本地 PostgreSQL 同步到 Supabase。
2. **对象存储**：`sync_storage.sh` 用 rclone 把本地 MinIO 同步到 B2。

### 部署同步容器

```bash
cd /root/score-player/deploy/softrouter/sync

cp .env.sync.example .env.sync
vi .env.sync        # 填 LOCAL_DATABASE_URL / CLOUD_DATABASE_URL(Supabase) / SYNC_MODE

cp rclone.conf.example rclone.conf
vi rclone.conf      # 填 minio(源) 与 b2(目的) 的 key/secret

# 确认主库栈网络名（compose 默认 <目录名>_spnet，即 softrouter_spnet）
docker network ls | grep spnet
# 若不是 softrouter_spnet，在 docker-compose.sync.yml 里改 MAIN_STACK_NETWORK 或用 env 覆盖

docker compose -f docker-compose.sync.yml up -d --build
docker compose -f docker-compose.sync.yml logs -f      # 观察调度与同步日志
```

### 同步策略说明

| 项 | 说明 |
|---|---|
| **DB 模式** | `SYNC_MODE=full`（默认）：镜像式全量刷新，单事务 TRUNCATE+重灌，云端==本地；`upsert`：按主键增量只增改 |
| **DB 只读保护** | 本地连接以 `readonly=True` 打开，硬性保证主库不被写 |
| **DB 事务性** | 云端写入失败自动 `rollback`，云端保持上一次完整快照，不会半损坏 |
| **对象存储** | `RCLONE_OP=sync`（默认，镜像，会删云端多余对象）或 `copy`（只增改不删） |
| **失败隔离** | `sync_all.sh` 不用 `set -e`，任何一步失败仅写日志，脚本始终退 0，不影响主库、不阻塞下次调度 |
| **频率** | `crontab` 默认 `15 * * * *`（每小时第 15 分钟），可自行调整 |
| **日志** | 追加到 `sync/logs/sync.log`，超 5MB 自动轮转，保留最近 5 份 |
| **sessions 表** | 默认不同步（登录会话上云无意义），`SYNC_SESSIONS=1` 可开启 |

### 手动跑一次 / 排错

```bash
docker compose -f docker-compose.sync.yml exec sync /app/sync_all.sh   # 立即执行一次
docker compose -f docker-compose.sync.yml exec sync python3 /app/sync_db.py   # 只跑 DB
docker compose -f docker-compose.sync.yml exec sync tail -n 100 /app/logs/sync.log
```

### 替代方案：软路由宿主机 crontab（不额外跑容器）

若不想常驻同步容器，可用宿主机 cron 直接调用同步脚本（需宿主机有 python3+psycopg2+rclone）：

```bash
crontab -e
# 每小时第 15 分钟执行；LOG 追加，失败不影响系统
15 * * * * cd /root/score-player/deploy/softrouter/sync && \
  LOCAL_DATABASE_URL='...' CLOUD_DATABASE_URL='...' RCLONE_CONFIG=./rclone.conf \
  LOG_DIR=./logs /bin/bash ./sync_all.sh
```

---

## 第三部分：score-player 后端配置

后端 `main.py` 的数据库与对象存储连接**已全部走环境变量，无任何硬编码**：

- 数据库：`DATABASE_URL`（`get_pool()` / `_dsn()`）。
- 对象存储：`B2_ENDPOINT / B2_KEY_ID / B2_APP_KEY / B2_BUCKET / B2_REGION`（`get_s3()`）。
  boto3 已设 `addressing_style=path` + `signature_version=s3v4`，**天然兼容 MinIO**。

本次仅新增一个**向后兼容**的环境开关（默认关闭，云端行为不变）：

- **`MEDIA_PROXY`**（`main.py` 的 `serve_media`）：
  - `0`（默认，Render 云端）：`/api/media` 302 跳转到公网可达的预签名 URL（Backblaze B2）。
  - `1`（软路由本地）：本地 MinIO 的预签名 URL 会指向内网 `http://minio:9000`，平板无法访问；
    因此改由 score-player **直接回源转发字节流**（实现了 HTTP `Range`，音频可正常拖动/续播）。

因此：**云端 Render 无需任何改动**；**软路由端只需设 `MEDIA_PROXY=1`**（compose 已内置）。

---

## 第四部分：Android APK 故障切换

APK 是 WebView 壳，端点逻辑在打包进 APK 的静态资源 `static/*.html` 里。改动集中在**每个页面的
网络引导段**（`static/home.html / login.html / new.html / player.html / downloads.html / users.html`）：

**改前**（写死 Render）：
```js
var API_BASE = IS_FILE ? 'https://score-player.onrender.com' : '';
```

**改后**（软路由优先 + Render 兜底，仅 `file://` 离线 APK 生效；Web 同源仍为 `''`）：
- 新增常量 `API_PRIMARY = 'https://istoreos.tail11098d.ts.net'`、`API_FALLBACK = 'https://score-player.onrender.com'`。
- 启动时对 `API_PRIMARY/api/v1/ping` 做 **4 秒超时探测**（`AbortController`）：通则用软路由，
  **超时/失败则切到 Render**，并弹一条**轻提示 Toast**「软路由不可用，已切换到云端服务」。
- 选择结果缓存在 `sessionStorage`（TTL 60s），跨页面复用，避免每次导航重探。
- `apiFetch` 的出口从 `fetch(url,opts)` 改为经统一封装 `window.__epFetch(url,opts)`：
  - 内部先 `await` 端点就绪，再按当前 base 发请求；
  - 若请求**网络层硬失败**（如软路由中途掉线），自动切到另一端点并重试一次，同样弹 Toast。
  - **不对普通请求施加短超时**，避免中断上传/转码等长耗时请求（只在探测阶段用超时）。

> 约束落实：故障切换只加在网络层（端点选择 + `__epFetch`），不改任何业务逻辑；Web 模式
> （`IS_FILE=false`）完全走原路径 `API_BASE=''`，现有功能零影响。

### 让改动进入 APK

这些 HTML 是 APK 的打包资源，需在**下次 Android 构建**时同步进 assets 再出包
（沿用既有「assets 同步 → 构建前验证 APK 内容 → 构建 → 复制 APK → git push」流程）。
构建产物落到 `static/android/score-player.apk` 与 `score_app/static/android/score-player.apk`。

### 验收要点

1. 软路由在线：APK 冷启动 → 全程走 `https://istoreos.tail11098d.ts.net`，无 Toast。
2. 软路由离线（关掉软路由或断 Funnel）：APK 冷启动 → 4 秒内自动切 Render，弹一次切换 Toast，功能正常。
3. 使用中软路由掉线：下一次接口请求触发失败重试并切 Render，弹 Toast，操作不中断。
4. 浏览器访问 `score-player.onrender.com`：行为与改动前完全一致（同源相对路径）。

---

## 约束对照检查表

| 用户约束 | 落实方式 |
|---|---|
| 软路由存储有限，MinIO 用外挂 volume，禁用内存 | compose 与 docker run 均 bind mount 到 `${DATA_ROOT}`（外挂磁盘），无 tmpfs |
| 同步失败不影响主库，只记录日志 | `sync_all.sh` 捕获错误、始终退 0；`sync_db.py` 本地只读 + 云端事务回滚 |
| APK 切换不影响现有功能，只在网络层加判断 | 仅改端点引导段与 `apiFetch` 出口；Web 模式路径不变；不动业务逻辑 |
| 同步方向单向 本地 ▶ 云端 | `sync_db.py` 只读本地写云端；`rclone` 源 minio → 目的 b2 |
