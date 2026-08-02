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
  ├─ 备用：Tailscale → https://istoreos.tail11098d.ts.net
  │
  └─ 兜底：Render → https://score-player.onrender.com
```

软路由本地栈由 Docker Compose 管理，包含 `sp-postgres`、`sp-minio`、`sp-app`、`sp-cloudflared` 和 `watchtower`。数据目录为 `/mnt/nas/score-player-data`，app 端口为 `9000`。应用镜像当前优先从阿里云 ACR 拉取：`crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest`，GHCR `ghcr.io/myb357/score-player:latest` 保留为备用镜像源。

关键运行配置包括 `MEDIA_PROXY=1`、`COOKIE_SECURE=0`、`S3_PUBLIC_ENDPOINT=https://media.scoreplayer-myb.top`、`B2_ENDPOINT=http://192.168.1.2:9002`。其中 `MEDIA_PROXY=1` 是软路由本地 MinIO 模式的正确取值：本地 MinIO 生成的预签名 URL 指向内网 `minio:9000`，平板/浏览器无法直连，因此由 app 通过 `/api/media` 回源转发媒体字节流（支持 HTTP Range）；`MEDIA_PROXY=0` 仅用于 Render / Backblaze B2 公网对象存储模式，此时 app 302 跳转到公网可达的预签名 URL，并直接使用 `S3_PUBLIC_ENDPOINT` 创建 S3 client，避免先用内网 `B2_ENDPOINT` 签名再替换域名导致 MinIO host 签名校验失败。

## 方案 A：软路由本地主部署（当前推荐）

当前推荐部署方式是使用 `deploy/softrouter/migrate.sh` 在软路由上一键迁移和启动。脚本会写出运行所需的 `.env`、`docker-compose.yml`、Cloudflare Tunnel 配置，登录阿里云 ACR，并启动完整本地栈。

```bash
cd deploy/softrouter
bash migrate.sh
```

迁移前需确认软路由已安装 Docker 和 docker-compose 或 Docker Compose v2，并且 `/mnt/nas/score-player-data` 已挂载到持久化存储。迁移后使用以下命令检查健康状态：

```bash
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -fsS http://127.0.0.1:9000/api/v1/ping
curl -fsS https://scoreplayer-myb.top/api/v1/ping
```

GitHub Actions 会同时推送镜像到 GHCR 和阿里云 ACR。软路由上的 Watchtower 每 5 分钟自动检查并更新 `sp-app`，因此发布新版本通常只需要推送代码并等待镜像构建完成。

CI 已改为「Webhook 主动部署」：GitHub Actions 在镜像推送完成后，会经 Cloudflare Tunnel 暴露的 `https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>` 向软路由的 `sp-webhook` 服务发起 `POST` 请求（带 `--retry 3` 重试）。`sp-webhook` 校验 token 通过后，在软路由本地执行 `docker pull` 最新阿里云 ACR 镜像并 `docker-compose up -d --no-deps sp-app` 重启应用容器。该链路要求 GitHub 仓库 Secret `WEBHOOK_TOKEN` 与软路由 `.env` 中的 `WEBHOOK_TOKEN` 一致；未配置或 token 不匹配时 CI 步骤会失败，Watchtower 仍作为默认更新机制兜底。已移除原先基于 Tailscale + SSH 的部署步骤及 `SOFTROUTER_SSH_KEY`、`TAILSCALE_AUTH_KEY` 相关配置。

## 方案 B：Render（当前云端兜底）

Render 当前不再作为主生产入口，而是作为软路由和 Tailscale 均不可用时的最终兜底。Render 服务仍可通过仓库根目录的 `render.yaml` 使用 Dockerfile 构建，并以 `/api/v1/ping` 作为健康检查路径。

Render 环境变量仍应通过平台控制台配置，不应写入仓库。核心变量包括 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET`、`B2_REGION`、`SECRET_KEY`、`SCORE_DATA_DIR`、`ADMIN_USERNAME`、`ADMIN_SALT`、`ADMIN_HASH` 等。

Render 连接 Supabase 时必须优先使用 Session Pooler 地址，并带 `sslmode=require`。这是因为 Supabase 直连地址可能只有 IPv6，而 Render 出网只支持 IPv4，使用直连地址可能导致登录或数据库访问返回 500。

## 方案 C：Railway（历史方案）

Railway 是历史部署方案，仓库中的 `railway.json` 保留用于记录和回退参考，但当前不再推荐作为主部署路径。历史 Railway 部署方式为：在 Railway 使用 GitHub 登录，选择 `myb357/score-player` 仓库，通过根目录 `Dockerfile` 构建服务，并在 Variables 中配置 `DATABASE_URL`、B2 凭据、`SECRET_KEY` 和管理员覆盖变量。

如果未来重新启用 Railway，应先确认 APK 端点、Cloudflare Tunnel、媒体域名和 CI/CD 镜像流向是否需要同步调整，避免出现文档、APK 和实际入口不一致。

## APK 端点配置

Android APK 当前使用三级端点：

```js
API_PRIMARY = 'https://scoreplayer-myb.top'
API_FALLBACK = 'https://istoreos.tail11098d.ts.net'
API_FALLBACK2 = 'https://score-player.onrender.com'
```

离线 APK 会优先访问 Cloudflare Tunnel 主入口，失败后切换到 Tailscale，最后才切到 Render。浏览器 Web 访问使用同源相对路径，不依赖该三级端点配置。

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
