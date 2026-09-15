---
title: 卡塔音乐
emoji: 🎵
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 卡塔音乐（Kata Music）

聚合网易云 / QQ 音乐 / 酷狗 / 酷我 / 咪咕等多平台的音乐搜索与下载 Web 应用（Flask + Waitress）。

## 部署到 Hugging Face Spaces

1. 在 [huggingface.co/new-space](https://huggingface.co/new-space) 新建 Space，SDK 选 **Docker**（或直接 push 本仓库，README 顶部的 `sdk: docker` 会自动识别）。
2. 上传 / push 代码后，Space 会自动 `docker build` 并运行，监听 **7860** 端口。
3. （可选）在 Space 的 **Settings → Variables and secrets** 里添加环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MD_SECRET_KEY` | Flask 会话加密密钥。**建议设置**为一段随机字符串；不设置时应用会首次运行自动生成并持久化到 `/data`。 | 自动生成 |
| `MD_ADMIN_PASSWORD` | 覆盖内置后台密码（明文，建议仅 HTTPS 使用） | 内置密码 |
| `MD_RATE_LIMIT` | 设为 `false` 关闭限流 | `true` |
| `MD_RATE_LIMIT_RPM` | 每 IP 每分钟请求上限 | `60` |
| `MD_TRANSLATION` | 设为 `false` 关闭歌词翻译 | `true` |

> 后台入口：点击页面左上角「🎵 卡塔音乐」Logo，输入密码进入管理面板。

## 数据持久化

- 运行时数据（`analytics.db` 访客/下载统计、自动生成的 `secret_key`）写入 **`/data/kata-music`**（HF Spaces 持久盘），容器重启 / 重新部署不丢失。
- 根文件系统是临时的，请勿在其中存数据。

## 本地运行

```bash
# Windows
双击 启动卡塔音乐.bat

# 或手动
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python app.py --port 7860 --debug
```

本地访问 `http://localhost:7860`。

## Docker 本地试跑

```bash
docker build -t kata-music .
docker run -p 7860:7860 kata-music
```
