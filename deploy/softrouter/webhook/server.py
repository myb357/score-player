#!/usr/bin/env python3
# ============================================================================
# score-player 软路由 Webhook 自动部署接收器
#
#   GitHub Actions 构建并推送镜像完成后，通过 Cloudflare Tunnel 暴露的
#   https://webhook.scoreplayer-myb.top/deploy?token=<WEBHOOK_TOKEN>
#   POST 请求触发本服务，本服务在软路由本地执行：
#       1) docker pull <阿里云 ACR 镜像>
#       2) docker-compose ... up -d --no-deps sp-app
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

# 与 CI / migrate.sh 保持一致：应用镜像主来源为阿里云 ACR
IMAGE = "crpi-rd0vl6t3c1p11agm.cn-beijing.personal.cr.aliyuncs.com/myb357/score-player:latest"
COMPOSE_FILE = "/root/score-player/deploy/softrouter/docker-compose.yml"
COMPOSE_SERVICE = "sp-app"


def log(msg):
    """统一打印到 stdout 并立即 flush，保证 docker logs 实时可见。"""
    print(f"[webhook] {msg}", flush=True)


def compose_base_cmd():
    """
    探测可用的 compose 命令：
      - 优先使用独立二进制 `docker-compose`（v1）；
      - 回退到 Docker CLI 插件 `docker compose`（v2）。
    这样无论软路由上装的是 v1 还是 v2 都能正常执行。
    """
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def ensure_docker_tooling():
    """
    python:3.11-alpine 基础镜像不自带 docker / docker-compose，
    这里在服务启动时尽力通过 apk 安装 docker-cli 及其 compose 插件。
    失败不阻断服务启动（可能镜像已内置或网络暂时不可用），仅记录日志。
    """
    if shutil.which("docker") and (shutil.which("docker-compose") or shutil.which("docker")):
        # docker 已存在即认为满足基本条件（compose 由 compose_base_cmd 再兜底）
        if shutil.which("docker-compose") or _docker_compose_plugin_ok():
            return
    if not shutil.which("apk"):
        log("WARN: 未检测到 apk，且缺少 docker 工具链，请确保运行环境已内置 docker/docker-compose")
        return
    try:
        log("正在通过 apk 安装 docker-cli / docker-cli-compose ...")
        subprocess.run(
            ["apk", "add", "--no-cache", "docker-cli", "docker-cli-compose"],
            check=True,
            capture_output=True,
            text=True,
        )
        log("docker 工具链安装完成")
    except Exception as e:  # noqa: BLE001 - 最佳努力安装，失败不阻断
        log(f"WARN: 安装 docker 工具链失败（将继续启动，部署时可能报错）：{e}")


def _docker_compose_plugin_ok():
    try:
        subprocess.run(["docker", "compose", "version"], check=True,
                       capture_output=True, text=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_deploy():
    """执行拉取镜像 + 重启 sp-app，返回 (ok: bool, detail: str)。"""
    steps = [
        ["docker", "pull", IMAGE],
        compose_base_cmd() + ["-f", COMPOSE_FILE, "up", "-d", "--no-deps", COMPOSE_SERVICE],
    ]
    outputs = []
    for cmd in steps:
        printable = " ".join(cmd)
        log(f"执行: {printable}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as e:  # noqa: BLE001
            log(f"命令异常: {printable} -> {e}")
            return False, f"command failed: {printable}: {e}"
        if proc.stdout:
            log(proc.stdout.strip())
        if proc.stderr:
            log(proc.stderr.strip())
        if proc.returncode != 0:
            return False, f"command exited {proc.returncode}: {printable}"
        outputs.append(printable)
    return True, "; ".join(outputs)


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
    ensure_docker_tooling()
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
