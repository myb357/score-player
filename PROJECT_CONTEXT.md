# score-player 项目开发上下文

> 文档目的：这份文档用于在未来交给 AI 或新开发者时，快速恢复 score-player 项目的完整上下文，直接开展增量开发。本文基于当前仓库根目录整理，重点覆盖项目定位、部署方式、代码结构、数据库结构、已实现功能、关键设计决策和后续开发注意事项。

## 1. 项目概述

score-player 是一个“谱子 + 伴奏播放器”网站。用户可以上传多张谱页图片，并可选择上传伴奏音频或视频，系统将其转换为可播放的谱子项目；播放时支持跟随伴奏时间轴自动翻页，也支持不依赖音频的独立倒计时翻页。

项目技术栈为 Flask/Python 后端、原生 HTML/CSS/JS 前端。当前代码实际使用 FastAPI 实现 Python Web 服务，前端仍是无构建步骤的原生 HTML、CSS、JavaScript 文件。整体应用部署为 Docker Web Service，后端负责鉴权、数据库访问、对象存储读写、音视频处理和 API 输出，前端负责页面渲染、上传交互、播放器控制、缓存和管理界面。

## 2. 部署信息

当前生产访问以软路由本地栈为主，主入口为 Cloudflare Tunnel 暴露的 `https://scoreplayer-myb.top`，转发到 `http://172.17.0.1:9000`。媒体资源使用独立域名 `https://media.scoreplayer-myb.top`，转发到 `http://172.17.0.1:9002`，直连本地 MinIO 并绕过 app 代理。Webhook 自动部署入口为 `https://webhook.scoreplayer-myb.top`，转发到 `http://172.17.0.1:9003`。外网 SSH 入口为 `ssh.scoreplayer-myb.top`，通过 Cloudflare Tunnel Access 转发到软路由 Dropbear `ssh://192.168.1.2:22`。备用入口为 Tailscale `https://istoreos.tail11098d.ts.net`，最终兜底仍保留 Render `https://score-player.onrender.com`。

软路由本地栈运行在 iStoreOS Docker Compose 中，部署目录固定为 `/root/score-player/deploy/softrouter`。当前服务包括 `sp-postgres`、`sp-minio`、`sp-app`、`sp-webhook`、`sp-cloudflared` 和 `watchtower`；其中用户侧主链路关键容器为 `sp-app`、`sp-webhook` 和 `sp-cloudflared`。数据目录固定为 `/mnt/nas/score-player-data`，app 对外端口为 `9000`，MinIO S3 端口为 `9002`，Webhook 端口为 `9003`。本地 PostgreSQL 与 MinIO 是主库和主对象存储，Render 侧继续作为云端兜底能力。外网 SSH 通过 locally managed Cloudflare Tunnel 实现，`/root/.cloudflared/config.yml` 中的 SSH ingress 必须写为 `service: ssh://192.168.1.2:22`，不能写 `localhost` 或 `127.0.0.1`；Cloudflare DNS 需要手动维护 `ssh` CNAME 到 `91086842-3bb6-4fc7-b11f-d65b0824c36a.cfargotunnel.com`，Zero Trust Routes 页面显示 `Published application` 不等同于 DNS 记录已存在。外网客户端安装 `cloudflared` 后，可在 `~/.ssh/config` 中配置 `Host score-router`、`HostName ssh.scoreplayer-myb.top`、`User root`、`ProxyCommand cloudflared access ssh --hostname %h`，之后用 `ssh score-router` 登录。

镜像主来源为阿里云 ACR：`crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest`。GitHub Actions 同时推送 GHCR：`ghcr.io/myb357/score-player:latest`，仅作为阿里云 ACR 不可达时的备用镜像源。软路由上的 Watchtower 每 5 分钟检查并自动拉取新镜像更新 `sp-app`。此外 CI 采用「Cloudflare Webhook 主动部署」：`docker-publish.yml` 仅监听 `main` 分支推送和 `workflow_dispatch` 手动触发；镜像推送完成后，GitHub Actions 经 Cloudflare Tunnel 暴露的 `https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>` 向软路由 `sp-webhook` 容器发起 POST 请求。软路由 Webhook workflow 使用 `curl --fail-with-body --show-error --silent --retry 3 --retry-delay 5 --retry-all-errors` 打印失败响应并对临时 5xx 或网络错误重试；Render 云端兜底服务通过 GitHub Auto-Deploy 自动更新，不需要 CI 额外触发。`sp-webhook`（`deploy/softrouter/webhook/server.py`，Python 标准库实现，监听 `9003`）校验 query param 中的 token 后优先执行 `docker pull` 阿里云 ACR 镜像；若阿里云 ACR 拉取失败，则降级拉取 GHCR 镜像，并将实际拉到的镜像 tag 为 compose 文件中的阿里云 ACR 镜像标签，部署前会先执行 `docker rm -f sp-app` 清理固定容器名，再执行 `docker-compose up -d --no-deps --force-recreate score-player`，避免 compose 项目状态不一致时出现 `/sp-app` 容器名冲突。`sp-webhook` 直接挂载宿主机 `/usr/bin/docker` 与 `/usr/bin/docker-compose`，启动时打印探测到的路径，不再通过 `apk` 动态安装工具链。该链路依赖 GitHub 仓库 Secret `WEBHOOK_TOKEN` 与软路由 `.env` 的 `WEBHOOK_TOKEN` 保持一致；Render 云端兜底服务依赖 Render 平台自身的 GitHub Auto-Deploy 自动更新。原基于 Tailscale + SSH 的自动部署方案（含 `SOFTROUTER_SSH_KEY`、`TAILSCALE_AUTH_KEY`）已移除。

软路由当前关键运行环境变量包括 `MEDIA_PROXY=1`、`COOKIE_SECURE=0`、`S3_PUBLIC_ENDPOINT=https://media.scoreplayer-myb.top`、`B2_ENDPOINT=http://192.168.1.2:9002`、`DATABASE_URL=postgresql://score:<password>@db:5432/scoredb?sslmode=disable`、`B2_BUCKET=score-player`、`B2_REGION=us-east-1`。软路由 PostgreSQL 使用 `docker.m.daocloud.io/postgres:17-alpine`，`PGDATA=/var/lib/postgresql/data`，必须保持与 `/mnt/nas/score-player-data/postgres` 中既有 PG17 数据目录兼容；不要改回 PG16 或 `/var/lib/postgresql/data/pgdata`，否则会新建空库导致登录和谱子数据异常。其中 `MEDIA_PROXY=1` 是软路由本地 MinIO 模式的正确取值：本地 MinIO 生成的预签名 URL 指向内网 `minio:9000`，终端设备（平板/浏览器）无法直连，因此由 app 经 `/api/media` 回源转发媒体字节流（支持 HTTP Range）；媒体代理当前使用 1MiB 分块转发，并返回 `Cache-Control: public, max-age=604800, immutable`，用于降低 Cloudflare Tunnel 小块传输开销并提升重复打开谱页/伴奏的缓存命中率。`MEDIA_PROXY=0` 仅用于 Render / Backblaze B2 公网对象存储模式，此时才会直接使用 `S3_PUBLIC_ENDPOINT` 作为 S3 client endpoint 并 302 跳转到公网预签名 URL。

Android APK 的原生入口使用“内网软路由优先、Render 云端次级、软路由外网兜底、无网时本地 assets 离线入口”的策略，且 GitHub Actions 已补齐自动构建 APK 步骤，版本号统一对齐为 1.3.29。Android Gradle 构建配置已统一 Java compileOptions 与 Kotlin jvmToolchain 为 JVM 17，避免 CI 中 Java/Kotlin target 不一致。启动或 WebView 首次加载前，Kotlin 侧会按 `http://192.168.1.2:9000`、`https://score-player.onrender.com`、`https://scoreplayer-myb.top` 的顺序发起轻量 HTTP HEAD 探测，连接和读取超时均为 2 秒；若内网可达则直接加载 `http://192.168.1.2:9000`，内网不可达但 Render 可达则加载 `https://score-player.onrender.com`，Render 也不可达时才加载 `https://scoreplayer-myb.top`，三者都不可达时加载 APK 内置 `file:///android_asset/login.html`，确保离线场景仍先经过本地指纹验证；指纹验证成功后再进入本地 `home.html` 并展示已下载谱子。每次 App 进入前台都会重新探测，以适配家庭 Wi-Fi 与外出网络切换。探测结果由原生侧控制 WebView URL，并通过 `AndroidBridge.getActiveBaseUrl()`、`AndroidBridge.isInternalNetworkReachable()` 与 `AndroidBridge.isCloudEndpointReachable()` 暴露给页面。App 原生 WebView 会给状态栏额外留出顶部 padding，避免页面内容贴近系统时间；登录页在已保存 token 且设备已设置锁屏/指纹时默认弹出指纹验证，验证成功后直接进入主页，仍保留密码登录作为兜底。播放页对于已下载谱子会优先读取 IndexedDB 中的本地 blob 资源，命中后不再执行线上详情刷新，避免把本地资源 URL 替换回云端 URL；首页离线且无列表缓存时，会直接读取 IndexedDB 中的已下载谱子元数据展示，避免无网时显示网页无法加载。首页“下载 Android App”和 `/api/version` 的 `apk_url` 均指向 `/download/android`，该路由直接返回镜像内 `static/android/score-player.apk`。当前生产发布链路已恢复为 CI 从 Android 源码构建 APK：GitHub Actions 会先把仓库 `static/` 下的 HTML/CSS/JS/manifest/icons 同步到 `android/app/src/main/assets/`，再生成基于 SHA-256 counter stream 的 `apk-size-guard.bin`，避免门禁文件因内容规律被 ZIP 高度压缩；随后执行 `assembleDebug`，并在打入 Docker 镜像前校验 APK 大小不得小于 1.5MB，且必须包含 `assets/home.html`、`assets/player.html`、`assets/style.css`、`assets/apk-size-guard.bin` 等关键资源，从根上避免 990KB 级别的小包覆盖线上。

软路由 `sync/` 目录包含本地 PostgreSQL / MinIO 向云端 Supabase / Backblaze B2 同步的容器化方案，并新增 `check_consistency.sh` 作为只读一致性巡检入口。该脚本会检查本地 PostgreSQL 核心表行数、本地 MinIO 与云端 B2 的对象数量和总大小；当线上 `.env.sync` 配置了 `CLOUD_DATABASE_URL` 时，还会只读查询云端 Supabase 核心表行数。线上启用 `sp-sync` 前必须确认 `.env.sync` 与 `rclone.conf` 两个不入库配置文件完整可用。发布后仍需验证 Render、Cloudflare 自定义域名与 Tailscale 入口的 `Content-Length`、MD5 和 APK 文件类型一致。

一键迁移脚本为 `deploy/softrouter/migrate.sh`。当前推荐在软路由上直接执行 `bash <(curl -fsSL "https://raw.githubusercontent.com/myb357/score-player/main/deploy/softrouter/migrate.sh")`。脚本已内嵌完整部署所需凭据与配置，包括 DB、MinIO、ACR、Webhook、GitHub Token、Cloudflare Tunnel 等内容；用户无需手动准备 `.env`，也无需先克隆仓库或手动下载 compose 文件，直接运行即可。脚本当前固定执行 9 步：步骤 1/9 创建目录结构（`/root/score-player/deploy/softrouter` 等）；步骤 2/9 写出 `.env`（含 DB、MinIO、ACR、Webhook、GitHub Token 等全部凭据）；步骤 3/9 从 GitHub `main` 分支下载最新 `docker-compose.yml`；步骤 4/9 从 GitHub `main` 分支下载最新 `webhook/server.py`；步骤 5/9 写出 Cloudflare Tunnel 凭证文件；步骤 6/9 写出 Cloudflare `config.yml`；步骤 7/9 ACR 登录；步骤 8/9 执行 `docker-compose up -d`，启动 `db`、`minio`、`score-player`、`sp-webhook`、`cloudflared` 等服务；步骤 9/9 验证容器状态。若目标 `.env` 已存在，脚本采用固定配置优先策略直接覆盖。

GitHub 仓库为 `myb357/score-player`，仓库类型为 private。当前生产发布统一走 `main` 分支，旧软路由自动部署分支已删除，文档、迁移脚本和 CI/CD 均不应再引用该分支。`render.yaml` 和 `railway.json` 均保留为历史或兜底部署配置；当前主生产路径不再依赖 Railway，Render 仅作为最终云端兜底。

## 3. 代码结构

`main.py` 是后端主程序，创建 FastAPI 应用，定义鉴权中间件、页面路由、用户管理 API、谱子 CRUD API、导入导出 API、B2 存储操作、Supabase PostgreSQL 连接池、ffmpeg 音视频转换、图片自动裁剪检测和数据库初始化逻辑。

`static/home.html` 是登录后的主页，负责展示谱子列表、导入谱子、批量选择、批量导出、批量删除、修改密码、清除本地缓存、退出登录，以及根据角色显示用户管理入口。

`static/login.html` 是登录页，提交用户名和密码到 `/api/login`，成功后跳转主页，失败时展示错误信息。

`static/new.html` 是新建和编辑谱子的页面。它支持谱子命名、选择翻页模式、多图片上传、拖拽排序、图片自动裁边后手动裁剪、上传音频或视频、截取伴奏片段、基于高级节拍分析整首伴奏节拍并自动推荐节拍器 BPM 与偏移、人工填写 BPM 时仅按既定 BPM 推荐偏移、试听选段时同步播放节拍器，以及编辑已有谱子时保留、替换或移除伴奏。节拍器配置包含 BPM、0.01 秒粒度的时间轴偏移和音量，保存到谱子设置内。

`static/player.html` 是播放器页面。它加载单个谱子详情和 B2 预签名资源地址，支持图片展示、自动翻页、手动翻页、播放暂停、单曲循环/顺序播放/随机播放模式、网页内伴奏音量增益、节拍器左侧悬浮二级菜单（开关和音量会按谱子记住上次状态）、倍速、A-B 循环、进度拖动、触屏快进快退、缩放平移、滚动/适应屏幕模式切换、全屏和导出当前谱子。

`static/users.html` 是超级管理员用户管理页，支持查看用户列表、创建普通用户或超级管理员、删除用户、重置用户密码，并对非超级管理员访问做前端守卫。

`static/style.css` 是全站样式文件，定义深色主题、布局、按钮、表单、卡片、播放器左右控制栏、移动端适配、Toast、上传区、裁剪弹窗等视觉样式。

`Dockerfile` 是容器构建文件，安装 Python 依赖和 ffmpeg，并以 Web 服务方式运行应用。运行期中间产物集中在 `SCORE_DATA_DIR`，包括 ffmpeg 转码和节拍分析临时文件；程序会按前缀清理过期临时文件，默认 6 小时，可通过 `SCORE_RUNTIME_TMP_MAX_AGE_SECONDS` 调整。

`requirements.txt` 是 Python 依赖清单，包含 FastAPI、Uvicorn、psycopg2、boto3、Pillow、python-multipart、imageio-ffmpeg、librosa 等运行依赖。

`render.yaml` 是历史 Render Blueprint 配置，定义 Web Service、Dockerfile 路径、免费套餐、健康检查路径 `/api/v1/ping`，以及 Render 环境变量声明。

`railway.json` 是 Railway 部署配置，指定 Dockerfile 构建、启动命令、健康检查路径 `/api/v1/ping` 和失败重启策略。

`DEPLOY.md` 是部署说明文档，记录 Render/Railway 部署方式，以及 Render 连接 Supabase 时必须改用 IPv4 可达的 Session Pooler 的原因和处理步骤。

`README.md` 是项目基础说明文档，用于介绍项目和基本使用方式。

`run.sh` 是运行脚本，通常用于容器或平台环境启动服务。

`.env.example` 是本地环境变量示例文件，只能放占位示例值，不应包含真实密钥。

`.env` 是本地环境变量文件，可能包含敏感配置，不应提交到 Git。

`.gitignore` 是 Git 忽略规则文件，当前用于避免提交本地环境和运行产物。

`score-player-source.zip` 是当前目录中的源码压缩包或历史产物，不是线上运行必需文件，后续开发一般不要改动或依赖它。

`venv/` 是本地 Python 虚拟环境目录，不应提交到 Git，也不应作为代码分析或部署依据。

`__pycache__/` 是 Python 字节码缓存目录，不应提交到 Git。

## 4. 数据库结构

数据库由 `main.py` 的 `init_db()` 初始化，使用 PostgreSQL 表。初始化时通过 `CREATE TABLE IF NOT EXISTS` 创建表，并通过 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 兼容旧数据库迁移。

`users` 表用于保存用户账号和角色。字段包括 `id SERIAL PRIMARY KEY`、`username TEXT UNIQUE NOT NULL`、`salt TEXT NOT NULL`、`password_hash TEXT NOT NULL`、`role TEXT NOT NULL DEFAULT 'user'`、`created_at BIGINT NOT NULL`。其中 `role` 当前支持 `superadmin` 和 `user`。

`sessions` 表用于保存登录会话。字段包括 `token TEXT PRIMARY KEY`、`user_id INTEGER REFERENCES users(id) ON DELETE CASCADE`、`created_at BIGINT NOT NULL`、`expires_at BIGINT NOT NULL`。初始化时会补充 `user_id` 字段，并删除历史遗留的 `user_id IS NULL` 会话。

`scores` 表用于保存谱子主信息。字段包括 `id SERIAL PRIMARY KEY`、`name TEXT NOT NULL`、`mode TEXT NOT NULL`、`audio_filename TEXT`、`owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE`、`created_at BIGINT NOT NULL`。其中 `mode` 当前取值为 `audio` 或 `countdown`，分别表示跟伴奏时间轴和独立倒计时。

`pages` 表用于保存每个谱子的谱页信息。字段包括 `id SERIAL PRIMARY KEY`、`score_id INTEGER NOT NULL REFERENCES scores(id) ON DELETE CASCADE`、`page_index INTEGER NOT NULL`、`image_filename TEXT NOT NULL`、`turn_seconds DOUBLE PRECISION NOT NULL DEFAULT 0`。其中 `page_index` 表示页面顺序，`turn_seconds` 在跟伴奏模式下表示翻到下一页的音频时间点，在倒计时模式下表示当前页停留秒数。

## 5. 已实现功能清单

用户登录功能已实现，用户通过 `/login` 登录，后端校验账号密码后写入 HttpOnly Cookie 会话。

会话鉴权中间件已实现，除登录页、登录 API、健康检查和静态资源外，其他页面与 API 都需要登录访问。

默认超级管理员初始化已实现，数据库首次初始化时会创建默认超管账号。

用户管理功能已实现，超级管理员可以查看用户、创建用户、删除用户和重置用户密码。

用户自助修改密码已实现，普通用户和超级管理员都可以在主页设置菜单中修改自己的密码。

谱子列表功能已实现，超级管理员可以看到所有谱子，普通用户只能看到自己创建的谱子。

谱子新建功能已实现，用户可以上传多张谱页图片并保存为一个谱子项目。

谱页图片自动裁边功能已实现，后端基于图片亮度检测内容区域，前端使用 CropperJS 让用户确认或微调裁剪框。

谱页拖拽排序功能已实现，新建或编辑时可以调整谱页顺序。

跟伴奏时间轴模式已实现，播放伴奏时会根据每页配置的时间点自动翻页。

独立倒计时模式已实现，不依赖音频，按每页配置的秒数自动翻页。

音频上传功能已实现，支持 mp3、wav、ogg、m4a、aac、flac 等常见音频格式。

视频上传并提取伴奏功能已实现，支持 mp4、mov、mkv、avi、webm、m4v、flv、wmv 等视频格式，并用 ffmpeg 转为 mp3。

伴奏片段裁剪功能已实现，上传或替换伴奏时可以截取起止时间之间的片段作为最终伴奏。

谱子编辑功能已实现，用户可以修改名称、翻页模式、谱页顺序、每页时间点，并保留、替换或移除伴奏。

谱子删除功能已实现，删除数据库记录前会删除该谱子在 B2 中的对象文件。

单个谱子导出功能已实现，可导出包含 `manifest.json`、谱页图片和伴奏文件的 ZIP 包。

批量导出功能已实现，可选择多个谱子并打包导出为一个 ZIP 包。

谱子导入功能已实现，可上传符合导出结构的 ZIP 包恢复谱子和资源文件。

批量删除功能已实现，可选择多个可访问谱子并一次性删除数据库记录和 B2 对象。

播放器基础播放功能已实现，支持播放、暂停、上一页、下一页、页面指示，以及伴奏结束后的单曲循环、顺序播放和随机播放。

播放器倍速功能已实现，支持 0.5x 到 2.0x 范围内调整播放速度；播放页会显式开启 `preservesPitch` 保持原音高，并限制过低速率以减少浏览器原生保调算法在极端倍速下的失真。播放器伴奏音量增益已实现，基于 Web Audio GainNode 在网页内将伴奏输出提升到 1.0x 到 3.0x，不修改系统音量。

播放器 A-B 循环功能已实现，可设置循环起点 A、终点 B 并清除循环。

播放器垂直进度条拖动功能已实现，可通过右侧进度条定位音频播放时间。

播放器触屏横向快进快退功能已实现，移动端可通过横向滑动进行音频快进或快退。

播放器图片缩放和平移功能已实现，支持鼠标滚轮缩放、拖拽平移、双击还原，以及触屏双指缩放。

播放器显示模式切换已实现，可在适应屏幕和滚动模式之间切换，并使用 localStorage 记忆每个谱子的设置。

播放器全屏功能已实现，支持原生 fullscreen，不支持时退化为页面内伪全屏。

前端缓存功能已实现，主页缓存谱子列表，播放器缓存谱子详情和图片/音频资源，提升重复访问速度并支持弱网下使用旧缓存。

清除缓存功能已实现，用户可在主页设置菜单中清除本地列表缓存、详情缓存、视图模式缓存和 Cache Storage 资源缓存。

B2 孤儿对象清理 API 已实现，超级管理员可调用 `/api/admin/b2/cleanup-orphans` 扫描并删除未被数据库引用的 `scores/` 前缀对象，支持 `dry_run` 参数。

健康检查接口已实现，`/api/v1/ping` 返回 `pong`，Render 使用它作为健康检查路径。

FastAPI OpenAPI 文档接口已开启，路径为 `/api/docs`、`/api/redoc`、`/api/openapi.json`。

## 6. 登录账号

默认超级管理员账号为 `admin`，默认密码为 `DXKM7in3GIO-nqBP`。

代码中不会保存明文密码，默认账号通过 `ADMIN_USERNAME`、`ADMIN_SALT`、`ADMIN_HASH` 组合初始化。线上如需要修改默认管理员密码，应生成新的 PBKDF2 salt/hash，并通过 Render 环境变量覆盖，而不是把明文密码写入代码。

## 7. 关键设计决策和注意事项

Render 线上连接 Supabase 必须使用 Session Pooler。原因是 Supabase 直连地址 `db.<ref>.supabase.co` 可能只有 IPv6 地址，而 Render 出网只支持 IPv4，导致线上容器无法连接数据库。Session Pooler 的 `aws-0-ap-northeast-1.pooler.supabase.com:5432` 可通过 IPv4 访问，并兼容当前 psycopg2 连接池逻辑，因此应作为线上 `DATABASE_URL` 的基础地址。

B2 文件路径规则以谱子 ID 隔离。所有谱子相关对象都放在 `scores/{score_id}/` 前缀下。新建谱子时谱页文件命名为 `page_{idx:03d}{ext}`，音频统一保存为 `audio.mp3`。编辑谱子新增图片时使用 `p_{随机hex}{ext}` 避免覆盖已有图片，替换音频时使用 `audio_{随机hex}.mp3` 避免与旧音频冲突。读取和下载资源时后端使用 B2 预签名 URL，兼容旧访问方式的 `/api/media/{score_id}/{filename}` 会 302 跳转到预签名 URL。

缓存策略分为列表缓存、详情缓存和资源缓存。主页用 localStorage 保存 `score-player:score-list-cache`，有效期为 1 小时。播放器用 localStorage 保存 `score-player:score-detail:{scoreId}`，并用 Cache Storage 的 `score-player-score-assets-v1` 缓存谱页图片和伴奏资源。播放器会先尝试展示本地缓存，再后台请求最新数据；若发现版本变化，会刷新资源缓存并重新渲染。清除缓存按钮会清理列表缓存、详情缓存、视图模式缓存和 Cache Storage。

数据一致性策略采用“数据库记录 + B2 对象”的补偿式一致性。创建谱子时先在事务中插入数据库记录并上传 B2 对象，如果过程中失败，会尽力删除已上传的 B2 对象。删除谱子或删除用户时，会先收集并删除相关 B2 对象，再删除数据库记录，数据库通过外键级联删除 pages 和 sessions。编辑谱子时先上传新增对象，再在事务中重写 pages 和更新 scores；事务成功后再尽力删除不再引用的旧图片或旧音频。导入失败时同样会尽力删除已上传对象。由于 B2 与 PostgreSQL 不是同一个事务系统，代码通过 best-effort cleanup 和孤儿清理 API 降低不一致风险。

鉴权和权限设计以会话 Cookie 与角色控制为核心。`sid` Cookie 设置为 HttpOnly、Secure、SameSite=Lax，有效期 7 天。普通用户只能访问和操作自己拥有的谱子；超级管理员可以访问和管理所有谱子及用户。中间件对未登录 API 返回 401，对未登录页面跳转登录页。

对象存储预签名 URL 有效期默认为 7 天，即 S3 最大值 604800 秒。前端缓存资源时会缓存预签名 URL 的响应内容，而不是依赖旧 URL 永久有效。

后端运行时临时目录仅用于 ffmpeg 转码临时文件，默认 `SCORE_DATA_DIR=/tmp/score_app_data`。持久数据全部外置到 Supabase PostgreSQL 和 Backblaze B2，因此 Render 服务本身设计为无状态，容器重建不会丢失谱子数据。

## 8. 增量开发指引

本地运行时，在仓库根目录创建并启用 Python 虚拟环境，安装依赖后配置环境变量，再启动 `main.py`。典型流程是执行 `python -m venv venv`，启用虚拟环境，执行 `pip install -r requirements.txt`，复制 `.env.example` 为 `.env` 并填入本地或测试环境的 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET` 等配置，然后执行 `python main.py` 或使用 `uvicorn main:app --host 0.0.0.0 --port 8000` 启动服务。启动后可访问 `/api/v1/ping` 验证服务健康，再访问 `/login` 登录。

推送代码触发当前生产部署时，应在仓库工作区确认只修改了预期文件，避免提交 `.env`、`venv/`、`__pycache__/`、本地临时文件或敏感密钥。提交到 GitHub 仓库 `myb357/score-player` 的 `main` 分支后，GitHub Actions `docker-publish.yml` 会自动构建 Android APK、构建并推送 Docker 镜像到 ACR 与 GHCR，并通过软路由 Webhook 触发 `sp-app` 更新；Watchtower 仍作为轮询兜底。

常见坑之一是 Render 无法连接 Supabase 直连地址。线上 `DATABASE_URL` 必须使用 Session Pooler，并带上 `sslmode=require` 或让代码自动追加 `sslmode=require`。如果使用直连 `db.<ref>.supabase.co`，Render 可能因 IPv6 不可达导致登录或数据库操作 500。

常见坑之二是不要把真实密钥写入仓库。`DATABASE_URL`、B2 key、B2 app key、管理员 salt/hash 等都应通过 Render Environment 或本地 `.env` 配置，仓库中只保存变量名和示例值。

常见坑之三是 B2 对象和数据库不是强事务一致。任何新增、删除、导入、编辑资源的功能都必须考虑失败回滚和孤儿对象清理。新增文件路径必须继续放在 `scores/{score_id}/` 下，删除记录时必须同步删除 B2 对象，批量操作要注意权限过滤。

常见坑之四是编辑播放器或缓存逻辑时要同步考虑预签名 URL 的有效期。前端不能假设 B2 URL 永久有效，应继续保持“先用本地缓存展示，再后台刷新最新数据和资源”的模式。

常见坑之五是视频转音频依赖 ffmpeg。Dockerfile 已安装 ffmpeg，代码也会尝试使用 `imageio_ffmpeg` 兜底；如果本地运行转换失败，需要确认本机有 ffmpeg 或依赖安装完整。

常见坑之六是当前项目前端没有构建流程，所有页面都在 `static/` 下直接维护。修改 HTML/CSS/JS 后不需要 npm build，但应手动访问对应页面验证登录、上传、编辑、播放、导入导出和移动端交互。

常见坑之七是用户权限要保持一致。新增 API 时必须通过 `current_user(request)` 获取当前用户，并明确判断普通用户只能操作自己的 `owner_id` 记录，超级管理员才可以跨用户操作。

常见坑之八是数据库迁移目前写在 `init_db()` 中。新增字段或表时，应优先使用向后兼容的 `CREATE TABLE IF NOT EXISTS` 或 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，避免破坏已有 Supabase 数据。
