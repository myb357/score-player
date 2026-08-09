# 部署指南：软路由为主，Render 为云端兜底

score-player 当前生产架构已经从单一云平台部署调整为“软路由本地主栈 + Render 最终兜底”。主访问入口为 Cloudflare Tunnel 暴露的 `https://scoreplayer-myb.top`，媒体域名 `https://media.scoreplayer-myb.top` 直连软路由本地 MinIO，备用入口为 Tailscale `https://istoreos.tail11098d.ts.net`，Render `https://score-player.onrender.com` 仅作为云端兜底。

## 当前整体架构

```
用户 / APK
  │
  ├─ 主入口：Cloudflare Tunnel → https://scoreplayer-myb.top → 软路由 sp-app:9000
  │                                      │
  │                                      └─ PostgreSQL + MinIO 本地主库
  │
  ├─ 媒体：Cloudflare Tunnel → https://media.scoreplayer-myb.top → MinIO:9002
  │
  ├─ 外网 SSH：Cloudflare Tunnel Access → ssh.scoreplayer-myb.top → Dropbear:192.168.1.2:22
  │
  ├─ 备用：Tailscale → https://istoreos.tail11098d.ts.net
  │
  └─ 兜底：Render → https://score-player.onrender.com
```

软路由本地栈由 Docker Compose 管理，部署目录为 `/root/score-player/deploy/softrouter`，包含 `sp-postgres`、`sp-minio`、`sp-app`、`sp-webhook`、`sp-cloudflared` 和 `watchtower`。数据目录为 `/mnt/nas/score-player-data`，app 端口为 `9000`，MinIO S3 端口为 `9002`，Webhook 端口为 `9003`。外网 SSH 入口已通过同一个 locally managed Cloudflare Tunnel 暴露为 `ssh.scoreplayer-myb.top`，本机 ingress 指向 `ssh://192.168.1.2:22`，并依赖 Cloudflare DNS 中手动维护的 `ssh` CNAME 记录指向 `91086842-3bb6-4fc7-b11f-d65b0824c36a.cfargotunnel.com`。应用镜像当前优先从阿里云 ACR 拉取：`crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest`；GHCR `ghcr.io/myb357/score-player:latest` 仅作为阿里云 ACR 不可达时的备用镜像源。

关键运行配置包括 `MEDIA_PROXY=1`、`COOKIE_SECURE=0`、`S3_PUBLIC_ENDPOINT=https://media.scoreplayer-myb.top`、`B2_ENDPOINT=http://192.168.1.2:9002`。其中 `MEDIA_PROXY=1` 是软路由本地 MinIO 模式的正确取值：本地 MinIO 生成的预签名 URL 指向内网 `minio:9000`，平板/浏览器无法直连，因此由 app 通过 `/api/media` 回源转发媒体字节流（支持 HTTP Range）；`MEDIA_PROXY=0` 仅用于 Render / Backblaze B2 公网对象存储模式，此时 app 302 跳转到公网可达的预签名 URL，并直接使用 `S3_PUBLIC_ENDPOINT` 创建 S3 client，避免先用内网 `B2_ENDPOINT` 签名再替换域名导致 MinIO host 签名校验失败。

## 方案 A：软路由本地主部署（当前推荐）

当前推荐部署方式是在软路由上直接执行一键迁移命令。脚本已内嵌完整部署所需凭据与配置，包括 DB、MinIO、ACR、Webhook、GitHub Token、Cloudflare Tunnel 等内容；用户无需手动准备 `.env`，也无需先克隆仓库或手动下载 compose 文件，直接运行即可。

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/myb357/score-player/main/deploy/softrouter/migrate.sh")
```

`migrate.sh` 当前执行流程固定为 9 步：步骤 1/9 创建目录结构（`/root/score-player/deploy/softrouter` 等）；步骤 2/9 写出 `.env`（含 DB、MinIO、ACR、Webhook、GitHub Token 等全部凭据）；步骤 3/9 从 GitHub `main` 分支下载最新 `docker-compose.yml`；步骤 4/9 从 GitHub `main` 分支下载最新 `webhook/server.py`；步骤 5/9 写出 Cloudflare Tunnel 凭证文件；步骤 6/9 写出 Cloudflare `config.yml`；步骤 7/9 ACR 登录；步骤 8/9 执行 `docker-compose up -d`，启动 `db`、`minio`、`score-player`、`sp-webhook`、`cloudflared` 等服务；步骤 9/9 验证容器状态。

迁移前需确认软路由已安装 Docker 和 docker-compose 或 Docker Compose v2，并且 `/mnt/nas/score-player-data` 已挂载到持久化存储。迁移后使用以下命令检查健康状态：

```bash
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -fsS http://127.0.0.1:9000/api/v1/ping
curl -fsS https://scoreplayer-myb.top/api/v1/ping
```

### 软路由自动更新与 APK 构建
软路由上的 Watchtower 每 5 分钟自动检查并更新 `sp-app`。GitHub Actions `docker-publish.yml` 已配置为在 Docker 镜像构建前**自动执行 Android APK 构建**，使用 JDK 17，并将产物打包进镜像；Android Gradle 配置已统一 Java compileOptions 与 Kotlin jvmToolchain 为 JVM 17，避免 CI 中 Java/Kotlin target 不一致。Android APK 版本号已统一对齐为 v1.3.20（`versionCode=32`，`versionName=1.3.20`），网页下载入口为 `/download/android`。CI 构建前会把 `static/` 中的 WebView 页面资源同步到 `android/app/src/main/assets/`，并生成基于 SHA-256 counter stream 的 `apk-size-guard.bin`，避免门禁文件被 ZIP 高度压缩后仍产出异常小包。发布新版本必须确认三端同步上线：Android App APK、iStoreOS 软路由生产域名 `https://scoreplayer-myb.top`、Render 兜底服务 `https://score-player.onrender.com`。推送 `main` 后需检查 GitHub Actions 成功、软路由 Webhook 部署成功、`/api/version` 与 `/download/android` 更新，同时确认 Render Auto-Deploy 已完成。Docker 发布 workflow 当前仅监听 `main` 分支推送，并支持 `workflow_dispatch` 手动触发，便于需要时随时手动构建和部署。

CI 采用「Webhook 主动部署」：GitHub Actions 在镜像推送完成后，会经 Cloudflare Tunnel 暴露的 `https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>` 向软路由的 `sp-webhook` 服务发起 `POST` 请求（带 `--fail-with-body` 与 `--retry 3 --retry-delay 5 --retry-all-errors` 重试，并附带诊断用 JSON body）。Render 云端兜底服务通过 GitHub Auto-Deploy 自动更新，不需要 CI 额外触发。`sp-webhook` 校验 query param 中的 token 通过后，在软路由本地优先执行 `docker pull` 最新阿里云 ACR 镜像；若阿里云 ACR 拉取失败，则降级拉取 GHCR 镜像，并将实际拉到的镜像 tag 为 compose 文件中的阿里云 ACR 镜像标签，再执行 `docker-compose up -d --no-deps score-player` 重启应用容器。容器内的 `docker` / `docker-compose` 直接从宿主机 `/usr/bin` 挂载，不再通过 `apk` 动态安装。该链路要求 GitHub 仓库 Secret `WEBHOOK_TOKEN` 与软路由 `.env` 中的 `WEBHOOK_TOKEN` 一致。未配置或 token 不匹配时软路由部署步骤会失败，Watchtower 仍作为软路由默认更新机制兜底。已移除原先基于 Tailscale + SSH 的部署步骤及 `SOFTROUTER_SSH_KEY`、`TAILSCALE_AUTH_KEY` 相关配置。

软路由同步方案位于 `deploy/softrouter/sync/`，通过 `sp-sync` 容器按 `crontab` 定时执行本地 PostgreSQL / MinIO 到云端 Supabase / Backblaze B2 的单向同步。`check_consistency.sh` 用于只读一致性巡检：检查本地 PostgreSQL 核心表行数，并对比本地 MinIO 与云端 B2 的对象数量和总大小；当 `.env.sync` 配置了 `CLOUD_DATABASE_URL` 时，还会只读查询云端 Supabase 核心表行数。

当前生产发布统一走 `main` 分支，旧软路由自动部署分支已删除；部署文档、一键迁移命令、迁移脚本下载源和 GitHub Actions 分支触发均应保持在 `main`。

## Cloudflare Tunnel 外网 SSH

当前软路由可通过 Cloudflare Tunnel Access 从外网 SSH 登录。由于 Tunnel 是本地配置文件管理模式，需在软路由 `/root/.cloudflared/config.yml` 的 `ingress` 中、`http_status:404` 之前保留以下规则：

```yaml
- hostname: ssh.scoreplayer-myb.top
  service: ssh://192.168.1.2:22
```

这里必须指向 `192.168.1.2:22`，因为 Dropbear 当前只监听软路由 LAN 地址；写成 `localhost` 会被解析到 IPv6 `[::1]:22` 并导致连接拒绝。Cloudflare Dashboard 的 Zero Trust Routes 页面显示 `Published application` 不代表 DNS 自动创建完成，还必须在 `scoreplayer-myb.top` 的 DNS Records 中保留 `ssh` CNAME，目标为 `91086842-3bb6-4fc7-b11f-d65b0824c36a.cfargotunnel.com`，并开启代理。

外网客户端安装 `cloudflared` CLI 后，可在 `~/.ssh/config` 添加：

```sshconfig
Host score-router
  HostName ssh.scoreplayer-myb.top
  User root
  ProxyCommand cloudflared access ssh --hostname %h
```

之后执行 `ssh score-router` 即可登录软路由。若出现 `no such host`，优先检查 DNS Records；若 `sp-cloudflared` 日志出现 `[::1]:22 connect: connection refused`，优先检查 SSH ingress 是否误写为 `localhost`。

## 方案 B：Render（当前云端兜底）

Render 当前不再作为主生产入口，而是作为软路由和 Tailscale 均不可用时的最终兜底。Render 服务仍可通过仓库根目录的 `render.yaml` 使用 Dockerfile 构建，并以 `/api/v1/ping` 作为健康检查路径。

Render 环境变量仍应通过平台控制台配置，不应写入仓库。核心变量包括 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET`、`B2_REGION`、`SECRET_KEY`、`SCORE_DATA_DIR`、`ADMIN_USERNAME`、`ADMIN_SALT`、`ADMIN_HASH` 等。

Render 连接 Supabase 时必须优先使用 Session Pooler 地址，并带 `sslmode=require`。这是因为 Supabase 直连地址可能只有 IPv6，而 Render 出网只支持 IPv4，使用直连地址可能导致登录或数据库访问返回 500。

## 方案 C：Railway（历史方案）

Railway 是历史部署方案，仓库中的 `railway.json` 保留用于记录和回退参考，但当前不再推荐作为主部署路径。历史 Railway 部署方式为：在 Railway 使用 GitHub 登录，选择 `myb357/score-player` 仓库，通过根目录 `Dockerfile` 构建服务，并在 Variables 中配置 `DATABASE_URL`、B2 凭据、`SECRET_KEY` 和管理员覆盖变量。

如果未来重新启用 Railway，应先确认 APK 端点、Cloudflare Tunnel、媒体域名和 CI/CD 镜像流向是否需要同步调整，避免出现文档、APK 和实际入口不一致。

## APK 端点配置

Android APK 当前由原生 Kotlin 入口控制 WebView 加载地址，采用三级优先策略：

```text
内网优先入口：http://192.168.1.2:9000
云端次级入口：https://score-player.onrender.com
软路由外网兜底入口：https://scoreplayer-myb.top
```

App 启动或 WebView 首次加载前，会在原生侧按 `http://192.168.1.2:9000` → `https://score-player.onrender.com` → `https://scoreplayer-myb.top` 的顺序发起 HTTP HEAD 探测，连接和读取超时均为 2 秒。内网探测成功时 WebView 加载内网地址；内网失败但 Render 可达时加载 Render + 云端对象存储；Render 也失败时才加载软路由 Cloudflare Tunnel 外网地址。每次 App 进入前台都会重新探测，以便在家庭 Wi-Fi 与外出网络之间自动切换。该逻辑位于 Android Kotlin 侧，不依赖 WebView JS 跨域请求；当前激活地址、内网可达状态和 Render 可达状态也通过 `AndroidBridge.getActiveBaseUrl()`、`AndroidBridge.isInternalNetworkReachable()`、`AndroidBridge.isCloudEndpointReachable()` 提供给页面按需读取。浏览器 Web 访问使用同源相对路径，不依赖该 APK 端点选择逻辑。

## 修改登录密码

用下面命令生成新的 salt/hash，再在对应部署环境变量里设置 `ADMIN_SALT` 与 `ADMIN_HASH`：

```python
import secrets, hashlib
pw = "你的新密码"
salt = secrets.token_hex(16)
h = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200000).hex()
print("ADMIN_SALT=", salt); print("ADMIN_HASH=", h)
```

## 故障排查：Render 登录报错 500 / 数据库连不上

如果 `https://score-player.onrender.com/api/v1/ping` 返回正常，但 `/api/login` 返回 500，优先检查 Render 的 `DATABASE_URL` 是否仍在使用 Supabase 直连地址 `db.<ref>.supabase.co`。该直连地址可能只有 IPv6，而 Render 出网只支持 IPv4。

修复方式是在 Supabase 控制台进入 Project Settings → Database → Connection string，选择 Session pooler，复制形如 `postgresql://postgres.<项目ref>:<密码>@aws-0-<区域>.pooler.supabase.com:5432/postgres` 的 URI，并在 Render Environment 中替换 `DATABASE_URL`。保存后触发 Render 重建，再重新验证登录。

数据库中的用户和密码哈希无需重建；该问题属于 Render 到 Supabase 的网络可达性配置问题。
