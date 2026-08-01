# 部署指南：获取长期稳定的公网 HTTPS 链接

本应用已容器化（`Dockerfile` 内置 ffmpeg），可一键部署到 Render 或 Railway。

---

## 方案 A：Render（推荐，含免费套餐）

### 步骤
1. 把 `score_app/` 目录推到你自己的 Git 仓库（GitHub / GitLab 均可）。
2. 打开 https://render.com ，用 GitHub 登录。
3. 点击 **New +** → **Blueprint**，选择该仓库；Render 会自动读取 `render.yaml` 创建 Web Service。
   - 或选择 **New +** → **Web Service** → **Docker**，手动指向 `Dockerfile`。
4. 等待构建完成，即可得到固定域名，例如：`https://score-app-xxxx.onrender.com`（HTTPS，长期有效）。

### 套餐说明（重要）
| 套餐 | 费用 | 数据持久化 | 常驻 |
|---|---|---|---|
| **Free** | 免费 | ❌ 无磁盘，重启/重新部署后数据清空 | ⚠️ 约 15 分钟无访问会休眠，下次访问需几十秒唤醒 |
| **Starter** | ~$7/月 | ✅ 挂载持久磁盘后永久保存 | ✅ 一直在线不休眠 |

- 免费套餐可满足“公网 HTTPS 链接长期有效”，但**数据不持久**且会休眠。
- 若要**数据永久保存 + 永不休眠**：在 Render 把 `plan` 改为 `starter`，并在 `render.yaml` 中取消 `disk:` 段落的注释（挂载 `/data`）。

---

## 方案 B：Railway

1. 打开 https://railway.app ，用 GitHub 登录。
2. **New Project** → **Deploy from GitHub repo**，选择仓库。Railway 自动识别 `Dockerfile`。
3. 部署后在服务的 **Settings → Networking → Generate Domain** 生成公网域名（HTTPS）。
4. 持久化：**Settings → Volumes** 新建 Volume 并挂载到 `/data`（即 `SCORE_DATA_DIR`）。
5. 费用：Railway 无免费常驻套餐，提供试用额度，之后约 $5/月起。

---

## 保持长期运行的建议
- **推荐**：Render Starter（$7/月）或 Railway Hobby——常驻、带持久磁盘，最省心。
- **免费方案保活**：Render Free 会休眠，可用 UptimeRobot（免费）每 5～10 分钟访问一次 `https://<你的域名>/api/v1/ping` 来减少休眠概率（但不保证 100% 常驻）。
- 无论哪种方案，务必挂载 `/data` 持久磁盘，否则谱子数据在容器重建后会丢失。

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
- `https://<你的服务>.onrender.com/api/v1/ping` 返回 200（应用本身正常）；
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
