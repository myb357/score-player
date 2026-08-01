# score-player 项目开发上下文

> 文档目的：这份文档用于在未来交给 AI 或新开发者时，快速恢复 score-player 项目的完整上下文，直接开展增量开发。本文基于当前 `score_app/` 代码目录整理，重点覆盖项目定位、部署方式、代码结构、数据库结构、已实现功能、关键设计决策和后续开发注意事项。

## 1. 项目概述

score-player 是一个“谱子 + 伴奏播放器”网站。用户可以上传多张谱页图片，并可选择上传伴奏音频或视频，系统将其转换为可播放的谱子项目；播放时支持跟随伴奏时间轴自动翻页，也支持不依赖音频的独立倒计时翻页。

项目技术栈为 Flask/Python 后端、原生 HTML/CSS/JS 前端。当前代码实际使用 FastAPI 实现 Python Web 服务，前端仍是无构建步骤的原生 HTML、CSS、JavaScript 文件。整体应用部署为 Docker Web Service，后端负责鉴权、数据库访问、对象存储读写、音视频处理和 API 输出，前端负责页面渲染、上传交互、播放器控制、缓存和管理界面。

## 2. 部署信息

公网地址为 `https://score-player.onrender.com`。

部署平台为 Render，通过 GitHub 自动部署，`main` 分支触发线上构建与发布。

GitHub 仓库为 `myb357/score-player`，仓库类型为 private。

数据库使用 Supabase PostgreSQL。Render 线上环境必须使用 Supabase Session Pooler 地址连接数据库，当前约定的 Session Pooler 主机与端口为 `aws-0-ap-northeast-1.pooler.supabase.com:5432`。

文件存储使用 Backblaze B2 的 S3 兼容接口，bucket 为 `score-player`，endpoint 为 `s3.ca-east-006.backblazeb2.com`。

Render 环境变量只应配置变量名，不应把敏感值写入仓库。核心变量名包括 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET`、`SECRET_KEY`。当前代码和 `render.yaml` 还会读取或声明 `SCORE_DATA_DIR`、`B2_REGION`、`B2_PRESIGN_TTL`、`ADMIN_USERNAME`、`ADMIN_SALT`、`ADMIN_HASH`、`FFMPEG_BINARY` 等变量；这些变量用于运行时临时目录、B2 区域、预签名 URL 有效期、管理员凭据覆盖和 ffmpeg 路径覆盖。

## 3. 代码结构

`score_app/main.py` 是后端主程序，创建 FastAPI 应用，定义鉴权中间件、页面路由、用户管理 API、谱子 CRUD API、导入导出 API、B2 存储操作、Supabase PostgreSQL 连接池、ffmpeg 音视频转换、图片自动裁剪检测和数据库初始化逻辑。

`score_app/static/home.html` 是登录后的主页，负责展示谱子列表、导入谱子、批量选择、批量导出、批量删除、修改密码、清除本地缓存、退出登录，以及根据角色显示用户管理入口。

`score_app/static/login.html` 是登录页，提交用户名和密码到 `/api/login`，成功后跳转主页，失败时展示错误信息。

`score_app/static/new.html` 是新建和编辑谱子的页面。它支持谱子命名、选择翻页模式、多图片上传、拖拽排序、图片自动裁边后手动裁剪、上传音频或视频、截取伴奏片段，以及编辑已有谱子时保留、替换或移除伴奏。

`score_app/static/player.html` 是播放器页面。它加载单个谱子详情和 B2 预签名资源地址，支持图片展示、自动翻页、手动翻页、播放暂停、倍速、A-B 循环、进度拖动、触屏快进快退、缩放平移、滚动/适应屏幕模式切换、全屏和导出当前谱子。

`score_app/static/users.html` 是超级管理员用户管理页，支持查看用户列表、创建普通用户或超级管理员、删除用户、重置用户密码，并对非超级管理员访问做前端守卫。

`score_app/static/style.css` 是全站样式文件，定义深色主题、布局、按钮、表单、卡片、播放器左右控制栏、移动端适配、Toast、上传区、裁剪弹窗等视觉样式。

`score_app/Dockerfile` 是容器构建文件，安装 Python 依赖和 ffmpeg，并以 Web 服务方式运行应用。

`score_app/requirements.txt` 是 Python 依赖清单，包含 FastAPI、Uvicorn、psycopg2、boto3、Pillow、python-multipart、imageio-ffmpeg 等运行依赖。

`score_app/render.yaml` 是 Render Blueprint 配置，定义 Web Service、Dockerfile 路径、免费套餐、健康检查路径 `/api/v1/ping`，以及 Render 环境变量声明。

`score_app/DEPLOY.md` 是部署说明文档，记录 Render/Railway 部署方式，以及 Render 连接 Supabase 时必须改用 IPv4 可达的 Session Pooler 的原因和处理步骤。

`score_app/README.md` 是项目基础说明文档，用于介绍项目和基本使用方式。

`score_app/run.sh` 是运行脚本，通常用于容器或平台环境启动服务。

`score_app/.env.example` 是本地环境变量示例文件，只能放占位示例值，不应包含真实密钥。

`score_app/.env` 是本地环境变量文件，可能包含敏感配置，不应提交到 Git。

`score_app/.gitignore` 是 Git 忽略规则文件，当前用于避免提交本地环境和运行产物。

`score_app/score-player-source.zip` 是当前目录中的源码压缩包或历史产物，不是线上运行必需文件，后续开发一般不要改动或依赖它。

`score_app/venv/` 是本地 Python 虚拟环境目录，不应提交到 Git，也不应作为代码分析或部署依据。

`score_app/__pycache__/` 是 Python 字节码缓存目录，不应提交到 Git。

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

播放器基础播放功能已实现，支持播放、暂停、上一页、下一页和页面指示。

播放器倍速功能已实现，支持 0.1x 到 2.0x 范围内调整播放速度。

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

本地运行时，进入 `score_app/` 目录，创建并启用 Python 虚拟环境，安装依赖后配置环境变量，再启动 `main.py`。典型流程是执行 `python -m venv venv`，启用虚拟环境，执行 `pip install -r requirements.txt`，复制 `.env.example` 为 `.env` 并填入本地或测试环境的 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET` 等配置，然后执行 `python main.py` 或使用 `uvicorn main:app --host 0.0.0.0 --port 8000` 启动服务。启动后可访问 `/api/v1/ping` 验证服务健康，再访问 `/login` 登录。

推送代码触发 Render 部署时，应在 `score_app/` 仓库工作区确认只修改了预期文件，避免提交 `.env`、`venv/`、`__pycache__/`、本地临时文件或敏感密钥。提交到 GitHub 仓库 `myb357/score-player` 的 `main` 分支后，Render 会自动拉取 main 分支并重新构建部署。若自动部署未触发，可在 Render 控制台执行 Manual Deploy。

常见坑之一是 Render 无法连接 Supabase 直连地址。线上 `DATABASE_URL` 必须使用 Session Pooler，并带上 `sslmode=require` 或让代码自动追加 `sslmode=require`。如果使用直连 `db.<ref>.supabase.co`，Render 可能因 IPv6 不可达导致登录或数据库操作 500。

常见坑之二是不要把真实密钥写入仓库。`DATABASE_URL`、B2 key、B2 app key、管理员 salt/hash 等都应通过 Render Environment 或本地 `.env` 配置，仓库中只保存变量名和示例值。

常见坑之三是 B2 对象和数据库不是强事务一致。任何新增、删除、导入、编辑资源的功能都必须考虑失败回滚和孤儿对象清理。新增文件路径必须继续放在 `scores/{score_id}/` 下，删除记录时必须同步删除 B2 对象，批量操作要注意权限过滤。

常见坑之四是编辑播放器或缓存逻辑时要同步考虑预签名 URL 的有效期。前端不能假设 B2 URL 永久有效，应继续保持“先用本地缓存展示，再后台刷新最新数据和资源”的模式。

常见坑之五是视频转音频依赖 ffmpeg。Dockerfile 已安装 ffmpeg，代码也会尝试使用 `imageio_ffmpeg` 兜底；如果本地运行转换失败，需要确认本机有 ffmpeg 或依赖安装完整。

常见坑之六是当前项目前端没有构建流程，所有页面都在 `static/` 下直接维护。修改 HTML/CSS/JS 后不需要 npm build，但应手动访问对应页面验证登录、上传、编辑、播放、导入导出和移动端交互。

常见坑之七是用户权限要保持一致。新增 API 时必须通过 `current_user(request)` 获取当前用户，并明确判断普通用户只能操作自己的 `owner_id` 记录，超级管理员才可以跨用户操作。

常见坑之八是数据库迁移目前写在 `init_db()` 中。新增字段或表时，应优先使用向后兼容的 `CREATE TABLE IF NOT EXISTS` 或 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，避免破坏已有 Supabase 数据。
