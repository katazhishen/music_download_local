# 卡塔音乐 — Docker 部署
# Build:  docker build -t kata-music .
# Run:    docker run -p 5000:5000 kata-music

FROM python:3.12-slim

WORKDIR /app

# Install system deps if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MD_HOST=0.0.0.0
ENV MD_PORT=5000

EXPOSE 5000

# Use waitress for production on any platform (gunicorn is Linux-only)
RUN pip install --no-cache-dir waitress
CMD ["waitress-serve", "--port=5000", "wsgi:app"]
