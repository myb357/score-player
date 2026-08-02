# score-player 软路由主部署：Cloudflare Tunnel + MinIO 直连媒体

本目录用于在软路由（iStoreOS）上运行 score-player 的本地主生产栈。当前主入口已经从 Tailscale Funnel 切换为 Cloudflare Tunnel：应用域名为 `https://scoreplayer-myb.top`，媒体域名为 `https://media.scoreplayer-myb.top` 并直连 MinIO，避免谱页图片、音频和视频再经过 app 代理。Tailscale `https://istoreos.tail11098d.ts.net` 保留为备用入口，Render `https://score-player.onrender.com` 保留为最终云端兜底。

```
                         ┌──────────────────────── 软路由 iStoreOS（主）────────────────────────┐
   Android APK / Web     │  docker compose:                                                     │
   主入口 ───────────────▶│  sp-cloudflared ──▶ sp-app :9000 ──▶ sp-postgres :5432  [主数据库]   │
   Cloudflare Tunnel     │                         │                                            │
                         │                         └──▶ sp-minio :9002       [主对象存储]       │
   媒体域名 ─────────────▶│  media.scoreplayer-myb.top ─────────▶ sp-minio :9002                 │
   MinIO 直连            │                                                                      │
   Watchtower            │  watchtower 每 5 分钟检查镜像并更新 sp-app                            │
                         └──────────────────────────────────────────────────────────────────────┘

   备用入口 ─────────────▶ https://istoreos.tail11098d.ts.net
   最终兜底 ─────────────▶ https://score-player.onrender.com（Render）
```

## 当前访问架构

| 层级 | 地址 | 说明 |
|---|---|---|
| 主入口 | `https://scoreplayer-myb.top` | Cloudflare Tunnel，Cloudflare DNS 已配置 |
| 媒体域名 | `https://media.scoreplayer-myb.top` | Cloudflare Tunnel 指向本地 MinIO S3 端口，绕过 app 代理 |
| 备用入口 | `https://istoreos.tail11098d.ts.net` | Tailscale 备用访问路径 |
| 最终兜底 | `https://score-player.onrender.com` | Render 云端兜底服务 |

当前架构以软路由本地 PostgreSQL 和 MinIO 为主库、主对象存储。Render 不再是主生产入口，只用于软路由与备用链路不可用时的最终兜底。

## 目录结构

```
deploy/softrouter/
├── docker-compose.yml            # 软路由栈配置模板
├── .env.softrouter.example       # 软路由环境变量模板
├── migrate.sh                    # 一键迁移脚本：写出配置、Cloudflare 凭证并启动服务
├── webhook/
│   ├── server.py                 # Webhook 自动部署接收器（Python 标准库，监听 9003）
│   └── Dockerfile                # Webhook 服务镜像（可选，compose 默认直接挂载 server.py）
├── scripts/
│   ├── init_minio.sh             # 首次创建 MinIO bucket
│   ├── import_from_supabase.sh   # 首次从 Supabase 导入 schema + 数据到本地
│   └── seed_storage_from_b2.sh   # 首次从 B2 回灌对象到本地 MinIO
└── sync/
    ├── Dockerfile                # 历史同步容器镜像
    ├── docker-compose.sync.yml   # 历史同步容器编排
    ├── sync_db.py                # 数据库同步：本地 ▶ Supabase
    ├── sync_storage.sh           # 对象同步：本地 MinIO ▶ B2
    ├── sync_all.sh               # 同步总入口
    ├── crontab                   # cron 表
    ├── rclone.conf.example       # rclone 两个 S3 远端模板
    └── .env.sync.example         # 同步容器环境变量模板
```

## 一键迁移

当前推荐直接使用 `migrate.sh` 完成软路由迁移。脚本内嵌当前部署所需的 `.env`、`docker-compose.yml`、Cloudflare Tunnel 凭证与配置，并会登录阿里云 ACR、启动完整服务栈。

```bash
cd deploy/softrouter
bash migrate.sh
```

脚本目标运行环境为 iStoreOS / OpenWrt 系系统，需具备 `docker`、`docker-compose` 或 `docker compose` 插件，以及 `openssl` 或 `python3`。脚本要求 root 权限运行，并默认使用 `/mnt/nas/score-player-data` 作为持久化数据目录。重复执行时会覆盖脚本管理的配置文件，并通过 compose 幂等更新容器。

迁移完成后，可用以下命令检查容器与入口健康状态：

```bash
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -fsS http://127.0.0.1:9000/api/v1/ping
curl -fsS https://scoreplayer-myb.top/api/v1/ping
curl -fsS https://istoreos.tail11098d.ts.net/api/v1/ping
```

## docker-compose.yml 说明

当前软路由运行栈包含以下服务，均由 compose 管理：

| 服务 | 容器名 | 镜像/来源 | 说明 |
|---|---|---|---|
| `db` | `sp-postgres` | PostgreSQL alpine 镜像 | 本地主数据库 |
| `minio` | `sp-minio` | MinIO 镜像 | 本地主对象存储，S3 API 端口映射到 `9002` |
| `app` | `sp-app` | 阿里云 ACR `crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest` | score-player 应用，端口 `9000` |
| `cloudflared` | `sp-cloudflared` | Cloudflare cloudflared 镜像 | Cloudflare Tunnel，负责应用域名和媒体域名入口 |
| `watchtower` | `watchtower` | `containrrr/watchtower` | 每 5 分钟拉取新镜像并更新 `sp-app` |
| `sp-webhook` | `sp-webhook` | `python:3.11-alpine` + 挂载 `webhook/server.py` | Webhook 自动部署接收器，监听 `9003`，经 `172.17.0.1:9003` 供 cloudflared 转发 |

当前关键配置如下：

```env
DATA_ROOT=/mnt/nas/score-player-data
APP_PUBLISH=9000
B2_ENDPOINT=http://192.168.1.2:9002
S3_PUBLIC_ENDPOINT=https://media.scoreplayer-myb.top
MEDIA_PROXY=1
COOKIE_SECURE=0
```

`MEDIA_PROXY=1` 是软路由本地 MinIO 部署下的正确取值。本地 MinIO 生成的预签名 URL 指向内网 `minio:9000`，平板和浏览器无法直连该内网地址，因此由 score-player 通过 `/api/media` 回源转发媒体字节流（支持 HTTP Range），而不是让客户端直连对象存储。`MEDIA_PROXY=0` 仅用于 Render / Backblaze B2 等公网对象存储模式：此时 app 会 302 跳转到公网可达的预签名 URL，并使用 `S3_PUBLIC_ENDPOINT` 作为 S3 client endpoint，使签名中的 host 与外网访问域名一致。`COOKIE_SECURE=0` 与当前软路由本地链路兼容，避免本地或代理路径下 Cookie 写入异常。

## 镜像与 CI/CD

GitHub Actions 会把最新应用镜像同时推送到两个 registry：阿里云 ACR `crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest` 为软路由主镜像源；GHCR `ghcr.io/myb357/score-player:latest` 仅作为阿里云 ACR 不可达时的备用来源。

Watchtower 在软路由上以 300 秒间隔运行，持续检查 `sp-app` 的新镜像并自动更新。因此正常发布流程是推送代码到 GitHub，等待 Actions 构建并推送镜像，随后由 Watchtower 自动在软路由拉取并替换应用容器。

## Webhook 自动部署（Cloudflare Tunnel 触发）

除 Watchtower 轮询外，CI 还会在镜像推送完成后经 Cloudflare Tunnel **主动**触发软路由部署，实现秒级发布：

```
GitHub Actions ──POST──▶ https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>
                              │  (Cloudflare Tunnel)
                              ▼
   cloudflared(host) ──http://172.17.0.1:9003──▶ sp-webhook 容器 (server.py)
                              │  token 校验通过
                              ▼
   docker pull <阿里云 ACR 镜像> || docker pull <GHCR 备用镜像>
   docker tag <实际拉到的镜像> <compose 文件 image 标签>
   docker-compose up -d --no-deps score-player
```

`sp-webhook` 服务由 `webhook/server.py`（Python 标准库实现，监听 `9003`）提供，`docker-compose.yml` 中以 `python:3.11-alpine` 挂载运行；容器会直接挂载宿主机 `/usr/bin/docker` 与 `/usr/bin/docker-compose` 二进制，并在启动日志中打印实际探测到的路径。该方案不再在容器启动时通过 `apk` 安装 docker 工具链，避免 iStoreOS 环境下安装慢或外网访问受阻。

启用步骤：

1. **软路由 `.env`**：新增 `WEBHOOK_TOKEN=<32 位随机串>`（例如 `python3 -c "import secrets;print(secrets.token_hex(16))"` 生成），随后 `docker compose up -d sp-webhook` 启动 Webhook 容器。

2. **GitHub 仓库 Secret**：在 `myb357/score-player` → Settings → Secrets and variables → Actions 中新增 Repository Secret `WEBHOOK_TOKEN`，其值必须与软路由 `.env` 中的 `WEBHOOK_TOKEN` **完全一致**。CI 中的 “Trigger soft router deploy via Webhook” 步骤会用它拼接请求。

3. **Cloudflare Tunnel 配置**：在软路由 `/root/.cloudflared/config.yml` 的 `ingress` 中，为 webhook 子域名新增一条转发规则，放在 `scoreplayer-myb.top` 条目之后、`http_status:404` 之前：

   ```yaml
   ingress:
     - hostname: scoreplayer-myb.top
       service: http://172.17.0.1:9000
     - hostname: webhook.scoreplayer-myb.top      # 新增
       service: http://172.17.0.1:9003            # 新增
     - hostname: media.scoreplayer-myb.top
       service: http://172.17.0.1:9002
     - service: http_status:404
   ```

   修改后重启 `sp-cloudflared`（`docker restart sp-cloudflared`）使配置生效。

4. **Cloudflare DNS**：为 `webhook.scoreplayer-myb.top` 添加一条 CNAME，指向本 Tunnel（`<TUNNEL_ID>.cfargotunnel.com`，与 `scoreplayer-myb.top`、`media.scoreplayer-myb.top` 同一目标），并保持“橙云”代理开启。

> 说明：`sp-webhook` 通过 `172.17.0.1:9003` 暴露给宿主机，与 cloudflared（`network_mode: host`）访问 `sp-app` 的 `172.17.0.1:9000` 方式保持一致；由于该端口只绑定在 docker0 网关地址且请求需带正确 token，不会额外暴露到局域网。token 不匹配返回 `401`，未配置 `WEBHOOK_TOKEN` 返回 `500`。

> 注意：`migrate.sh` 内嵌了独立的 `docker-compose.yml` 与 Cloudflare `config.yml`。若通过重新执行 `migrate.sh` 来重建软路由栈，需要同步在脚本内嵌的 compose 中加入 `sp-webhook` 服务、在内嵌 `config.yml` 中加入上面的 webhook ingress 条目，否则 Webhook 服务不会随 `migrate.sh` 一起部署。

## APK 三级端点

Android APK 的离线 WebView 资源使用三级端点故障切换：

```js
API_PRIMARY = 'https://scoreplayer-myb.top'
API_FALLBACK = 'https://istoreos.tail11098d.ts.net'
API_FALLBACK2 = 'https://score-player.onrender.com'
```

冷启动时 APK 优先探测 Cloudflare Tunnel 主入口，失败后切换到 Tailscale 备用入口，再失败才切换到 Render。浏览器 Web 访问仍走同源相对路径，不受 APK 端点探测逻辑影响。

## 历史同步任务说明

`sync/` 目录保留了早期“本地 ▶ 云端”的同步方案，包含数据库同步到 Supabase、对象同步到 Backblaze B2 的脚本和容器配置。当前实际生产主访问已经切到软路由本地栈，Render 仅作为最终兜底；如需重新启用云端备份同步，应先确认当前数据流向和覆盖策略，再启动同步容器。

## 验收要点

1. `https://scoreplayer-myb.top/api/v1/ping` 返回 `pong`。
2. `https://media.scoreplayer-myb.top` 能通过 Cloudflare Tunnel 访问本地 MinIO 暴露的媒体资源。
3. `sp-postgres`、`sp-minio`、`sp-app`、`sp-cloudflared`、`watchtower`、`sp-webhook` 均处于运行状态。
4. APK 冷启动优先使用 Cloudflare Tunnel，主入口不可用时依次切换到 Tailscale 与 Render。
5. GitHub Actions 推送新镜像后，Watchtower 能在约 5 分钟内更新 `sp-app`；Webhook 链路配置完成后，`https://webhook.scoreplayer-myb.top/health` 返回 `{"status":"ok"}`，且 CI 的 “Trigger soft router deploy via Webhook” 步骤成功触发 `sp-app` 即时更新。
