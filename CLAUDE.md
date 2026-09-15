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
venv\Scripts\pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml aiohttp deep-translator yt-dlp
venv\Scripts\python app.py --port 7860 --debug
```

The web UI opens at `http://localhost:7860`. The CLI version (`main.py`) also works standalone for interactive search/download.

## Architecture

```
app.py              ← Main Flask app: routes, search aggregation, download pipeline, admin APIs
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
  video_extractor.py ← Video audio extraction (yt-dlp + ffmpeg): parse video URL, extract MP3

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

### Multi-source search (`app.py`)
`SEARCH_SOURCES` is a priority-ordered list of `(name, search_fn, [supported_platforms])`. The `search_all_sources()` function fans out to all matching sources via `ThreadPoolExecutor`, then deduplicates by `(normalized_title, normalized_artist)` while merging missing fields (cover, duration) from lower-priority sources into the first occurrence.

To add a new search source: write a function matching the signature `fn(query, platform, page) -> dict` returning `{"songs": [...], "total": N, "error": ...}`, then add it to `_register_sources()`.

### Audio resolution — strict sequential fallback (`app.py:api_stream_audio` / `api_download`)
Both the stream and download endpoints try audio sources in strict priority order, moving to the next source whenever one fails to deliver playable audio (dead link, expired auth, non-audio body) — an error is returned only when every source fails.

Stream order: `cache` → `direct_url` (Meting link from search results) → `netease_direct` (each quality tier) → `netease_cross` (search by title+artist, each tier) → `myhkw_id` → `myhkw_kw` → `gdstudio`. Download order: `direct_url` → `netease_direct` → `netease_cross` → `myhkw_keyword` → `myhkw_id` → `gdstudio`. Download strategies run up to 2 attempts × 2 rounds.

URLs are cached in `_audio_url_cache` only after a fetch is verified to return real audio (`_looks_like_audio` / `_detect_audio_format` magic-byte sniff); a cached URL that fails is evicted immediately. Never cache an unverified URL — a bad direct link would otherwise poison the cache and block all fallbacks for the song. On success, `analytics.track_download()` records the song/platform/channel used.

### Admin access
Click the "🎵 卡塔音乐" logo in the top-left → password modal. Password is hardcoded in `app.py:ADMIN_PASSWORD = "yan060826"`. The admin dashboard is a full-page overlay injected by `admin.js`. API health checking fires all 8 external API pings in parallel with progressive card updates.

### Visitor tracking (`analytics.py`)
SQLite-based. `track_visit(ip, user_agent)` is called from Flask's `@app.before_request`. Visitors are deduplicated by `hash(ip + ua[:60])`. Stats expose today/month/year unique + total + repeat counts. Downloads track song/artist/platform/channel/success.

Lock discipline (app.py): `_enqueue_visitor` must never call `_flush_visitor_buffer` while holding `_visitor_buffer_lock` — it self-deadlocks the whole server once the buffer hits `_VISITOR_FLUSH_SIZE` (20 visits). The flush runs outside the lock; the lock is an `RLock` as defense. The localhost IP is exempt from `_rate_limit` (the frontend alone fires dozens of requests per page).

### API health monitoring
The `_ping_single()` function in `app.py` tests one external API. The `/api/admin/api-check-one?name=...` endpoint returns a single result; the frontend fires 8 parallel requests and updates cards progressively with "待更新" → "已更新" labels. A configurable interval selector (1min–24h, default 1h) controls auto-refresh in the admin UI.

## Production deployment

```
docker build -t kata-music .
docker run -p 7860:7860 kata-music
```

Set `RENDER=true` environment variable for production mode (disables template auto-reload, uses `/tmp/downloads`). The Dockerfile uses waitress as the WSGI server.
