# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

卡塔音乐 (Kata Music) is a Flask-based Chinese music search & download web app. It aggregates results from multiple music platforms (NetEase, Kugou, Kuwo) through third-party proxy APIs, streams MP3/FLAC downloads, handles NCM decryption, translates LRC lyrics, and includes an admin analytics dashboard.

## Start / develop

```bash
# First run — creates venv on D: drive and installs deps
双击 启动卡塔音乐.bat

# Or manually:
python -m venv venv
venv\Scripts\pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml aiohttp deep-translator
venv\Scripts\python web.py --port 5000 --debug
```

The web UI opens at `http://localhost:5000`. The CLI version (`main.py`) also works standalone for interactive search/download.

## Architecture

```
web.py              ← Main Flask app: routes, search aggregation, download pipeline, admin APIs
main.py             ← CLI entry point (interactive TUI + argparse)
config.py           ← Environment-variable config (MD_HOST, MD_PORT, etc.)
analytics.py        ← SQLite visitor/download tracking + stats queries (admin backend)
wsgi.py             ← WSGI entry point for production (waitress/gunicorn)

core/
  utils.py          ← Logging, filename sanitization, duration formatting
  platform_base.py  ← Abstract base classes (SongInfo, SearchResult, BasePlatform)
  downloader.py     ← DownloadManager: async download with progress

platforms/
  netease/          ← NetEase Cloud Music API client (search, detail, URL, playlists, NCM decrypt)
  myhkw_api.py      ← myhkw.cn proxy: search, audio URL resolution, lyrics
  gdstudio_api.py   ← GDStudio multi-platform search + URL/lyrics/cover resolution
  tonzhon_api.py    ← Legacy Tonzhon fallback (largely dead, kept for reference)

static/
  css/style.css     ← Main frontend styles (dark theme)
  css/admin.css     ← Admin dashboard styles (glassmorphism cards, responsive)
  js/app.js         ← Frontend app: search UI, player, downloads, lyrics modal
  js/admin.js       ← Admin dashboard: password gate, stats, API health checks, charts

templates/
  index.html        ← Single-page app template (frontend + admin overlay)

data/               ← Runtime: analytics.db (SQLite), tracked by analytics.py
```

## Key patterns

### Multi-source search (`web.py`)
`SEARCH_SOURCES` is a priority-ordered list of `(name, search_fn, [supported_platforms])`. The `search_all_sources()` function fans out to all matching sources via `ThreadPoolExecutor`, then deduplicates by `(normalized_title, normalized_artist)` while merging missing fields (cover, duration) from lower-priority sources into the first occurrence.

To add a new search source: write a function matching the signature `fn(query, platform, page) -> dict` returning `{"songs": [...], "total": N, "error": ...}`, then add it to `_register_sources()`.

### Download pipeline (`web.py:api_download`)
The download endpoint tries strategies in order: `myhkw_cached` → `myhkw_by_id` → `myhkw_keyword` → `netease_direct`. Each strategy runs up to 2 attempts, and the whole list loops up to 2 rounds. On success, `analytics.track_download()` records the song/platform/channel used.

### Admin access
Click the "🎵 卡塔音乐" logo in the top-left → password modal. Password is hardcoded in `web.py:ADMIN_PASSWORD = "yan060826"`. The admin dashboard is a full-page overlay injected by `admin.js`. API health checking fires all 8 external API pings in parallel with progressive card updates.

### Visitor tracking (`analytics.py`)
SQLite-based. `track_visit(ip, user_agent)` is called from Flask's `@app.before_request`. Visitors are deduplicated by `hash(ip + ua[:60])`. Stats expose today/month/year unique + total + repeat counts. Downloads track song/artist/platform/channel/success.

### API health monitoring
The `_ping_single()` function in `web.py` tests one external API. The `/api/admin/api-check-one?name=...` endpoint returns a single result; the frontend fires 8 parallel requests and updates cards progressively with "待更新" → "已更新" labels. A configurable interval selector (1min–24h, default 1h) controls auto-refresh in the admin UI.

## Production deployment

```
docker build -t kata-music .
docker run -p 5000:5000 kata-music
```

Set `RENDER=true` environment variable for production mode (disables template auto-reload, uses `/tmp/downloads`). The Dockerfile uses waitress as the WSGI server.
