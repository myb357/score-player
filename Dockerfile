# 谱子播放器 —— 可用于 Render / Railway 等平台的容器镜像
# 该镜像内置 ffmpeg，支持视频提取音轨与音频剪辑。
FROM python:3.11-slim

# 安装系统 ffmpeg（比 imageio-ffmpeg 更稳定，二者任一可用即可）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# APK 随仓库 static/android/score-player.apk 一起进入镜像，避免构建时重新下载旧 Release 覆盖新版 APK。

# 临时目录：仅存放 ffmpeg 转码临时文件（数据已外置到 Supabase + Backblaze B2，无需持久磁盘）
ENV SCORE_DATA_DIR=/tmp/score_app_data
RUN mkdir -p /tmp/score_app_data

EXPOSE 8000

# 平台通过 $PORT 注入端口（Render/Railway 均支持）；本地默认 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
