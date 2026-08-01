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

# 数据目录（SQLite + 上传的图片/音频）。在 Render/Railway 上挂载持久磁盘到该路径即可实现持久化。
ENV SCORE_DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# 平台通过 $PORT 注入端口（Render/Railway 均支持）；本地默认 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
