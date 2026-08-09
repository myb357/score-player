# score-player 软路由主部署：Cloudflare Tunnel + MinIO 直连媒体

本目录用于在软路由（iStoreOS）上运行 score-player 的本地主生产栈。当前主入口已经从 Tailscale Funnel 切换为 Cloudflare Tunnel：应用域名为 `https://scoreplayer-myb.top`，媒体域名为 `https://media.scoreplayer-myb.top` 并直连 MinIO，避免谱页图片、音频和视频再经过 app 代理。Tailscale `https://istoreos.tail11098d.ts.net` 保留为备用入口，Render `https://score-player.onrender.com` 保留为最终云端兜底。

```
                         ┌──────────────────────── 软路由 iStoreOS（主）────────────────────────┐
   Android APK / Web     │  docker compose:                                                     │
   主入口 ───────────────▶│  sp-cloudflared ──▶ sp-app :9000 ──▶ sp-postgres :5432  [主数据库]   │
   Cloudflare Tunnel     │                         │                                            │
                         │                         └──▶ sp-minio :9002       [主对象存储]       │
   媒体域名 ─────────────▶│  media.scoreplayer-myb.top ─────────▶ sp-minio :9002                 │
   SSH 入口 ─────────────▶│  ssh.scoreplayer-myb.top ───────────▶ Dropbear 192.168.1.2:22        │
   Webhook ─────────────▶│  webhook.scoreplayer-myb.top ───────▶ sp-webhook :9003               │
   Watchtower            │  watchtower 每 5 分钟检查镜像并更新 sp-app                            │
                         └──────────────────────────────────────────────────────────────────────┘

   备用入口 ─────────────▶ https://istoreos.tail11098d.ts.net
   最终兜底 ─────────────▶ https://score-player.onrender.com（Render）
```

## 当前访问架构

| 层级 | 地址 | 说明 |
|---|---|---|
| 主入口 | `https://scoreplayer-myb.top` | Cloudflare Tunnel，转发到 `http://172.17.0.1:9000` |
| 媒体域名 | `https://media.scoreplayer-myb.top` | Cloudflare Tunnel，转发到 `http://172.17.0.1:9002`，指向本地 MinIO S3 端口并绕过 app 代理 |
| Webhook | `https://webhook.scoreplayer-myb.top` | Cloudflare Tunnel，转发到 `http://172.17.0.1:9003`，触发软路由自动部署 |
| 外网 SSH | `ssh.scoreplayer-myb.top` / `ssh score-router` | Cloudflare Tunnel Access SSH，转发到软路由 Dropbear `ssh://192.168.1.2:22` |
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

## 一键迁移（当前最重要入口）

在软路由上直接执行以下命令即可完成迁移与启动：

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/myb357/score-player/main/deploy/softrouter/migrate.sh")
```

`migrate.sh` 已内嵌完整部署所需凭据与配置，包括 DB、MinIO、ACR、Webhook、GitHub Token、Cloudflare Tunnel 等内容。用户无需手动准备 `.env`，也无需先克隆仓库或手动下载 compose 文件，直接运行上述命令即可。若目标 `.env` 已存在，脚本会按固定配置优先策略直接覆盖，以确保一键部署结果可复现。

脚本当前执行流程固定为 9 步：

1. 创建目录结构（`/root/score-player/deploy/softrouter` 等）。
2. 写出 `.env`（含 DB、MinIO、ACR、Webhook、GitHub Token 等全部凭据）。
3. 从 GitHub `main` 分支下载最新 `docker-compose.yml`。
4. 从 GitHub `main` 分支下载最新 `webhook/server.py`。
5. 写出 Cloudflare Tunnel 凭证文件。
6. 写出 Cloudflare `config.yml`。
7. ACR 登录。
8. 执行 `docker-compose up -d`，启动 `db`、`minio`、`score-player`、`sp-webhook`、`cloudflared` 等服务。
9. 验证容器状态。

脚本目标运行环境为 iStoreOS / OpenWrt 系系统，需具备 `docker`、`docker-compose` 或 `docker compose` 插件、`curl`，以及 `openssl` 或 `python3`。脚本要求 root 权限运行，并默认使用 `/mnt/nas/score-player-data` 作为持久化数据目录。重复执行时会覆盖脚本管理的配置文件，并通过 compose 幂等更新容器。

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

`MEDIA_PROXY=1` 是软路由本地 MinIO 部署下的正确取值。本地 MinIO 生成的预签名 URL 指向内网 `minio:9000`，平板和浏览器无法直连该内网地址，因此由 score-player 通过 `/api/media` 回源转发媒体字节流（支持 HTTP Range），而不是让客户端直连对象存储。当前代理按 1MiB 分块从对象存储转发，并返回 `Cache-Control: public, max-age=604800, immutable`，用于降低 Cloudflare Tunnel 下的小块传输开销，并让重复打开同一谱页/伴奏时更容易命中浏览器或 Cloudflare 缓存。`MEDIA_PROXY=0` 仅用于 Render / Backblaze B2 等公网对象存储模式：此时 app 会 302 跳转到公网可达的预签名 URL，并使用 `S3_PUBLIC_ENDPOINT` 作为 S3 client endpoint，使签名中的 host 与外网访问域名一致。`COOKIE_SECURE=0` 与当前软路由本地链路兼容，避免本地或代理路径下 Cookie 写入异常。

## 镜像与 CI/CD

GitHub Actions 会把最新应用镜像同时推送到两个 registry：阿里云 ACR `crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest` 为软路由主镜像源；GHCR `ghcr.io/myb357/score-player:latest` 仅作为阿里云 ACR 不可达时的备用来源。

CI/CD 流程为：推送代码到 `main` 后，GitHub Actions `docker-publish.yml` 使用 JDK 17 先构建 Android APK（Release 失败时 fallback 到 Debug），再把 APK 复制到 `static/android/score-player.apk` 与 `score_app/static/android/score-player.apk`，随后构建并推送 Docker 镜像到阿里云 ACR 与 GHCR。Android Gradle 配置已统一 Java compileOptions 与 Kotlin jvmToolchain 为 JVM 17，当前 APK 版本为 v1.3.17（`versionCode=29`，`versionName=1.3.17`），网页下载入口为 `/download/android`。镜像推送完成后，CI 立即执行 `Trigger soft-router deploy` 步骤，通过 `curl --fail-with-body --show-error --silent --retry 3 --retry-delay 5 --retry-all-errors -X POST "https://webhook.scoreplayer-myb.top/deploy?token=${WEBHOOK_TOKEN}"` 触发软路由 Webhook，并附带一段诊断用 JSON body。Render 云端兜底服务通过 GitHub Auto-Deploy 自动更新，不需要 CI 额外触发。`sp-webhook` 以 query param 中的 `token` 为准并会读取丢弃 body，因此当前请求格式与 `server.py` 期望一致。软路由步骤配置了 `continue-on-error: true`，因此 Webhook 临时不可用或返回失败不会导致整条 CI 失败。软路由上的 Watchtower 仍会以 300 秒间隔作为兜底，持续检查 `sp-app` 的新镜像并自动更新。

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

`sp-webhook` 服务由 `webhook/server.py`（Python 标准库实现，监听 `9003`）提供，`docker-compose.yml` 中以 `python:3.11-alpine` 挂载运行；容器会直接挂载宿主机 `/usr/bin/docker` 与 `/usr/bin/docker-compose` 二进制，并挂载宿主机 `/root/.docker` 以复用 `migrate.sh` 写入的 ACR 登录凭据，保证容器内 `docker pull` 可读取宿主机认证信息。该方案不再在容器启动时通过 `apk` 安装 docker 工具链，避免 iStoreOS 环境下安装慢或外网访问受阻。

启用步骤：

1. **软路由 `.env`**：直接运行 `bash migrate.sh`，脚本会写出已内嵌的 `WEBHOOK_TOKEN`、`GITHUB_TOKEN` 与其他运行时配置，不再需要手动填写 `.env`。如需变更 token 或域名，应修改 `migrate.sh` 中的内嵌配置后重新执行脚本。随后 `docker compose up -d sp-webhook` 启动 Webhook 容器。

2. **GitHub 仓库 Secret**：在 `myb357/score-player` → Settings → Secrets and variables → Actions 中新增 Repository Secret `WEBHOOK_TOKEN`，其值必须与软路由 `.env` 中的 `WEBHOOK_TOKEN` **完全一致**。CI 中的 “Trigger soft-router deploy” 步骤会用它拼接请求。

3. **Cloudflare Tunnel 配置**：在软路由 `/root/.cloudflared/config.yml` 的 `ingress` 中，为 webhook 子域名新增一条转发规则，放在 `scoreplayer-myb.top` 条目之后、`http_status:404` 之前：

   ```yaml
   ingress:
     - hostname: scoreplayer-myb.top
       service: http://172.17.0.1:9000
     - hostname: webhook.scoreplayer-myb.top      # 新增
       service: http://172.17.0.1:9003            # 新增
     - hostname: media.scoreplayer-myb.top
       service: http://172.17.0.1:9002
     - hostname: ssh.scoreplayer-myb.top       # 外网 SSH
       service: ssh://192.168.1.2:22           # Dropbear 仅监听 LAN IP
     - service: http_status:404
   ```

   修改后重启 `sp-cloudflared`（`docker restart sp-cloudflared`）使配置生效。

4. **Cloudflare DNS**：为 `webhook.scoreplayer-myb.top` 添加一条 CNAME，指向本 Tunnel（`<TUNNEL_ID>.cfargotunnel.com`，与 `scoreplayer-myb.top`、`media.scoreplayer-myb.top` 同一目标），并保持“橙云”代理开启。

> 说明：`sp-webhook` 通过 `172.17.0.1:9003` 暴露给宿主机，与 cloudflared（`network_mode: host`）访问 `sp-app` 的 `172.17.0.1:9000` 方式保持一致；由于该端口只绑定在 docker0 网关地址且请求需带正确 token，不会额外暴露到局域网。token 不匹配返回 `401`，未配置 `WEBHOOK_TOKEN` 返回 `500`。

> 注意：`migrate.sh` 不再内嵌 `docker-compose.yml` 与 `webhook/server.py`，而是在运行时通过 GitHub API 下载 `main` 分支上的最新版本；后续调整 compose 或 webhook 服务时，只需保证仓库模板已更新并合入 `main`。Cloudflare `config.yml` 仍由 `migrate.sh` 写出，若调整 Tunnel ingress 仍需同步更新迁移脚本。

## 外网 SSH 访问软路由（Cloudflare Tunnel Access）

当前软路由已通过 Cloudflare Tunnel 暴露 SSH 入口，外网设备可使用 `cloudflared access ssh` 连接。Tunnel 仍是本地配置文件管理模式，Zero Trust 的 Routes 页面显示 `Published application` 只表示本机 `cloudflared` 已上报该 hostname，不等同于 Cloudflare DNS 自动创建了记录；DNS 仍需在 `dash.cloudflare.com` 的 `scoreplayer-myb.top` → DNS → Records 中手动维护一条 `ssh` CNAME。

软路由本机 `/root/.cloudflared/config.yml` 的 `ingress` 规则需在 `http_status:404` 之前包含以下配置：

```yaml
  - hostname: ssh.scoreplayer-myb.top
    service: ssh://192.168.1.2:22
```

这里必须使用 `ssh://192.168.1.2:22`，不要写成 `ssh://localhost:22` 或 `ssh://127.0.0.1:22`。当前 iStoreOS 的 Dropbear 只监听软路由 LAN 地址 `192.168.1.2:22`，`localhost` 会被 `cloudflared` 解析到 IPv6 `[::1]:22` 并导致 `connect: connection refused`；即使 `sp-cloudflared` 使用 host 网络模式，也应显式指向该 LAN IP。修改配置后执行 `docker restart sp-cloudflared` 生效，并用 `docker logs --tail=80 sp-cloudflared` 检查是否仍有 origin connect 错误。

Cloudflare DNS 侧需要手动新增或保留以下记录：

```text
Type: CNAME
Name: ssh
Target: 91086842-3bb6-4fc7-b11f-d65b0824c36a.cfargotunnel.com
Proxy status: Proxied
TTL: Auto
```

外网客户端需要安装 `cloudflared` CLI。macOS 可使用 `brew install cloudflared`，随后可直接执行：

```bash
ssh -l root -o ProxyCommand="cloudflared access ssh --hostname ssh.scoreplayer-myb.top" ssh.scoreplayer-myb.top
```

为简化日常使用，可在外网客户端 `~/.ssh/config` 中添加别名：

```sshconfig
Host score-router
  HostName ssh.scoreplayer-myb.top
  User root
  ProxyCommand cloudflared access ssh --hostname %h
```

之后直接执行 `ssh score-router` 即可登录软路由。若客户端报 `lookup ssh.scoreplayer-myb.top: no such host`，优先检查 Cloudflare DNS Records 中是否真实存在 `ssh` CNAME；若 `sp-cloudflared` 日志出现 `dial tcp [::1]:22: connect: connection refused`，说明 SSH ingress 仍错误指向 `localhost`，需改回 `ssh://192.168.1.2:22`。

## 分支与版本状态

当前生产发布统一走 `main` 分支，旧软路由自动部署分支已删除；部署文档、一键迁移命令、迁移脚本下载源和 GitHub Actions 分支触发均应保持在 `main`。当前 Android APK 版本为 v1.3.22（`versionCode=34`，`versionName=1.3.22`），软路由部署通过 ACR 主镜像源、GHCR 备用镜像源和 Cloudflare Webhook 主动触发完成。每次发布必须确认 Android App APK、软路由生产域名 `https://scoreplayer-myb.top`、Render 兜底服务 `https://score-player.onrender.com` 三端同步上线。

## APK 内网优先端点

Android APK 的原生 Kotlin 入口负责在 WebView 加载前选择访问地址：

```text
内网优先入口：http://192.168.1.2:9000
云端次级入口：https://score-player.onrender.com
软路由外网兜底入口：https://scoreplayer-myb.top
```

冷启动时 APK 会按 `http://192.168.1.2:9000` → `https://score-player.onrender.com` → `https://scoreplayer-myb.top` 的顺序发起轻量 HTTP HEAD 探测，连接和读取超时均为 2 秒；内网可达时直接加载内网地址，内网不可达但 Render 可达时加载 Render + 云端对象存储，Render 也不可达时再加载软路由 Cloudflare Tunnel 外网地址。每次 App 进入前台都会重新探测，以适配网络环境切换。探测和 URL 选择均在原生 Android 侧完成，不依赖 WebView JS；当前选择结果通过 `AndroidBridge.getActiveBaseUrl()`、`AndroidBridge.isInternalNetworkReachable()` 与 `AndroidBridge.isCloudEndpointReachable()` 暴露给页面按需读取。浏览器 Web 访问仍走同源相对路径，不受 APK 端点探测逻辑影响。当前 Android 版本号已对齐为 1.3.5。生产发布链路会在 CI 中先把仓库 `static/` 资源同步到 Android assets，再生成基于 SHA-256 counter stream 的 `apk-size-guard.bin`，随后执行 `assembleDebug` 构建 APK；打入 Docker 镜像前会校验 APK 大小不得小于 1.5MB，且必须包含 `assets/home.html`、`assets/player.html`、`assets/style.css` 和 `assets/apk-size-guard.bin`，避免网页下载到异常偏小的小包。APK 在三类线上入口均不可达时会加载内置 `file:///android_asset/login.html`，离线场景仍先走本地指纹验证，验证成功后再进入本地首页并展示已下载谱子；首页离线且无列表缓存时会从 IndexedDB 读取已下载谱子元数据展示，播放页会继续优先读取本地 blob 资源。原生 WebView 会额外下移页面内容，避免与系统状态栏时间挤在一起；登录页在已保存 token 且设备已设置锁屏/指纹时默认触发指纹登录，并保留密码登录兜底。

## 同步与一致性巡检说明

`sync/` 目录保留“本地 ▶ 云端”的同步方案，包含数据库同步到 Supabase、对象同步到 Backblaze B2 的脚本和容器配置。同步容器依赖两个不入库的线上配置文件：`.env.sync` 和 `rclone.conf`。其中 `.env.sync` 必须显式配置 `LOCAL_DATABASE_URL`、`CLOUD_DATABASE_URL`、`SYNC_MODE`、`SYNC_SESSIONS`、`B2_BUCKET`、`RCLONE_OP`；`rclone.conf` 必须配置 `[minio]` 与 `[b2]` 两个远端。

`check_consistency.sh` 是只读一致性巡检入口，会读取本地 PostgreSQL 核心表行数，并通过 rclone 对比本地 MinIO 与云端 B2 的对象数量和总大小；当 `.env.sync` 中配置了 `CLOUD_DATABASE_URL` 时，还会只读查询云端 Supabase 核心表行数。该脚本不执行任何写入，不会修改本地或云端数据。

当前实际生产主访问已经切到软路由本地栈，Render 仅作为最终兜底；启用 `sp-sync` 前必须确认 Supabase 连接串和 B2 凭据可用，避免同步容器周期性报错。

## 验收要点

1. `https://scoreplayer-myb.top/api/v1/ping` 返回 `pong`。
2. `https://media.scoreplayer-myb.top` 能通过 Cloudflare Tunnel 访问本地 MinIO 暴露的媒体资源。
3. `sp-postgres`、`sp-minio`、`sp-app`、`sp-cloudflared`、`watchtower`、`sp-webhook` 均处于运行状态。
4. APK 冷启动优先探测 `http://192.168.1.2:9000`；内网不可达时探测并优先使用 `https://score-player.onrender.com`，Render 也不可达时才使用 `https://scoreplayer-myb.top`，并且每次回到前台重新探测。
5. GitHub Actions 推送新镜像后，Watchtower 能在约 5 分钟内更新 `sp-app`；Webhook 链路配置完成后，`https://webhook.scoreplayer-myb.top/health` 返回 `{"status":"ok"}`，且 CI 的 “Trigger soft router deploy via Webhook” 步骤成功触发 `sp-app` 即时更新。
