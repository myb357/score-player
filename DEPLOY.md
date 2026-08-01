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
