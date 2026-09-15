# 卡塔音乐 — Hugging Face Spaces (Docker Space) 部署
#
# HF Spaces 对 Docker 的要求：
#   - 应用必须监听 7860 端口（默认，可通过 README.md 的 app_port 覆盖）
#   - 容器以 root 运行；根文件系统是临时的，重启即清空
#   - 持久化数据要写到 /data（挂在持久盘上）
#   - README.md 顶部用 YAML 头声明 sdk: docker
#
# 本地构建/试跑：
#   docker build -t kata-music .
#   docker run -p 7860:7860 kata-music

FROM python:3.12-slim

WORKDIR /app

# 系统依赖：ffmpeg 用于视频/音频提取（若不用「视频转音频」功能可删除以缩小镜像）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 生产模式（关闭模板自动重载、严格 CSRF 等）。
# HF Spaces 容器里会自动带 SPACE_ID 环境变量，代码同样会据此识别为生产环境。
ENV RENDER=true
ENV MD_HOST=0.0.0.0
ENV MD_PORT=7860

# 持久化目录：/data 是 HF Spaces 的持久盘（根目录重启即清空）。
# analytics.db 与自动生成的 secret_key 都写到这里，重启不丢数据、会话不失效。
ENV MD_DATA_DIR=/data/kata-music
RUN mkdir -p /data/kata-music

# 监听 7860（HF Spaces 默认端口）
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health')" || exit 1

CMD ["waitress-serve", "--port=7860", "--threads=8", "wsgi:app"]
