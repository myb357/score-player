# 部署指南：迁移到 Railway 并获取长期稳定的公网 HTTPS 链接

本应用已容器化（`Dockerfile` 内置 ffmpeg），当前迁移目标为 Railway。仓库根目录已提供 `railway.json`，Railway 会按根目录 `Dockerfile` 构建，并使用 `/api/v1/ping` 做健康检查。

---

## 方案 A：Railway（当前推荐）

### 步骤
1. 打开 https://railway.app ，使用 GitHub 登录。
2. 点击 **New Project** → **Deploy from GitHub repo**，选择 `myb357/score-player` 仓库。
3. Railway 会读取仓库根目录的 `railway.json`，并按 `Dockerfile` 构建服务。
4. 到服务的 **Variables** 配置运行时环境变量，至少需要填入 `DATABASE_URL`、`B2_KEY_ID`、`B2_APP_KEY`、`B2_ENDPOINT`、`B2_BUCKET`、`SECRET_KEY`；如需覆盖默认管理员账号，再配置 `ADMIN_USERNAME`、`ADMIN_SALT`、`ADMIN_HASH`。
5. 到服务的 **Settings → Networking → Generate Domain** 生成公网 HTTPS 域名。
6. 部署完成后访问 `https://<railway-domain>/api/v1/ping`，返回 `pong` 即表示应用健康。

### Railway 必填/建议环境变量
| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | Supabase PostgreSQL 连接串。建议使用 Session Pooler，并带 `sslmode=require`；代码也会自动补充 `sslmode=require`。 |
| `B2_KEY_ID` | Backblaze B2 S3 兼容 Access Key。 |
| `B2_APP_KEY` | Backblaze B2 S3 兼容 Secret Key。 |
| `B2_ENDPOINT` | 当前约定为 `s3.ca-east-006.backblazeb2.com`。 |
| `B2_BUCKET` | 当前约定为 `score-player`。 |
| `B2_REGION` | 当前约定为 `ca-east-006`，不填时也可从 endpoint 自动解析。 |
| `SCORE_DATA_DIR` | 运行时临时目录，建议保持 `/tmp/score_app_data`。 |
| `SECRET_KEY` | 会话签名密钥，生产环境必须配置为随机强密钥。 |
| `ADMIN_USERNAME` / `ADMIN_SALT` / `ADMIN_HASH` | 可选，用于覆盖默认管理员账号和密码哈希。 |

### 迁移注意事项
本项目的持久数据已外置到 Supabase PostgreSQL 和 Backblaze B2，Railway 容器本身只保存 ffmpeg 转码临时文件，因此无需迁移本地磁盘数据。`SCORE_DATA_DIR` 可以继续使用 `/tmp/score_app_data`，不要把谱子图片或音频改回容器本地存储。

如果线上域名从 Render 切换为 Railway，Android 包或前端中如有写死旧域名，需要同步更新为新的 Railway 域名。

---

## 方案 B：Render（历史方案，含免费套餐）

### 步骤
1. 把 `score_app/` 目录推到你自己的 Git 仓库（GitHub / GitLab 均可）。
2. 打开 https://render.com ，用 GitHub 登录。
3. 点击 **New +** → **Blueprint**，选择该仓库；Render 会自动读取 `render.yaml` 创建 Web Service。
   - 或选择 **New +** → **Web Service** → **Docker**，手动指向 `Dockerfile`。
4. 等待构建完成，即可得到固定域名，例如：`https://score-player.onrender.com`（HTTPS，长期有效）。

### 套餐说明（重要）
| 套餐 | 费用 | 数据持久化 | 常驻 |
|---|---|---|---|
| **Free** | 免费 | ❌ 无磁盘，重启/重新部署后数据清空 | ⚠️ 约 15 分钟无访问会休眠，下次访问需几十秒唤醒 |
| **Starter** | ~$7/月 | ✅ 挂载持久磁盘后永久保存 | ✅ 一直在线不休眠 |

- 免费套餐可满足“公网 HTTPS 链接长期有效”，但**数据不持久**且会休眠。
- 若要**数据永久保存 + 永不休眠**：在 Render 把 `plan` 改为 `starter`，并在 `render.yaml` 中取消 `disk:` 段落的注释（挂载 `/data`）。

---

## 保持长期运行的建议
- **推荐**：Railway Hobby。当前架构已使用 Supabase PostgreSQL + Backblaze B2 做持久化，容器本地仅保存临时转码文件。
- **Render 历史保活方案**：Render Free 会休眠，可用 UptimeRobot（免费）每 5～10 分钟访问一次 `https://<你的域名>/api/v1/ping` 来减少休眠概率（但不保证 100% 常驻）。
- 不建议把业务数据重新放回 Railway/Render 本地磁盘；如仅用于 ffmpeg 临时文件，`SCORE_DATA_DIR=/tmp/score_app_data` 即可。

## 修改登录密码
用下面命令生成新的 salt/hash，再在平台环境变量里设置 `ADMIN_SALT` 与 `ADMIN_HASH`：
```python
import secrets, hashlib
pw = "你的新密码"
salt = secrets.token_hex(16)
h = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200000).hex()
print("ADMIN_SALT=", salt); print("ADMIN_HASH=", h)
```

---

## ⚠️ 故障排查：Render 登录报错 500 / 数据库连不上（IPv6 问题）

### 现象
- `https://score-player.onrender.com/api/v1/ping` 返回 200（应用本身正常）；
- 但 `/api/login` 返回 **500 Internal Server Error**，网页登录失败。

### 根因
Supabase 的**直连地址** `db.<ref>.supabase.co` 现在**只有 IPv6（AAAA）地址，没有 IPv4（A）地址**。
而 **Render 的出网只支持 IPv4**，因此 Render 容器无法连上 Supabase 直连端口，所有数据库操作抛异常 → 登录 500。
（本地开发机若有 IPv6 则能连上，所以本地正常、线上失败。）

### 修复：把 Render 的 `DATABASE_URL` 换成 Supabase 的 IPv4「连接池（Connection Pooler / Supavisor）」地址

1. 打开 Supabase 控制台 → **Project Settings → Database → Connection string** → 选择 **Session pooler**（或页面上的 "Connection pooling"）。
2. 复制其中的 URI，形如：
   ```
   postgresql://postgres.<项目ref>:<你的密码>@aws-0-<区域>.pooler.supabase.com:5432/postgres
   ```
   - 用户名是 `postgres.<项目ref>`（注意带项目 ref，和直连不同）；
   - 主机是 `aws-0-<区域>.pooler.supabase.com`（**IPv4 可达**）；
   - **Session 模式用 5432 端口**（推荐，兼容连接池）；Transaction 模式用 6543。
   - 本项目 ref 为 `vniuunggpcvjriysjgcx`，区域 `<区域>` 请以控制台显示为准（如 `us-east-1` / `ap-northeast-1` 等）。
3. 到 **Render → 你的服务 → Environment**，把 `DATABASE_URL` 的值改成上面复制的 pooler URI，保存。
4. Render 会自动重建；或手动 **Manual Deploy → Clear build cache & deploy / Deploy latest commit**。
5. 重新访问网站登录（admin / 你的密码）即可。

> 说明：数据库里的 `users`/`admin` 数据、密码哈希都是正确的（已核验），无需重建数据；这纯粹是 Render→Supabase 的网络可达性（IPv6）配置问题，只需替换连接串。
> 若密码中含特殊字符（如 `!`）导致 URI 解析异常，可对该字符做 URL 编码（`!` → `%21`）。
