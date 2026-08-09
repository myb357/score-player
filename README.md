# 谱子播放器（Score Player）

一个单用户的乐谱/谱子在线播放应用：上传谱页图片 + 伴奏（音频或视频），设置翻页规则后即可全屏跟谱演奏。

## 功能
- 登录系统：单用户（`admin`）、密码 PBKDF2 加密存储、基于 Cookie 的会话（7 天过期）。
- 主页：已保存谱子卡片列表（名称 + 创建时间 + 模式），支持进入播放、删除。
- 新建谱子：多图上传并拖拽排序、逐页设置翻页时间点、两种翻页模式、伴奏上传（音频或视频，视频自动用 ffmpeg 提取音轨）、音频在线剪辑（ffmpeg 裁剪片段）、节拍器 BPM / 0.01 秒时间轴偏移 / 音量配置，支持 librosa 基于整首伴奏推荐 BPM 和偏移；已填写 BPM 时只推荐偏移，试听选段时同步播放节拍器用于对齐伴奏。
- 播放页：全屏谱面、伴奏播放/暂停/进度条、自动翻页（跟伴奏时间轴 / 独立倒计时）、手动翻页、默认单曲循环，并可切换顺序播放或随机播放，支持可开关节拍器、节拍器音量调节（开关和音量收纳在左侧悬浮二级菜单，并记住上次状态）、网页内伴奏音量增益、保留原音高的倍速播放（0.5x～2.0x，0.1 步长，滑块 + 微调按钮）、A-B 分段循环、全屏模式（控制栏自动隐藏、移动/触摸浮现）。
- 全站移动端响应式。

## 技术栈
- 后端：FastAPI（Python），Supabase PostgreSQL 存元数据，Backblaze B2 存图片/音频，ffmpeg 处理音视频。
- 前端：原生 HTML/CSS/JS（无框架），由后端直接托管。
- 部署：Docker 容器，已提供 Railway 配置。

## 本地运行
```bash
git clone <repo-url>
cd score-player
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 填入 DATABASE_URL、B2_KEY_ID、B2_APP_KEY 等真实配置
uvicorn main:app --host 0.0.0.0 --port 8000
# 打开 http://localhost:8000
```

## 环境变量
| 变量 | 说明 | 默认 |
|---|---|---|
| `SCORE_DATA_DIR` | 运行时临时目录（仅用于 ffmpeg 转码等临时文件） | `/tmp/score_app_data` |
| `DATABASE_URL` | Supabase PostgreSQL 连接串 | 无，必填 |
| `B2_KEY_ID` / `B2_APP_KEY` | Backblaze B2 S3 兼容访问密钥 | 无，必填 |
| `B2_ENDPOINT` | Backblaze B2 S3 endpoint | `s3.ca-east-006.backblazeb2.com` |
| `B2_BUCKET` | Backblaze B2 bucket | `score-player` |
| `B2_REGION` | Backblaze B2 region | 可从 endpoint 自动解析 |
| `ADMIN_USERNAME` | 登录用户名 | `admin` |
| `ADMIN_SALT` / `ADMIN_HASH` | 密码的 PBKDF2 盐与哈希（仅存哈希） | 内置默认 |
| `FFMPEG_BINARY` | 指定 ffmpeg 路径（可选） | 自动探测系统/内置 |

> 密码只以 PBKDF2-HMAC-SHA256（20 万轮）哈希形式保存，不存明文。如需修改密码，用相同算法生成新的 salt/hash 并通过环境变量覆盖。

## 部署到 Render / Railway（长期常驻）
详见 [DEPLOY.md](DEPLOY.md)。镜像已内置 ffmpeg。
