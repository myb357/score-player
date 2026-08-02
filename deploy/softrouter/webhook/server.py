#!/usr/bin/env python3
# ============================================================================
# score-player 软路由 Webhook 自动部署接收器
#
#   GitHub Actions 构建并推送镜像完成后，通过 Cloudflare Tunnel 暴露的
#   https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>
#   POST 请求触发本服务，本服务在软路由本地执行：
#       1) docker pull <阿里云 ACR 镜像>；失败时降级 docker pull <GHCR 镜像>
#       2) docker tag <实际拉到的镜像> <compose 文件 image 标签>
#       3) docker-compose ... up -d --no-deps score-player
#   从而完成软路由上 sp-app 应用容器的主动更新。
#
# 设计约束：
#   - 仅使用 Python 标准库（http.server），不依赖任何第三方包。
#   - token 从环境变量 WEBHOOK_TOKEN 读取，query param token 校验不通过返回 401。
#   - 所有日志打印到 stdout，便于 `docker logs sp-webhook` 排查。
# ============================================================================
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---- 配置 ----
HOST = "0.0.0.0"
PORT = 9003
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# 与 CI / migrate.sh / docker-compose.yml 保持一致：compose 固定使用阿里云 ACR 镜像标签。
# 部署时优先拉阿里云 ACR；若不可达，再降级拉 GHCR，并 tag 成 compose 需要的标签。
COMPOSE_IMAGE = "crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest"
PRIMARY_IMAGE = COMPOSE_IMAGE
FALLBACK_IMAGE = "ghcr.io/myb357/score-player:latest"
COMPOSE_FILE = "/root/score-player/deploy/softrouter/docker-compose.yml"
COMPOSE_SERVICE = "score-player"


def log(msg):
    """统一打印到 stdout 并立即 flush，保证 docker logs 实时可见。"""
    print(f"[webhook] {msg}", flush=True)


def compose_base_cmd():
    """
    探测可用的 compose 命令：
      - 优先使用独立二进制 `docker-compose`（v1，软路由 iStoreOS 默认）；
      - 回退到 Docker CLI 插件 `docker compose`（v2）。
    宿主机二进制通过 docker-compose.yml volumes 直接挂载进容器，无需安装。
    """
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def run_cmd(cmd, timeout=600):
    """执行命令，返回 CompletedProcess；异常由调用方处理。"""
    printable = " ".join(cmd)
    log(f"执行: {printable}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.stdout:
        log(proc.stdout.strip())
    if proc.stderr:
        log(proc.stderr.strip())
    return proc


def pull_image_with_fallback():
    """优先拉阿里云 ACR；失败后降级拉 GHCR，并统一 tag 为 compose image。"""
    try:
        log("使用阿里云 ACR 拉取应用镜像")
        proc = run_cmd(["docker", "pull", PRIMARY_IMAGE])
        if proc.returncode == 0:
            pulled_image = PRIMARY_IMAGE
            log("使用阿里云 ACR 镜像部署")
        else:
            log(f"阿里云失败，降级 ghcr.io：docker pull 返回 {proc.returncode}")
            proc = run_cmd(["docker", "pull", FALLBACK_IMAGE])
            if proc.returncode != 0:
                return False, f"fallback pull exited {proc.returncode}: {FALLBACK_IMAGE}", ""
            pulled_image = FALLBACK_IMAGE
            log("阿里云失败，降级 ghcr.io 镜像部署")
    except Exception as e:  # noqa: BLE001
        log(f"阿里云失败，降级 ghcr.io：{e}")
        try:
            proc = run_cmd(["docker", "pull", FALLBACK_IMAGE])
        except Exception as fallback_error:  # noqa: BLE001
            return False, f"fallback pull failed: {fallback_error}", ""
        if proc.returncode != 0:
            return False, f"fallback pull exited {proc.returncode}: {FALLBACK_IMAGE}", ""
        pulled_image = FALLBACK_IMAGE
        log("阿里云失败，降级 ghcr.io 镜像部署")

    # 无论实际拉到哪个镜像，都打上 compose 文件 image 字段的标签，确保 compose 使用最新镜像。
    proc = run_cmd(["docker", "tag", pulled_image, COMPOSE_IMAGE])
    if proc.returncode != 0:
        return False, f"docker tag exited {proc.returncode}: {pulled_image} -> {COMPOSE_IMAGE}", pulled_image
    return True, "", pulled_image


def run_deploy():
    """执行拉取镜像 + 重启 sp-app，返回 (ok: bool, detail: str)。"""
    ok, detail, pulled_image = pull_image_with_fallback()
    if not ok:
        return False, detail

    cmd = compose_base_cmd() + ["-f", COMPOSE_FILE, "up", "-d", "--no-deps", COMPOSE_SERVICE]
    try:
        proc = run_cmd(cmd)
    except Exception as e:  # noqa: BLE001
        printable = " ".join(cmd)
        log(f"命令异常: {printable} -> {e}")
        return False, f"command failed: {printable}: {e}"
    if proc.returncode != 0:
        printable = " ".join(cmd)
        return False, f"command exited {proc.returncode}: {printable}"
    return True, f"pulled={pulled_image}; compose_image={COMPOSE_IMAGE}; {' '.join(cmd)}"


class DeployHandler(BaseHTTPRequestHandler):
    server_version = "score-player-webhook/1.0"

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 复用统一日志格式，避免 BaseHTTPRequestHandler 默认写 stderr
        log("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "message": "webhook alive"})
            return
        self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/deploy":
            self._json(404, {"status": "error", "message": "not found"})
            return

        # 读取并丢弃请求体（CI 可能带 JSON body），避免连接挂起
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:  # noqa: BLE001
                pass

        token = parse_qs(parsed.query).get("token", [""])[0]
        if not WEBHOOK_TOKEN:
            log("ERROR: 环境变量 WEBHOOK_TOKEN 未配置，拒绝所有部署请求")
            self._json(500, {"status": "error", "message": "WEBHOOK_TOKEN not configured"})
            return
        if token != WEBHOOK_TOKEN:
            log("拒绝: token 校验失败")
            self._json(401, {"status": "error", "message": "invalid token"})
            return

        log("token 校验通过，开始部署 ...")
        ok, detail = run_deploy()
        if ok:
            log("部署触发成功")
            self._json(200, {"status": "ok", "message": "deploy triggered"})
        else:
            log(f"部署失败: {detail}")
            self._json(500, {"status": "error", "message": detail})


def main():
    if not WEBHOOK_TOKEN:
        log("WARN: 启动时未检测到 WEBHOOK_TOKEN 环境变量，部署请求会被拒绝（500）")
    log(f"docker: {shutil.which('docker') or '未找到'}")
    log(f"docker-compose: {shutil.which('docker-compose') or '未找到'}")
    server = ThreadingHTTPServer((HOST, PORT), DeployHandler)
    log(f"webhook 服务已启动，监听 {HOST}:{PORT}，路由 POST /deploy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，正在关闭 ...")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
