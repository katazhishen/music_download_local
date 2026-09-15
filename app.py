#!/usr/bin/env python3
"""
Music Downloader — Web UI with multi-platform support.
Search powered by myhkw.cn + NetEase direct API + multiple fallbacks.
Audio download via myhkw.cn proxy (replaces dead tonzhon.whamon.com).

Usage:
    pip install flask requests
    python app.py
    # Open http://127.0.0.1:7860
"""

import os, sys, json, tempfile, time, threading, secrets, io, zipfile, re, hashlib, hmac, itertools
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

try:
    import requests as req
    from flask import Flask, render_template, request, jsonify, send_file, Response

    from core.utils import log, sanitize_filename, build_filename
    from platforms.netease import NeteaseAPI, decrypt_ncm, parse_netease_url
    import analytics  # visitor + download tracking
    from platforms.myhkw_api import (
        resolve_song_url,
        resolve_song_url_raw,
        resolve_song_by_keyword,
        get_lyrics as myhkw_lyrics,
        search_myhkw,
    )
    from platforms.gdstudio_api import (
        search_gdstudio,
        get_song_url as gdstudio_get_url,
        get_lyrics as gdstudio_lyrics,
        get_cover_url as gdstudio_cover,
    )
    # Legacy tonzhon fallback (dead, kept for reference)
    try:
        from platforms.tonzhon_api import resolve_song_url as tonzhon_resolve
        from platforms.tonzhon_api import get_lyrics as tonzhon_lyrics
    except ImportError:
        tonzhon_resolve = None
        tonzhon_lyrics = None

    # Video audio extraction (yt-dlp based)
    try:
        from platforms.video_extractor import get_extractor, detect_platform, _HAS_YTDLP as _HAS_YTDLP
    except ImportError:
        get_extractor = None
        detect_platform = None
        _HAS_YTDLP = False
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml aiohttp")
    sys.exit(1)

# Optional: translation support for LRC lyrics
try:
    from deep_translator import GoogleTranslator
    _HAS_TRANSLATOR = True
except ImportError:
    _HAS_TRANSLATOR = False
    GoogleTranslator = None

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
from config import resolve_data_dir

# Production mode: disables template auto-reload, strict CSRF, etc.
# Detected via RENDER=true (Render), MD_PRODUCTION=true, or Hugging Face
# Spaces (SPACE_ID env var is always set inside a Space's container).
IS_PRODUCTION = (
    os.environ.get("RENDER", "").lower() == "true"
    or os.environ.get("MD_PRODUCTION", "").lower() in ("1", "true", "yes")
    or "SPACE_ID" in os.environ
)


def _load_or_create_secret_key() -> str:
    """Return a stable Flask session secret key.

    Priority:
      1. ``MD_SECRET_KEY`` env var (set it in Hugging Face Space settings →
         Variables and secrets for a fully fixed key).
      2. Persisted ``<data-dir>/secret_key`` file — auto-generated once so
         sessions survive container restarts when the env var isn't set.
      3. In-memory random key (last resort; sessions reset on restart).
    """
    env = os.environ.get("MD_SECRET_KEY", "").strip()
    if env:
        return env
    key_file = resolve_data_dir() / "secret_key"
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = os.urandom(24).hex()
        key_file.write_text(key, encoding="utf-8")
        return key
    except Exception:
        return os.urandom(24).hex()


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = not IS_PRODUCTION

api = NeteaseAPI()

# Replace the bare ``requests`` module with a shared, connection-pooled session
# so every upstream API call reuses TCP connections and retries transient errors
# instead of opening a fresh connection each time.
_session = req.Session()
try:
    _retry = req.adapters.Retry(
        total=2, connect=2, read=1, backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
except TypeError:
    # Older urllib3 without ``allowed_methods`` — POST simply won't be retried.
    _retry = req.adapters.Retry(
        total=2, connect=2, read=1, backoff_factor=0.3,
        status_forcelist=(500, 502, 503, 504),
    )
_adapter = req.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=40, max_retries=_retry)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)
_session.utils = req.utils  # preserve module-level helpers like req.utils.quote
req = _session

# Temp directory for transient scratch files (NCM decrypt, yt-dlp extraction).
# All downloads stream directly to the browser — nothing persists on disk.
_TMP_DIR = Path(tempfile.gettempdir()) / "kata-music"
_TMP_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Site pause / maintenance mode
# ---------------------------------------------------------------------------
_site_paused: bool = False
_site_pause_time: float = 0.0
_site_pause_pid: str = ""  # session-level id so only admin who paused can resume

# ---------------------------------------------------------------------------
# Server-side in-memory caches (for performance)
# ---------------------------------------------------------------------------
# Search result cache: key = "query|platform|page" -> (timestamp, data)
_search_cache: dict[str, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL = 300  # 5 minutes

# Audio URL cache: key = "platform|song_id" -> (timestamp, url, quality)
_audio_url_cache: dict[str, tuple[float, str, str]] = {}
_AUDIO_URL_CACHE_TTL = 600  # 10 minutes

# Visitor tracking buffer: batch writes to reduce SQLite contention
_visitor_buffer: list[tuple[str, str]] = []
_visitor_buffer_lock = threading.RLock()
_VISITOR_FLUSH_INTERVAL = 5  # flush every 5 seconds
_VISITOR_FLUSH_SIZE = 20  # or when buffer reaches this size
_visitor_last_flush: float = time.time()
_visitor_flush_timer: threading.Timer | None = None


# ---------------------------------------------------------------------------
# "Now playing" tracker — in-memory TTL map of who is currently playing what.
# Key: "platform|song_id" -> {visitor_id: last_heartbeat_ts}
# ---------------------------------------------------------------------------
_now_playing: dict[str, dict[str, float]] = {}
_now_playing_lock = threading.Lock()
_NOW_PLAYING_TTL = 45  # seconds without a heartbeat before a listener expires


def _now_playing_key(platform: str, song_id: str) -> str:
    return f"{platform}|{song_id}"


def _prune_now_playing(now: float | None = None):
    """Drop listeners whose heartbeat has expired."""
    now = now if now is not None else time.time()
    with _now_playing_lock:
        for key in list(_now_playing.keys()):
            entries = _now_playing[key]
            for vid in list(entries.keys()):
                if now - entries[vid] > _NOW_PLAYING_TTL:
                    del entries[vid]
            if not entries:
                del _now_playing[key]


def report_play(platform: str, song_id: str, visitor_id: str, action: str):
    """Register a play/pause/heartbeat event for a song.

    A visitor counts as "playing" a song if they've sent a heartbeat within
    the TTL window. On play/heartbeat the timestamp is refreshed; on
    pause/stop/ended the visitor is removed from that song.
    """
    if not song_id:
        return
    key = _now_playing_key(platform, song_id)
    now = time.time()
    with _now_playing_lock:
        if action in ("pause", "stop", "ended"):
            entries = _now_playing.get(key)
            if entries:
                entries.pop(visitor_id, None)
                if not entries:
                    _now_playing.pop(key, None)
            return
        # play / heartbeat — a visitor only plays one song at a time,
        # so remove them from any other song they were playing.
        for other_key, entries in list(_now_playing.items()):
            if other_key != key:
                entries.pop(visitor_id, None)
                if not entries:
                    _now_playing.pop(other_key, None)
        _now_playing.setdefault(key, {})[visitor_id] = now


def now_playing_count(platform: str, song_id: str) -> int:
    """Number of distinct visitors currently playing this song (within TTL)."""
    _prune_now_playing()
    with _now_playing_lock:
        return len(_now_playing.get(_now_playing_key(platform, song_id), {}))


def _flush_visitor_buffer():
    """Flush buffered visitor records to the database."""
    global _visitor_last_flush
    with _visitor_buffer_lock:
        if not _visitor_buffer:
            return
        batch = _visitor_buffer[:]
        _visitor_buffer.clear()
    for ip, ua in batch:
        try:
            analytics.track_visit(ip, ua)
        except Exception:
            pass
    _visitor_last_flush = time.time()


def _enqueue_visitor(ip: str, ua: str):
    """Buffer a visitor record; flush if batch is large enough.

    The flush runs outside the lock — flushing while still holding the
    buffer lock deadlocks (non-reentrant acquire inside the same thread).
    """
    with _visitor_buffer_lock:
        _visitor_buffer.append((ip, ua))
        should_flush = len(_visitor_buffer) >= _VISITOR_FLUSH_SIZE
    if should_flush:
        _flush_visitor_buffer()


def _cleanup_search_cache():
    """Evict expired search cache entries."""
    now = time.time()
    stale = [k for k, (ts, _) in _search_cache.items() if now - ts > _SEARCH_CACHE_TTL]
    for k in stale:
        del _search_cache[k]


def _cleanup_audio_url_cache():
    """Evict expired audio URL cache entries."""
    now = time.time()
    stale = [k for k, (ts, _, _) in _audio_url_cache.items() if now - ts > _AUDIO_URL_CACHE_TTL]
    for k in stale:
        del _audio_url_cache[k]


def _is_site_paused() -> bool:
    """Check if the site is currently in paused/maintenance mode."""
    return _site_paused


# ---------------------------------------------------------------------------
# Console warning — "DO NOT CLOSE THIS WINDOW"
# ---------------------------------------------------------------------------
def _enable_vt_processing():
    """Enable ANSI virtual terminal processing on Windows (Win10 1511+)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VT = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        if not (mode.value & ENABLE_VT):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


def _print_warning_banner():
    """Show a colourful scrolling-then-static warning so users don't close the
    console window by mistake.

    The warning animates for ~3 seconds (rainbow colour cycle) then settles
    into a static red bold banner.  A daemon thread runs the animation so the
    server can start immediately.
    """
    if not sys.stdout.isatty():
        return

    _enable_vt_processing()

    # Fallback: print a plain banner if threading isn't viable
    try:
        _print_animated_warning()
    except Exception:
        _print_static_warning()


def _print_static_warning():
    """Static red bold warning — works even without ANSI (text degrades)."""
    RED = "\033[1;31m"
    RESET = "\033[0m"
    line = "=" * 50
    sys.stdout.write(
        f"\n{RED}{line}{RESET}\n"
        f"{RED}  🈲！！！运行时请勿关闭此窗口！！！🈲  {RESET}\n"
        f"{RED}  🈲  DO NOT CLOSE THIS WINDOW!  🈲  {RESET}\n"
        f"{RED}{line}{RESET}\n\n"
    )
    sys.stdout.flush()


def _print_animated_warning():
    """Rainbow colour-cycle for 3 s, then static red banner."""
    # 256-color palette indices — warm → cool → warm loop
    COLORS = [196, 202, 208, 214, 220, 226, 190, 154, 118, 82,
              46, 47, 48, 49, 50, 51, 45, 39, 33, 27, 21, 20, 19, 18, 17]
    TEXT_CN = "🈲！！！运行时请勿关闭此窗口！！！🈲"
    TEXT_EN = "🈲  DO NOT CLOSE THIS WINDOW!  🈲"
    BORDER = "=" * 50

    RED = "\033[1;31m"
    RESET = "\033[0m"
    LINES = 3

    stop = threading.Event()

    def _cycle():
        i = 0
        while not stop.is_set():
            c = COLORS[i % len(COLORS)]
            code = f"\033[1;38;5;{c}m"
            sys.stdout.write(
                f"\r{code}{BORDER}{RESET}\n"
                f"{code}  {TEXT_CN}  {RESET}\n"
                f"{code}  {TEXT_EN}  {RESET}"
            )
            sys.stdout.flush()
            time.sleep(0.12)
            if i > 0:
                sys.stdout.write(f"\033[{LINES}A")
            i += 1

    t = threading.Thread(target=_cycle, daemon=True)
    t.start()
    time.sleep(3)
    stop.set()
    t.join(timeout=0.5)

    # Clear cycling lines and print static warning
    sys.stdout.write("\r\033[K" * LINES)
    sys.stdout.write(f"\033[{LINES}A")
    sys.stdout.write(
        f"\r{RED}{BORDER}{RESET}\n"
        f"{RED}  {TEXT_CN}  {RESET}\n"
        f"{RED}  {TEXT_EN}  {RESET}\n\n"
    )
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Content-Disposition helper — safe Unicode filenames for HTTP
# ---------------------------------------------------------------------------
def _make_content_disp(filename_full: str, ext: str) -> str:
    """Build Content-Disposition header value with RFC 5987 Unicode support.

    ``filename*=UTF-8''...`` carries the real Unicode name (all browsers).
    ``filename="..."`` is the ASCII fallback for ancient clients.

    The ASCII fallback uses NFKD normalization so accented Latin chars survive
    (é→e, ñ→n, ü→u). Pure CJK chars are stripped — the browser MUST use
    ``filename*=`` to get the correct name.
    """
    import unicodedata, re
    from urllib.parse import quote

    full = f"{filename_full}.{ext}"

    # ASCII fallback: NFKD decompose accented chars (é→e, ñ→n, ü→u),
    # then strip remaining non-ASCII (CJK, Cyrillic, etc.)
    nfkd = unicodedata.normalize("NFKD", full)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    # Remove chars unsafe for filenames
    ascii_name = ascii_name.replace('"', "").replace("'", "").strip()
    # Collapse whitespace
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip()
    # Remove leading/trailing punctuation/dashes from stripped CJK
    ascii_name = ascii_name.strip(",-./ ")
    # If nothing meaningful remains, use "song"
    if len(ascii_name) < 2:
        ascii_name = "song"

    return (
        f"attachment; "
        f"filename*=UTF-8''{quote(full)}; "
        f'filename="{ascii_name}"'
    )

# ---------------------------------------------------------------------------
# Supported platforms
PLATFORMS = {
    "netease":  {"name": "网易云",   "icon": "🎵"},
    "qq":       {"name": "QQ音乐",   "icon": "🐧"},
    "kugou":    {"name": "酷狗",     "icon": "🐶"},
    "kuwo":     {"name": "酷我",     "icon": "🎤"},
    "migu":     {"name": "咪咕音乐", "icon": "📻"},
}


def _search_xmsj_like(base_url: str, source_name: str, query: str, platform: str = "netease", page: int = 1) -> dict:
    """Generic search for xmsj-based sites (maicong/music project).
    Always queries netease internally since other platforms may not be supported."""
    from urllib.parse import urljoin
    search_type = "netease"  # xmsj-like sites work best with netease
    try:
        resp = req.post(
            base_url,
            data={"input": query, "filter": "name", "type": search_type, "page": page},
            headers={
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": base_url,
            },
            timeout=20,
        )
        data = resp.json()
        if data.get("code") == 200:
            songs = []
            for item in data.get("data", []):
                # Handle both xmsj format (title/author) and myhkw format (name/artist)
                title = item.get("title") or item.get("name", "Unknown")
                artist = item.get("author") or item.get("artist", "Unknown")
                artist = artist.replace("/", ", ")
                # Resolve cover URL (myhkw returns relative proxy path like api.php?get=pic&...)
                cover_raw = item.get("pic") or item.get("cover", "")
                if cover_raw and cover_raw.startswith("api.php"):
                    cover_raw = urljoin(base_url, cover_raw)
                songs.append({
                    "id": str(item.get("songid", "")),
                    "title": title,
                    "artist": artist,
                    "cover": cover_raw,
                    "lyric": item.get("lrc", ""),
                    "url": item.get("url", ""),
                    "link": item.get("link", ""),
                    "platform": platform,
                    "platform_name": PLATFORMS.get(platform, {}).get("name", platform),
                    "source": source_name,
                })
            return {"songs": songs, "total": len(songs), "error": None}
        return {"songs": [], "total": 0, "error": data.get("error", "Unknown error")}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_myhkw(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via s.myhkw.cn (明月浩空音乐)."""
    return _search_xmsj_like("http://s.myhkw.cn/", "myhkw", query, platform, page)


def search_xiageba(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via xiageba.liumingye.cn (下歌吧) — Nuxt-based music site."""
    try:
        resp = req.get(
            "https://xiageba.liumingye.cn/api/music/search",
            params={"q": query, "page": page, "pageSize": 20},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://xiageba.liumingye.cn/"},
            timeout=15,
        )
        data = resp.json()
        songs = []
        for item in data.get("data", []):
            songs.append({
                "id": item.get("id", ""),
                "title": item.get("title", "Unknown"),
                "artist": item.get("artist", "Unknown"),
                "cover": item.get("cover", ""),
                "lyric": "",
                "url": "",
                "link": f"https://xiageba.liumingye.cn/#/song/{item.get('id','')}",
                "platform": "xiageba",
                "platform_name": "下歌吧",
                "source": "xiageba",
            })
        return {"songs": songs, "total": data.get("total", len(songs)), "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_luckxz(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via luckxz.com by scraping search results page."""
    if platform != "netease":
        return {"songs": [], "total": 0, "error": "luckxz only supports generic search"}
    try:
        from bs4 import BeautifulSoup
        resp = req.post(
            "https://luckxz.com/index/search/",
            data={"keyword": query, "action": "1"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://luckxz.com/"},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"songs": [], "total": 0, "error": f"HTTP {resp.status_code}"}

        soup = BeautifulSoup(resp.text, "lxml")
        songs = []
        # luckxz results are in h2 tags: 《title》-artist [format]
        import re
        for h2 in soup.select("h2")[:20]:
            text = h2.get_text(strip=True)
            # Pattern: 《songname》-artist [WAV/MP3/FLAC]
            match = re.match(r'[《「](.+?)[》」]\s*-\s*(.+?)\s*\[', text)
            if not match:
                continue
            title = match.group(1).strip()
            artist = match.group(2).strip()
            # Also try to find download link
            link_el = soup.select_one(f'a[href*="{title[:4]}"]') if len(title) >= 4 else None
            link = link_el.get("href", "") if link_el else ""
            if link and not link.startswith("http"):
                link = "https://luckxz.com" + link

            songs.append({
                "id": link.split("/")[-1].replace(".html", "") if link else f"lx{abs(hash(title))%100000}",
                "title": title,
                "artist": artist,
                "cover": "",
                "lyric": "",
                "url": link,
                "link": link,
                "platform": "netease",
                "platform_name": "幸运小猪",
                "source": "luckxz",
            })
        return {"songs": songs, "total": len(songs), "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_qqmusic(query: str, platform: str = "qq", page: int = 1) -> dict:
    """Search via QQ Music official API.

    Heat data: batch-fetches comment counts from QQ Music's global comment
    API after search.  Each song's comment total serves as the popularity
    indicator for ranking.
    """
    import concurrent.futures

    try:
        resp = req.get(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={
                "t": 0, "aggr": 1, "lossless": 0, "flag_qc": 0,
                "p": page, "n": 20, "w": query,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://y.qq.com/",
            },
            timeout=15,
        )
        raw = resp.text
        # Response is JSONP: callback({...})
        if raw.startswith("callback("):
            raw = raw[9:-1]
        data = json.loads(raw)
        songs = []
        song_ids = []  # numeric IDs for heat fetch
        for item in data.get("data", {}).get("song", {}).get("list", []):
            singer_list = item.get("singer", [])
            artist = ", ".join(s.get("name", "") for s in singer_list) if singer_list else "Unknown"
            albummid = item.get("albummid", "")
            numeric_id = str(item.get("id", "") or item.get("songid", ""))
            song_ids.append(numeric_id)
            songs.append({
                "id": item.get("songmid", ""),
                "title": item.get("songname", "Unknown"),
                "artist": artist,
                "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg" if albummid else "",
                "duration": (item.get("interval") or 0) * 1000,
                "heat": 0,
                "lyric": "",
                "url": "",
                "link": f"https://y.qq.com/n/ryqq/songDetail/{item.get('songmid', '')}",
                "platform": "qq",
                "platform_name": "QQ音乐",
                "source": "qqmusic",
                "_media_mid": item.get("media_mid", ""),
                "_songmid": item.get("songmid", ""),
                "_numeric_id": numeric_id,
            })
        total = data.get("data", {}).get("song", {}).get("totalnum", len(songs))

        # ── Batch-fetch comment counts as heat indicator ──
        if song_ids:
            def _fetch_qq_comment(sid):
                try:
                    r = req.get(
                        "https://c.y.qq.com/base/fcgi-bin/fcg_global_comment_h5.fcg",
                        params={"biztype": 1, "topid": sid, "cmd": 8,
                                "pagenum": 0, "pagesize": 1},
                        headers={"User-Agent": "Mozilla/5.0",
                                 "Referer": "https://y.qq.com/"},
                        timeout=8,
                    )
                    j = r.json()
                    total_comments = (
                        j.get("data", {}).get("comment", {}).get("totalsource", 0)
                        or j.get("data", {}).get("comment", {}).get("total", 0)
                        or j.get("total", 0)
                    )
                    return sid, int(total_comments) if total_comments else 0
                except Exception:
                    return sid, 0

            song_by_numid = {}
            for i, s in enumerate(songs):
                nid = s.get("_numeric_id", "")
                if nid:
                    song_by_numid[nid] = i

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(_fetch_qq_comment, sid): sid for sid in song_ids if sid}
                for fut in concurrent.futures.as_completed(futs, timeout=15):
                    try:
                        sid, count = fut.result()
                        if count > 0 and sid in song_by_numid:
                            songs[song_by_numid[sid]]["heat"] = count
                    except Exception:
                        pass

        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_migu(query: str, platform: str = "migu", page: int = 1) -> dict:
    """Search via Migu Music (咪咕音乐) official API.

    Heat data: tries to extract popularity indicators from the search
    response (topResult flag, etc.) and batch-fetches song detail for
    play counts via Migu's song_info.do endpoint.
    """
    import concurrent.futures

    try:
        resp = req.get(
            "https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do",
            params={
                "text": query,
                "pageNo": page,
                "pageSize": 20,
                "searchSwitch": '{"song":1}',
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://m.music.migu.cn/",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != "000000":
            return {"songs": [], "total": 0, "error": data.get("info", "unknown")}

        songs = []
        need_detail_ids = []  # (copyrightId, contentId) pairs
        for item in data.get("songResultData", {}).get("result", []):
            singers = [s.get("name", "") for s in item.get("singers", [])]
            artist = ", ".join(singers) if singers else "Unknown"
            cover = ""
            album_imgs = item.get("albumImgs") or item.get("imgItems") or []
            if album_imgs:
                cover = album_imgs[0].get("img", "")

            # Try to extract heat from search response fields
            heat = 0
            # topResult flag — featured songs get a base score
            if item.get("topResult"):
                heat = max(heat, 500)
            # Some Migu responses include a numeric score / sort order
            sort_val = item.get("sort") or item.get("score") or 0
            if isinstance(sort_val, (int, float)) and sort_val > 0:
                heat = max(heat, int(sort_val))

            copyright_id = item.get("copyrightId", "")
            content_id = item.get("contentId", "")
            if copyright_id:
                need_detail_ids.append((copyright_id, content_id))

            songs.append({
                "id": content_id or str(item.get("id", "")),
                "title": item.get("name", "Unknown"),
                "artist": artist,
                "cover": cover,
                "duration": 0,
                "heat": heat,
                "lyric": "",
                "url": "",
                "link": f"https://music.migu.cn/v3/music/song/{copyright_id}",
                "platform": "migu",
                "platform_name": "咪咕音乐",
                "source": "migu",
                "_copyright_id": copyright_id,
                "_content_id": content_id,
            })

        # ── Batch-fetch song detail for play counts ──
        if need_detail_ids:
            def _fetch_migu_detail(copyright_id, content_id):
                try:
                    r = req.get(
                        "https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/song_info.do",
                        params={"copyrightId": copyright_id,
                                "contentId": content_id},
                        headers={"User-Agent": "Mozilla/5.0",
                                 "Referer": "https://m.music.migu.cn/"},
                        timeout=8,
                    )
                    j = r.json()
                    if j.get("code") != "000000":
                        return copyright_id, 0
                    detail = j.get("data") or j.get("songData") or {}
                    # Try common play-count field names
                    count = (
                        detail.get("listenCount")
                        or detail.get("playCount")
                        or detail.get("totalListen")
                        or detail.get("listenNum")
                        or 0
                    )
                    return copyright_id, int(count) if count else 0
                except Exception:
                    return copyright_id, 0

            song_by_cpid = {}
            for i, s in enumerate(songs):
                cpid = s.get("_copyright_id", "")
                if cpid:
                    song_by_cpid[cpid] = i

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                futs = {}
                for cpid, ctid in need_detail_ids[:20]:  # limit to 20
                    if cpid and cpid not in futs.values():
                        fut = ex.submit(_fetch_migu_detail, cpid, ctid)
                        futs[fut] = cpid
                for fut in concurrent.futures.as_completed(futs, timeout=15):
                    try:
                        cpid, count = fut.result()
                        if count > 0 and cpid in song_by_cpid:
                            idx = song_by_cpid[cpid]
                            # Use Max so we don't overwrite a higher value from search
                            songs[idx]["heat"] = max(songs[idx]["heat"], count)
                    except Exception:
                        pass

        total = int(data.get("songResultData", {}).get("totalCount", "0"))
        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_kugou(query: str, platform: str = "kugou", page: int = 1) -> dict:
    """Search via Kugou mobile API (works globally).

    Heat data: uses the best available popularity metric from Kugou's
    search response — preferring play/listen count, falling back to
    ownercount (download count).
    """
    try:
        resp = req.get(
            "http://mobilecdn.kugou.com/api/v3/search/song",
            params={"format": "json", "keyword": query, "page": page, "pagesize": 20, "showtype": 1},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        data = resp.json()
        songs = []
        for item in data.get("data", {}).get("info", []):
            # Kugou provides several popularity-related fields — use the
            # highest-value one as the heat score.
            heat = max(
                int(item.get("heat", 0) or 0),             # hot score (if available)
                int(item.get("hot", 0) or 0),              # alternate hot field
                int(item.get("ownercount", 0) or 0),       # download count
                int(item.get("pay_type", 0) or 0),         # 0=free 1/3=paid (not heat, skip low values)
            )
            # filter: pay_type alone isn't meaningful heat
            if heat == int(item.get("pay_type", 0) or 0) and heat < 10:
                heat = int(item.get("ownercount", 0) or 0)

            songs.append({
                "id": item.get("hash", ""),
                "title": item.get("songname", "Unknown"),
                "artist": item.get("singername", "Unknown"),
                "cover": item.get("imgUrl", "") or "",
                "heat": max(heat, 0),
                "lyric": "",
                "url": "",
                "link": f"https://www.kugou.com/song/#hash={item.get('hash','')}",
                "platform": "kugou",
                "platform_name": "酷狗",
                "source": "kugou",
                "filename": item.get("filename", ""),
            })
        total = data.get("data", {}).get("total", len(songs))
        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_direct(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search directly via NetEase API — works for any platform tab
    since most songs exist on NetEase regardless of source preference."""
    try:
        result = api.search_sync(query, page=page, limit=20)
        songs = []
        need_cover_ids = []

        for s in result.songs:
            cover = s.cover_url or ""
            songs.append({
                "id": s.song_id,
                "title": s.title,
                "artist": s.artist,
                "cover": cover,
                "duration": s.duration_ms,  # ms
                "heat": 0,  # filled by detail API below
                "lyric": "",
                "url": "",
                "link": f"https://music.163.com/#/song?id={s.song_id}",
                "platform": "netease",
                "platform_name": "网易云",
                "source": "direct",
            })
            need_cover_ids.append(s.song_id)  # always fetch detail for covers + heat

        # Batch-fetch covers + popularity via song detail API
        if need_cover_ids and len(need_cover_ids) > 0:
            try:
                ids_str = "[" + ",".join(need_cover_ids) + "]"
                detail_resp = req.get(
                    "https://music.163.com/api/song/detail",
                    params={"id": need_cover_ids[0], "ids": ids_str},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://music.163.com/",
                    },
                    timeout=15,
                )
                detail_data = detail_resp.json()
                detail_songs = detail_data.get("songs", [])
                cover_map = {}
                for ds in detail_songs:
                    al = ds.get("album") or ds.get("al") or {}
                    pic = al.get("picUrl", "")
                    if pic:
                        cover_map[str(ds["id"])] = pic
                for sng in songs:
                    if not sng["cover"] and sng["id"] in cover_map:
                        sng["cover"] = cover_map[sng["id"]]
            except Exception:
                pass  # covers will be lazy-fetched by frontend

        # Fetch real comment counts (likes) in parallel for heat ranking
        import concurrent.futures

        def _fetch_comment_count(sid):
            try:
                r = req.get(
                    f"https://music.163.com/api/v1/resource/comments/R_SO_4_{sid}",
                    params={"limit": 0},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
                    timeout=8,
                )
                return sid, r.json().get("total", 0)
            except Exception:
                return sid, 0

        all_ids = [s["id"] for s in songs]
        song_by_id = {s["id"]: s for s in songs}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_fetch_comment_count, sid): sid for sid in all_ids}
            for fut in concurrent.futures.as_completed(futures, timeout=15):
                try:
                    sid, count = fut.result()
                    if sid in song_by_id and count > 0:
                        song_by_id[sid]["heat"] = count
                except Exception:
                    pass

        return {"songs": songs, "total": result.total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_kuwo(query: str, platform: str = "kuwo", page: int = 1) -> dict:
    """Search via Kuwo (酷我音乐) search API."""
    try:
        resp = req.get(
            "http://search.kuwo.cn/r.s",
            params={
                "all": query, "ft": "music",
                "pn": (page - 1) * 20, "rn": 20,
                "rformat": "json", "encoding": "utf8",
            },
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
        )
        raw = resp.content

        # The response is single-quoted JavaScript object notation, not JSON
        text = raw.decode("utf-8", errors="replace")

        # Extract TOTAL
        tm = re.search(r"'TOTAL'\s*:\s*'(\d+)'", text)
        total = int(tm.group(1)) if tm else 0

        # Find abslist array start
        am = re.search(r"'abslist'\s*:\s*\[", text)
        if not am:
            return {"songs": [], "total": 0, "error": None}

        # Extract song objects by tracking brace depth
        start = am.end()
        depth = 0
        obj_start = -1
        song_blocks = []

        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    song_blocks.append(text[obj_start : i + 1])
                    obj_start = -1
            elif ch == "]" and depth == 0:
                break

        songs = []
        for block in song_blocks:
            name_m = re.search(r"'NAME'\s*:\s*'([^']+)'", block)
            artist_m = re.search(r"'ARTIST'\s*:\s*'([^']+)'", block)
            rid_m = re.search(r"'MUSICRID'\s*:\s*'([^']+)'", block)
            dur_m = re.search(r"'DURATION'\s*:\s*'(\d+)'", block)
            album_m = re.search(r"'ALBUM'\s*:\s*'([^']+)'", block)
            playcnt_m = re.search(r"'PLAYCNT'\s*:\s*'(\d+)'", block)

            if not name_m or not rid_m:
                continue

            title = name_m.group(1)
            title = title.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").strip()

            artist = artist_m.group(1) if artist_m else "Unknown"
            artist = artist.replace("&nbsp;", " ").replace("&amp;", "&").replace("\\\\u0026", " & ").strip()

            rid = rid_m.group(1).replace("MUSIC_", "")
            dur_sec = int(dur_m.group(1)) if dur_m and dur_m.group(1).isdigit() else 0
            album = album_m.group(1) if album_m else ""
            album = album.replace("&nbsp;", " ").replace("&amp;", "&").strip()

            heat_val = int(playcnt_m.group(1)) if playcnt_m and playcnt_m.group(1).isdigit() else 0

            songs.append({
                "id": rid,
                "title": title,
                "artist": artist,
                "album": album,
                "duration": dur_sec * 1000,
                "heat": heat_val,
                "cover": "",
                "lyric": "",
                "url": "",
                "link": f"http://www.kuwo.cn/play_detail/{rid}",
                "platform": "kuwo",
                "platform_name": "酷我音乐",
                "source": "kuwo",
            })

        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Catalogue-site search — scrape HTML-based music catalogues
# (tgws.cc, blmp3.cn, htwav.top — all cloud-storage-backed music indices)
# ---------------------------------------------------------------------------

def _scrape_tgws_blmp3(base_url: str, source_name: str, query: str,
                        page: int = 1) -> dict:
    """Scrape tgws.cc / blmp3.cn (same CMS) search results.

    These are PHP-based music catalogue sites with server-rendered HTML.
    Search URL: /search?name=XXX&page=N
    Each result <li> contains: <h2><a> → title [+ format], artistTag → artist.
    Song detail page at /musicInfo/{id}.html provides cloud-storage links.
    """
    from bs4 import BeautifulSoup
    try:
        resp = req.get(
            f"{base_url}/search",
            params={"name": query, "page": page},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"songs": [], "total": 0, "error": f"HTTP {resp.status_code}"}

        soup = BeautifulSoup(resp.text, "lxml")
        songs = []
        total = 0

        # Find result list items — they're <li> inside searchRightDiv
        result_div = soup.select_one(".searchRightDiv")
        if not result_div:
            return {"songs": [], "total": 0, "error": None}

        items = result_div.select("li")
        for item in items:
            h2 = item.find("h2")
            if not h2:
                continue
            link = h2.find("a")
            if not link:
                continue

            # Extract song ID from href: /musicInfo/{id}.html
            href = link.get("href", "")
            song_id = ""
            id_m = re.search(r'/musicInfo/(\d+)\.html', href)
            if id_m:
                song_id = id_m.group(1)

            # Title: strip the [MP3/FLAC] quality suffix
            title = link.get_text(strip=True)
            title = re.sub(r'\s*\[MP3.*?\]\s*$', '', title)
            title = re.sub(r'\s*\[(?:MP3|FLAC|WAV|MP3/FLAC).*?\]\s*$', '', title)
            title = title.replace(" ", " ").strip()

            if not title:
                continue

            # Artist from artistTag
            artist_el = item.select_one(".artistTag, a[href*='search?name=']")
            artist = artist_el.get_text(strip=True) if artist_el else "Unknown"

            # Detail page URL
            detail_url = f"{base_url}{href}" if href.startswith("/") else href

            songs.append({
                "id": song_id or f"{source_name}_{abs(hash(title)) % 100000000}",
                "title": title,
                "artist": artist,
                "cover": "",
                "duration": 0,
                "heat": 0,
                "lyric": "",
                "url": "",
                "link": detail_url,
                "platform": source_name,
                "platform_name": {
                    "tgws": "糖果无损", "blmp3": "百灵无损",
                }.get(source_name, source_name),
                "source": source_name,
            })

        # Extract total from pagination
        pagination = soup.select_one(".pagination")
        if pagination:
            page_links = pagination.select("a")
            if page_links:
                last_page = 1
                for a in page_links:
                    try:
                        p = int(a.get_text(strip=True))
                        if p > last_page:
                            last_page = p
                    except ValueError:
                        continue
                total = last_page * 20  # ~20 results per page

        if not total:
            total = len(songs)

        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def _scrape_htwav(query: str, page: int = 1) -> dict:
    """Scrape htwav.top search results.

    Search URL: /search.html?key=XXX&search=1&page=N
    Results in <ul class="listbox"> <li> with:
      <h2><a href="/detail/{id}.html" title="...">title [MP3] -size</a></h2>
      <small><em>分享时间</em> <em>演唱：artist</em></small>
    """
    from bs4 import BeautifulSoup
    try:
        resp = req.get(
            "https://www.htwav.top/search.html",
            params={"key": query, "search": 1, "page": page},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"songs": [], "total": 0, "error": f"HTTP {resp.status_code}"}

        soup = BeautifulSoup(resp.text, "lxml")
        songs = []
        total = 0

        # Results inside <ul class="listbox">
        listbox = soup.select_one(".listbox")
        if not listbox:
            return {"songs": [], "total": 0, "error": None}

        items = listbox.select("li")
        for item in items:
            h2 = item.find("h2")
            if not h2:
                continue
            link = h2.find("a")
            if not link:
                continue

            href = link.get("href", "")
            song_id = ""
            id_m = re.search(r'/detail/(\d+)\.html', href)
            if id_m:
                song_id = id_m.group(1)

            title = link.get_text(strip=True)
            # Strip format tag and size: "title [MP3] -4.7M"
            title = re.sub(r'\s*\[(?:MP3|FLAC|WAV|MP3/FLAC).*?\].*$', '', title)
            title = title.rstrip(" -0123456789.MKkGgBb").strip()
            if not title:
                continue

            # Artist from <em> 演唱：xxx</em>
            small = item.find("small")
            artist = "Unknown"
            if small:
                ems = small.select("em")
                for em in ems:
                    text = em.get_text(strip=True)
                    if "演唱" in text:
                        artist = text.replace("演唱：", "").replace("演唱:", "").strip()
                        artist = artist.replace("&amp;", "&")
                        break

            detail_url = f"https://www.htwav.top{href}" if href.startswith("/") else href

            songs.append({
                "id": song_id or f"htwav_{abs(hash(title)) % 100000000}",
                "title": title,
                "artist": artist,
                "cover": "",
                "duration": 0,
                "heat": 0,
                "lyric": "",
                "url": "",
                "link": detail_url,
                "platform": "htwav",
                "platform_name": "海豚无损",
                "source": "htwav",
            })

        # Total from pagination
        pagination = soup.select_one(".pagination")
        if pagination:
            page_links = pagination.select("a")
            last_page = 1
            for a in page_links:
                try:
                    p = int(a.get_text(strip=True))
                    if p > last_page:
                        last_page = p
                except ValueError:
                    continue
            total = last_page * 15  # ~15 results per page

        if not total:
            total = len(songs)

        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_tgws(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search tgws.cc (糖果无损音乐网)."""
    return _scrape_tgws_blmp3("https://www.tgws.cc", "tgws", query, page)


def search_blmp3(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search blmp3.cn (百灵无损音乐网)."""
    return _scrape_tgws_blmp3("https://www.blmp3.cn", "blmp3", query, page)


def search_htwav(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search htwav.top (海豚无损音乐网)."""
    return _scrape_htwav(query, page)


def search_meting(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via Meting API (api.i-meto.com) — working search + direct audio URL.

    The ``url`` field is an authenticated, directly-downloadable audio link that
    gets merged into higher-priority deduped results (filling their empty url).
    """
    from platforms.meting_api import meting_search, meting_extract_id
    try:
        raw = meting_search(query, platform, page)
        if not raw:
            return {"songs": [], "total": 0, "error": None}
        songs = []
        for item in raw:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            url = item.get("url") or ""
            pic = item.get("pic") or ""
            sid = meting_extract_id(url)
            if not sid:
                sid = meting_extract_id(pic) or f"mt{abs(hash(title)) % 100000000}"
            songs.append({
                "id": sid,
                "title": title,
                "artist": (item.get("author") or "Unknown").strip(),
                "cover": pic,
                "lyric": "",
                "url": url,  # authenticated direct audio URL
                "link": "",
                "platform": platform,
                "platform_name": PLATFORMS.get(platform, {}).get("name", platform),
                "source": "meting",
                "_lrc_url": item.get("lrc") or "",
            })
        return {"songs": songs, "total": len(songs), "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Multi-source search framework — add new sites here
# ---------------------------------------------------------------------------

# Source: (name, search_fn, platforms_supported)
SEARCH_SOURCES: list[tuple[str, callable, list[str]]] = []

def _register_sources():
    """Register all search sources in priority order. Add new sites here."""
    SEARCH_SOURCES.clear()

    # Source 1: direct NetEase API — best quality (covers, duration)
    SEARCH_SOURCES.append(("direct", search_direct, ["netease"]))

    # Source 2: myhkw netease — additional results with audio proxy URLs
    SEARCH_SOURCES.append(("myhkw_ne", search_myhkw, ["netease"]))

    # Source 3: QQ Music official API
    SEARCH_SOURCES.append(("qqmusic", search_qqmusic, ["qq"]))

    # Source 4: Kugou native API — for kugou platform tab
    SEARCH_SOURCES.append(("kugou", search_kugou, ["kugou"]))

    # Source 5: Kuwo (酷我音乐) search API
    SEARCH_SOURCES.append(("kuwo", search_kuwo, ["kuwo"]))

    # Source 6: Migu Music (咪咕音乐) official API
    SEARCH_SOURCES.append(("migu", search_migu, ["migu"]))

    # Source 7: Meting API — working search + direct audio URLs (netease/qq/kugou/kuwo)
    SEARCH_SOURCES.append(("meting", search_meting, ["netease", "qq", "kugou", "kuwo"]))

    # Source 8: GDStudio multi-platform aggregation — extra results for all platforms
    SEARCH_SOURCES.append(("gdstudio", search_gdstudio_wrapper, ["netease", "qq", "kugou", "kuwo", "migu"]))

    # Source 8: Xiageba (下歌吧) — supplementary results for netease/qq/migu
    SEARCH_SOURCES.append(("xiageba", search_xiageba, ["netease", "qq", "migu"]))

    # Source 9: tgws.cc — supplementary catalogue results for all platforms
    SEARCH_SOURCES.append(("tgws", search_tgws, ["netease", "qq", "kugou", "kuwo", "migu"]))

    # Source 10: blmp3.cn — supplementary catalogue results
    SEARCH_SOURCES.append(("blmp3", search_blmp3, ["netease", "qq", "kugou", "kuwo", "migu"]))

    # Source 11: htwav.top — supplementary catalogue results
    SEARCH_SOURCES.append(("htwav", search_htwav, ["netease", "qq", "kugou", "kuwo", "migu"]))

    # Source 12: luckxz.com — scraping fallback (lowest priority, no audio URL)
    SEARCH_SOURCES.append(("luckxz", search_luckxz, ["netease"]))

def _normalize(text: str) -> str:
    """Normalize text for dedup: lowercase, strip punctuation/spaces."""
    text = re.sub(r'[^\w\s]', '', text.lower())
    return re.sub(r'\s+', ' ', text).strip()


def _dedup_songs(all_songs: list[dict]) -> list[dict]:
    """Remove duplicate songs across sources. Keeps first occurrence (highest priority),
    but merges missing fields (cover, duration, etc.) from lower-priority duplicates."""
    seen = {}
    result = []
    for s in all_songs:
        key = (_normalize(s["title"]), _normalize(s["artist"]))
        if key not in seen and s["title"] != "Unknown":
            seen[key] = len(result)
            result.append(dict(s))
        elif key in seen:
            # Merge missing fields from lower-priority sources
            existing = result[seen[key]]
            for field in ("cover", "duration", "lyric", "url", "link"):
                if not existing.get(field) and s.get(field):
                    existing[field] = s[field]
            # Merge popularity fields by taking the highest known value
            for field in ("heat", "play_count", "comment_count"):
                try:
                    existing[field] = max(int(existing.get(field) or 0), int(s.get(field) or 0))
                except (TypeError, ValueError):
                    continue
    return result


def search_tonzhon(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via Tonzhon's API (search netease for any platform since songs overlap)."""
    try:
        # Tonzhon uses /api/search/{keyword} for authenticated, but we can try
        # Home page /api/new-songs for discovery
        prefix = {"netease": "n", "qq": "q", "migu": "m"}.get(platform, "n")
        resp = req.get(
            f"https://tonzhon.whamon.com/api/search/{req.utils.quote(query)}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tonzhon.whamon.com/"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("songs"):
                songs = []
                for item in data["songs"]:
                    songs.append({
                        "id": str(item.get("newId", "")).lstrip("nqmk"),
                        "title": item.get("name", "Unknown"),
                        "artist": item.get("artists", [{}])[0].get("name", "Unknown") if item.get("artists") else "Unknown",
                        "cover": item.get("cover", ""),
                        "lyric": "",
                        "url": "",
                        "link": f"https://music.163.com/#/song?id={str(item.get('newId','')).lstrip('nqmk')}",
                        "platform": platform,
                        "platform_name": PLATFORMS.get(platform, {}).get("name", platform),
                        "source": "tonzhon",
                    })
                return {"songs": songs, "total": len(songs), "error": None}
        return {"songs": [], "total": 0, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_gdstudio_wrapper(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via GDStudio multi-platform API (supports netease/qq/kugou/kuwo/migu)."""
    try:
        songs = search_gdstudio(query, platform, page)
        if not songs:
            return {"songs": [], "total": 0, "error": None}

        # Resolve cover URLs in parallel (GDStudio pic endpoint returns JSON, not image)
        import concurrent.futures

        def _resolve_cover(idx, s):
            cover = ""
            pic_id = s.get("pic_id", "")
            src = s.get("source", platform)
            if pic_id:
                try:
                    cover = gdstudio_cover(pic_id, src)
                except Exception:
                    pass
            return idx, cover

        covers = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_resolve_cover, i, s): i for i, s in enumerate(songs)}
            for fut in concurrent.futures.as_completed(futures, timeout=10):
                try:
                    idx, cover_url = fut.result()
                    if cover_url:
                        covers[idx] = cover_url
                except Exception:
                    pass

        # Convert to standard format
        result_songs = []
        for i, s in enumerate(songs):
            result_songs.append({
                "id": s["id"],
                "title": s["title"],
                "artist": s["artist"],
                "cover": covers.get(i, ""),
                "lyric": "",
                "url": "",    # lazy load via url_id
                "link": f"https://music.163.com/#/song?id={s['id']}" if s["source"] == "netease" else "",
                "platform": s["platform"],
                "platform_name": s["platform_name"],
                "source": "gdstudio",
                # Store GDStudio-specific IDs for lazy resolution
                "_url_id": s["url_id"],
                "_lyric_id": s["lyric_id"],
                "_pic_id": s["pic_id"],
                "_source": s["source"],
            })
        return {"songs": result_songs, "total": len(result_songs), "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


# Maximum songs returned per search page (expanded from 20).
_SEARCH_PAGE_SIZE = 50
# Outer concurrency for fan-out across sources. Kept moderate to avoid
# thread explosion from each source's own nested ThreadPoolExecutor.
_SEARCH_MAX_WORKERS = 10


def search_all_sources(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search across all configured sources, merge and dedup results.

    ``platform == "all"`` fans out across every real platform so a single
    query searches NetEase/QQ/Kugou/Kuwo/Migu simultaneously.
    """
    import concurrent.futures

    all_songs = []
    max_total = 0
    errors = []
    results_by_source = {}  # name → [songs]

    # Expand "all" into the real platforms; otherwise run a single platform.
    platform_list = list(PLATFORMS.keys()) if platform == "all" else [platform]

    # Build the task list: every (source × platform) pairing it supports.
    tasks = []
    for name, fn, platforms in SEARCH_SOURCES:
        for p in platform_list:
            if p in platforms:
                tasks.append((name, fn, p))

    overall_timeout = 45 if platform == "all" else 30
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SEARCH_MAX_WORKERS) as ex:
        futures = {}
        for name, fn, p in tasks:
            futures[ex.submit(fn, query, p, page)] = name

        for fut in concurrent.futures.as_completed(futures, timeout=overall_timeout):
            name = futures[fut]
            try:
                result = fut.result()
                if result.get("songs"):
                    results_by_source.setdefault(name, []).extend(result["songs"])
                    max_total = max(max_total, result.get("total", 0))
                    log.info(f"[{name}] found {len(result['songs'])} results, total={result.get('total',0)}")
                elif result.get("error"):
                    log.debug(f"[{name}] {result['error']}")
            except Exception as e:
                errors.append(f"{name}: {e}")
                log.debug(f"[{name}] failed: {e}")

    # Merge in SEARCH_SOURCES priority order (first = highest priority)
    for name, _fn, _platforms in SEARCH_SOURCES:
        if name in results_by_source:
            all_songs.extend(results_by_source[name])

    # Dedup and return
    deduped = _dedup_songs(all_songs)
    # Use the largest total reported by any source for pagination
    display_total = max(max_total, len(deduped))
    log.info(f"Search: {len(all_songs)} raw → {len(deduped)} deduped (total={display_total}) from {len(results_by_source)} sources")
    return {"songs": deduped[:_SEARCH_PAGE_SIZE], "total": display_total, "error": None if deduped else "No results from any source"}


# Register sources now that all functions are defined
_register_sources()


# ---------------------------------------------------------------------------
# Per-song platform stats (play count + comment count) & hot comments
# ---------------------------------------------------------------------------

def _fetch_netease_play_count(song_id: str) -> int:
    """Cumulative play count (``pc``) from NetEase's song-detail endpoint."""
    try:
        resp = req.get(
            "https://music.163.com/api/song/detail",
            params={"id": song_id, "ids": f"[{song_id}]"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://music.163.com/",
            },
            timeout=10,
        )
        songs = resp.json().get("songs", [])
        if songs:
            return int(songs[0].get("pc") or 0)
    except Exception as e:
        log.warning(f"[stats] NetEase play-count fetch failed for song {song_id}: {e}")
    return 0


def _fetch_netease_comments(song_id: str, limit: int = 20) -> dict:
    """Fetch comment total + hot comments for a NetEase song."""
    try:
        r = req.get(
            f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}",
            params={"limit": limit, "offset": 0},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"[comments] NetEase HTTP {r.status_code} for song {song_id}")
            return {"total": 0, "comments": [], "error": f"HTTP {r.status_code}"}

        data = r.json()
        # NetEase wraps failures in {"code": non-200, "message": ...} even with HTTP 200.
        code = data.get("code")
        if code not in (None, 200):
            log.warning(
                f"[comments] NetEase API code={code} for song {song_id}: "
                f"{data.get('message', '')}"
            )
            return {"total": 0, "comments": [], "error": f"API code {code}: {data.get('message', '')}"}

        hot = data.get("hotComments") or []
        comments = []
        for c in hot[:limit]:
            user = c.get("user") or {}
            comments.append({
                "nickname": user.get("nickname", "匿名"),
                "avatar": user.get("avatarUrl", ""),
                "content": c.get("content", ""),
                "liked": c.get("likedCount", 0),
                "time": c.get("time", 0),
            })
        return {"total": int(data.get("total", 0) or 0), "comments": comments}
    except Exception as e:
        log.warning(f"[comments] NetEase comment fetch failed for song {song_id}: {e}")
        return {"total": 0, "comments": [], "error": str(e)}


# Short-lived cache for platform play/comment counts so periodic refreshes
# (player-bar heartbeat) don't re-hit NetEase's API every few seconds.
_song_stats_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_SONG_STATS_CACHE_TTL = 120  # seconds


def _get_platform_song_stats(platform: str, song_id: str) -> tuple[int, int]:
    """Return (play_count, comment_count) for a song, best-effort per platform.

    NetEase is fully supported; other platforms currently return 0/0 (their
    public APIs don't expose reliable per-song play/comment counts by id).
    """
    if platform == "netease" and song_id:
        key = f"{platform}|{song_id}"
        cached = _song_stats_cache.get(key)
        if cached and time.time() - cached[0] < _SONG_STATS_CACHE_TTL:
            return cached[1]
        result = (_fetch_netease_play_count(song_id),
                  _fetch_netease_comments(song_id, limit=0)["total"])
        _song_stats_cache[key] = (time.time(), result)
        return result
    return 0, 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/img/<path:filename>")
def serve_img(filename):
    """Serve static images from the img/ directory."""
    from flask import send_from_directory
    img_dir = Path(__file__).parent / "img"
    return send_from_directory(str(img_dir), filename)


@app.route("/")
def index():
    return render_template("index.html", platforms=PLATFORMS, is_production=IS_PRODUCTION)


@app.route("/api/search")
def api_search():
    """Unified search across platforms."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503

    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "netease")
    page = request.args.get("page", 1, type=int)
    if not q:
        return jsonify({"error": "Missing query"}), 400

    # Check in-memory cache first
    cache_key = f"{q}|{platform}|{page}"
    _cleanup_search_cache()
    cached = _search_cache.get(cache_key)
    if cached:
        cached_ts, cached_data = cached
        if time.time() - cached_ts < _SEARCH_CACHE_TTL:
            log.info(f"[cache] Search HIT: {cache_key}")
            return jsonify(cached_data)

    # Search all configured sources, merge & dedup
    result = search_all_sources(q, platform, page)

    # Cache the result
    if not result.get("error"):
        _search_cache[cache_key] = (time.time(), result)

    return jsonify(result)


@app.route("/api/song/stats")
def api_song_stats():
    """Per-song stats for the player bar: platform play/comment count,
    on-site currently-playing count, and on-site download count."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    platform = request.args.get("platform", "netease")
    song_id = request.args.get("song_id", "")
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    play_count, comment_count = _get_platform_song_stats(platform, song_id)
    return jsonify({
        "play_count": play_count,
        "comment_count": comment_count,
        "site_playing": now_playing_count(platform, song_id),
        "site_downloads": analytics.get_song_download_count(title, artist, platform, song_id),
    })


@app.route("/api/song/comments")
def api_song_comments():
    """Hot comments for a song (NetEase hot comments currently)."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    platform = request.args.get("platform", "netease")
    song_id = request.args.get("song_id", "")
    if platform == "netease" and song_id:
        return jsonify(_fetch_netease_comments(song_id))
    return jsonify({"total": 0, "comments": [], "unsupported": True})


@app.route("/api/play/report", methods=["POST"])
def api_play_report():
    """Heartbeat endpoint: reports a play/pause/ended event for now-playing stats."""
    data = request.get_json(silent=True) or {}
    platform = str(data.get("platform", "netease"))
    song_id = str(data.get("song_id", ""))
    action = str(data.get("action", "play"))  # play | pause | heartbeat | ended
    report_play(platform, song_id, _get_request_visitor_id(), action)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Hot searches & search suggestions
# ---------------------------------------------------------------------------
_HOT_SEARCHES_FALLBACK = ["周杰伦", "林俊杰", "薛之谦", "陈奕迅", "邓紫棋", "许嵩", "毛不易", "周深", "五月天", "林宥嘉"]
_hot_cache: tuple[float, list[str]] = (0.0, [])


def _fetch_hot_searches() -> list[str]:
    """Hot search keywords from NetEase (cached 1h), fallback to a static list."""
    global _hot_cache
    now = time.time()
    if _hot_cache[1] and now - _hot_cache[0] < 3600:
        return _hot_cache[1]
    try:
        r = req.get(
            "https://music.163.com/api/search/hot",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            timeout=8,
        )
        hots = [h.get("first") or h.get("searchWord") or "" for h in r.json().get("result", {}).get("hots", [])]
        hots = [h for h in hots if h]
        if hots:
            _hot_cache = (now, hots[:20])
            return hots[:20]
    except Exception:
        pass
    return _HOT_SEARCHES_FALLBACK


def _fetch_suggestions(query: str) -> list[str]:
    """Search suggestions from NetEase suggest API."""
    try:
        r = req.get(
            "https://music.163.com/api/search/suggest/web",
            params={"s": query},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            timeout=8,
        )
        result = r.json().get("result", {})
        sugs = []
        for key in ("songs", "artists", "albums"):
            for item in result.get(key, [])[:5]:
                name = item.get("name", "")
                if name:
                    sugs.append(name)
        return list(dict.fromkeys(sugs))[:10]
    except Exception:
        return []


@app.route("/api/search/hot")
def api_search_hot():
    return jsonify({"hot": _fetch_hot_searches()})


@app.route("/api/search/suggest")
def api_search_suggest():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"suggestions": []})
    return jsonify({"suggestions": _fetch_suggestions(q)})


@app.route("/api/song/<platform>/<song_id>")
def api_song_detail(platform, song_id):
    """Get song detail with cover URL. Cross-searches NetEase if needed."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")

    # For netease: direct API
    if platform == "netease":
        detail = api.get_song_detail_sync(song_id)
        if detail:
            return jsonify({
                "id": detail.song_id, "title": detail.title, "artist": detail.artist,
                "cover": detail.cover_url, "lyric": "", "url": "",
                "link": f"https://music.163.com/#/song?id={detail.song_id}",
            })

    # For non-netease or netease-without-direct-match: cross-search netease for cover
    if title:
        try:
            search_q = f"{title} {artist}" if artist else title
            result = api.search_sync(search_q, limit=3)
            if result.songs:
                for ns in result.songs:
                    detail = api.get_song_detail_sync(ns.song_id)
                    if detail and detail.cover_url:
                        return jsonify({
                            "id": song_id, "title": title, "artist": artist,
                            "cover": detail.cover_url, "lyric": "", "url": "",
                            "link": "",
                        })
        except Exception:
            pass

    return jsonify({"error": "Song not found"}), 404


@app.route("/api/cover")
def api_cover_proxy():
    """Proxy cover images through server to bypass CDN Referer restrictions.

    NetEase CDN (p1.music.126.net etc.) now blocks requests that don't
    carry a ``Referer: https://music.163.com/`` header.  Browsers send
    the page's own origin as Referer, which gets rejected.  This endpoint
    fetches the image server-side with the correct Referer and returns it.
    """
    url = request.args.get("url", "")
    if not url or not url.startswith("http"):
        return jsonify({"error": "Invalid URL"}), 400

    # Allow only known music CDN domains (prevent open-proxy abuse)
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    allowed = (
        "music.126.net", "p1.music.126.net", "p2.music.126.net",
        "p3.music.126.net", "p4.music.126.net",
        "music.163.com", "api.music.163.com",
        "myhkw.cn", "s.myhkw.cn",
        "kugou.com", "imge.kugou.com",
        "kwimgs.kugou.com",
        "qpic.cn", "y.gtimg.cn",
    )
    if not any(domain == d or domain.endswith("." + d) for d in allowed):
        return jsonify({"error": "Domain not allowed"}), 403

    try:
        resp = req.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": "https://music.163.com/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return Response(resp.content, content_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _cross_search_netease(title: str, artist: str) -> tuple[str, str, str] | None:
    """Find a matching song on NetEase via myhkw proxy (with audio URL)."""
    try:
        result = resolve_song_by_keyword(title, artist)
        if result:
            audio_url, ne_id = result
            return (ne_id, artist, title)
    except Exception:
        pass
    return None


def _detect_audio_format(data: bytes) -> str:
    """Detect audio format from magic bytes: 'flac', 'mp3', or 'unknown'."""
    if not data:
        return "unknown"
    if data[:4] == b"fLaC":
        return "flac"
    # MP3: ID3 tag header or MPEG sync bytes (also covers AAC ADTS frames)
    if data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    # Not audio — error pages / auth-failure text often return HTTP 200
    return "unknown"


def _embed_mp3_tags(
    filepath: str, title: str, artist: str, album: str,
    cover_data: bytes | None, cover_mime: str,
):
    """Embed ID3v2 tags into an MP3 file."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, TPUB, TENC

    audio = MP3(filepath, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    if album:
        audio.tags.add(TALB(encoding=3, text=album))
    audio.tags.add(TPUB(encoding=3, text="卡塔音乐"))
    audio.tags.add(TENC(encoding=3, text="Kata Music"))
    if cover_data:
        audio.tags.add(APIC(
            encoding=3, mime=cover_mime, type=3,
            desc="Cover", data=cover_data,
        ))
    audio.save(v2_version=3)


def _embed_flac_tags(
    filepath: str, title: str, artist: str, album: str,
    cover_data: bytes | None, cover_mime: str,
):
    """Embed VorbisComment + picture into a FLAC file."""
    from mutagen.flac import FLAC, Picture

    audio = FLAC(filepath)
    audio["title"] = title
    audio["artist"] = artist
    if album:
        audio["album"] = album
    audio["publisher"] = "卡塔音乐"
    audio["organization"] = "Kata Music"
    audio["encodedby"] = "Kata Music"

    if cover_data:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = cover_mime
        pic.desc = "Cover"
        pic.data = cover_data
        audio.add_picture(pic)

    audio.save()


# ---------------------------------------------------------------------------
# LRC Translation helpers
# ---------------------------------------------------------------------------
_LRC_TIMESTAMP_RE = re.compile(r'^\[(\d{2}:\d{2}\.\d{2,3})\](.*)$')
_LRC_META_RE = re.compile(r'^\[(ti|ar|al|by|offset|re|ve|length):(.*)\]$', re.IGNORECASE)

# Patterns for lines that should NEVER be sent to the translator
_RE_PURE_SYMBOLS = re.compile(
    r'^[\s♩-♯★☆✿ 　♪♫♬～…★☆♥♦♣♠•·°※〓▽▼△▲□■◇◆◎●○◯☆★▶▷◀◁→←↑↓↗↘↙↖]+$'
)
_RE_INSTRUMENTAL = re.compile(
    r'(間奏|前奏|尾奏|伴奏|solo|interlude|instrumental|intro|outro|bridge|'
    r'纯音乐|演奏|过门|间场)',
    re.IGNORECASE,
)
_RE_LATIN_WORD = re.compile(r"[a-zA-Z]{2,}")


def _is_purely_structural(text: str) -> bool:
    """Return True if text is just spaces/dashes/separators (not translatable)."""
    cleaned = text.strip().replace(" ", "").replace("-", "").replace("~", "").replace("·", "")
    return len(cleaned) == 0


def _is_non_lyric_line(text: str) -> bool:
    """Return True for instrumental markers, pure symbols, or decorative
    content that should *never* be sent to the translation API.

    Skipping these avoids wasting API calls and prevents the translator
    from hallucinating translations for content like ``♪ 間奏 ♪``.
    """
    if not text or not text.strip():
        return True
    t = text.strip()
    if _RE_PURE_SYMBOLS.match(t):
        return True
    if _RE_INSTRUMENTAL.search(t):
        return True
    return False


def _needs_translation(text: str, target_lang: str, force_translate: bool = False) -> bool:
    """Return True if *text* contains characters outside the target language's
    native script — meaning it likely needs translation.

    When *force_translate* is True (e.g. the song as a whole has Japanese
    kana, confirming the lyrics are non-Chinese), ALL CJK-bearing lines are
    treated as translatable so pure-kanji Japanese lines are not skipped.
    """
    if not text or not text.strip():
        return False
    if _is_purely_structural(text):
        return False
    if _is_non_lyric_line(text):
        return False

    if force_translate:
        # The song is confirmed non-target → any content with actual
        # characters needs translation
        return True

    if target_lang == "zh":
        # Needs translation if text has Japanese kana, Korean hangul,
        # or meaningful Latin words (mixed-content).
        if any(0x3040 <= ord(ch) <= 0x30FF for ch in text):   # Hiragana / Katakana
            return True
        if any(0xAC00 <= ord(ch) <= 0xD7AF for ch in text):   # Hangul syllables
            return True
        if _RE_LATIN_WORD.search(text):                       # Latin words ≥ 2 chars
            return True
        return False

    if target_lang == "en":
        # Needs translation if text has any non-ASCII character
        return any(ord(ch) > 127 for ch in text)

    # Unknown target — translate everything
    return True


def _detect_source_language(texts: list[str]) -> str:
    """Heuristic to pick an explicit source language for a batch of lyrics.

    When the batch contains Japanese kana or Korean hangul we tell
    Google Translate exactly what the source is instead of relying on
    ``source="auto"``.  This fixes mixed-content lines (e.g. Japanese +
    English) where the auto-detector gives up.
    """
    kana = 0
    hangul = 0
    for t in texts:
        for ch in t:
            cp = ord(ch)
            if 0x3040 <= cp <= 0x30FF:
                kana += 1
            elif 0xAC00 <= cp <= 0xD7AF:
                hangul += 1
    if kana > 0:
        return "ja"
    if hangul > 0:
        return "ko"
    return "auto"


def _translate_one_chunk(
    chunk: list[str],
    source_lang: str,
    target_full: str,
    delimiter: str,
    chunk_idx: tuple[int, int],
) -> tuple:
    """Translate a single chunk in a worker thread.  Returns
    ``(chunk_idx, translated_parts_or_None)`` so the caller can
    reassemble results in order.
    """
    try:
        joined = delimiter.join(chunk)
        translator = GoogleTranslator(source=source_lang, target=target_full)
        translated_joined = translator.translate(joined)
        if translated_joined:
            parts = translated_joined.split(delimiter)
            if len(parts) == len(chunk):
                return (chunk_idx, parts)
    except Exception:
        pass
    return (chunk_idx, None)


def _batch_translate(texts: list[str], target_lang: str) -> list[str]:
    """Translate a list of lyric texts using Google Translate.

    Optimisations over the naive approach:

    * Detects the dominant source language (ja/ko) so mixed-content lines
      are fully translated.
    * Translates chunks in **parallel** (3 workers by default) — the API
      is I/O-bound, so concurrency gives a ~2-3× speedup.
    * Falls back to line-by-line translation for any chunk that failed,
      retrying with ``source="auto"`` if the explicit source didn't help.
    * Re-uses translator instances where possible in the fallback path.
    """
    if not texts:
        return []

    lang_map = {"zh": "chinese (simplified)", "en": "english"}
    target_full = lang_map.get(target_lang, target_lang)
    source_lang = _detect_source_language(texts)
    delimiter = " ||| "
    chunk_size = 30  # slightly smaller chunks → faster per-chunk, better parallelism
    n_chunks = (len(texts) + chunk_size - 1) // chunk_size

    # ------------------------------------------------------------------
    # Phase 1 — parallel batch translation
    # ------------------------------------------------------------------
    ordered = [None] * n_chunks
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for ci in range(n_chunks):
            start = ci * chunk_size
            chunk = texts[start:start + chunk_size]
            fut = pool.submit(
                _translate_one_chunk, chunk, source_lang, target_full, delimiter, (ci, ci)
            )
            futures[fut] = ci

        for fut in as_completed(futures):
            ci = futures[fut]
            try:
                _, parts = fut.result()
                ordered[ci] = parts
            except Exception:
                ordered[ci] = None

    # ------------------------------------------------------------------
    # Phase 2 — fill gaps with individual translation
    # ------------------------------------------------------------------
    results = []
    for ci in range(n_chunks):
        parts = ordered[ci]
        if parts is not None:
            results.extend(parts)
            continue

        # Fallback for this chunk — translate line-by-line
        start = ci * chunk_size
        for text in texts[start:start + chunk_size]:
            translated = False
            # Try explicit source first
            if source_lang != "auto":
                try:
                    t = GoogleTranslator(source=source_lang, target=target_full)
                    result = t.translate(text)
                    if result and result != text:
                        results.append(result)
                        translated = True
                except Exception:
                    pass
            # Retry with auto
            if not translated:
                try:
                    t = GoogleTranslator(source="auto", target=target_full)
                    result = t.translate(text)
                    results.append(result if result else text)
                except Exception:
                    results.append(text)

    return results


def translate_lrc(lrc_text: str, target_lang: str) -> str:
    """Translate LRC lyrics, preserving all timestamps and metadata tags.

    Only the text portions are translated; ``[mm:ss.xx]`` brackets and
    metadata tags like ``[ti:...]`` / ``[ar:...]`` are kept intact.

    Non-lyric content (instrumental markers, pure symbols like ♪♫) is
    intentionally skipped so API calls are not wasted on untranslatable
    decoration.
    """
    if not _HAS_TRANSLATOR:
        return lrc_text
    if not lrc_text or not lrc_text.strip():
        return lrc_text

    lines = lrc_text.replace("\r\n", "\n").split("\n")

    # ------------------------------------------------------------------
    # Context detection: does the song as a whole contain Japanese kana?
    # If yes, *all* CJK-bearing lines are treated as translatable so
    # pure-kanji Japanese lines aren't skipped just because they lack
    # kana characters.
    # ------------------------------------------------------------------
    _has_kana_anywhere = any(
        0x3040 <= ord(ch) <= 0x30FF for line in lines for ch in line
    )
    _has_hangul_anywhere = any(
        0xAC00 <= ord(ch) <= 0xD7AF for line in lines for ch in line
    )
    force_translate = _has_kana_anywhere or _has_hangul_anywhere

    # Collect translatable texts and their positions
    texts_to_translate = []                      # ordered list → translator
    line_map = []                                # (line_idx, is_ts, prefix, orig_text)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            line_map.append((i, False, "", ""))
            continue

        # Timestamp line: [mm:ss.xx] lyrics text
        m = _LRC_TIMESTAMP_RE.match(stripped)
        if m:
            timestamp = m.group(1)
            text = m.group(2).strip()
            if _is_non_lyric_line(text):
                # Keep the line as-is but don't waste an API call
                line_map.append((i, True, f"[{timestamp}] ", ""))
            elif text and _needs_translation(text, target_lang, force_translate):
                texts_to_translate.append(text)
                line_map.append((i, True, f"[{timestamp}] ", text))
            else:
                line_map.append((i, True, f"[{timestamp}] ", ""))
            continue

        # Metadata tag: [ti:Song Title], [ar:Artist Name], etc.
        m = _LRC_META_RE.match(stripped)
        if m:
            tag = m.group(1)
            value = m.group(2).strip()
            if value and _needs_translation(value, target_lang, force_translate):
                texts_to_translate.append(value)
                line_map.append((i, False, f"[{tag}:", value))
            else:
                line_map.append((i, False, "", ""))
            continue

        # Other non-timestamp lines — keep as-is
        line_map.append((i, False, "", ""))

    if not texts_to_translate:
        return lrc_text

    # Translate
    translated_texts = _batch_translate(texts_to_translate, target_lang)

    # Reassemble
    result_lines = list(lines)
    ti = 0
    for orig_idx, is_timestamp, prefix, original_text in line_map:
        if original_text and ti < len(translated_texts):
            translated = translated_texts[ti]
            ti += 1
            if is_timestamp:
                result_lines[orig_idx] = f"{prefix}{translated}"
            else:
                result_lines[orig_idx] = f"{prefix}{translated}]"

    return "\n".join(result_lines)


def _download_mp3_from_cdn(cdn_url: str, artist: str, title: str, song_id: str, platform: str):
    """Download MP3 from CDN URL, embed cover, return Response."""
    mp3_data = None
    for hdrs in [
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://music.163.com/"},
        {"User-Agent": "NeteaseMusic/8.0.0", "Referer": "https://music.163.com/"},
        {"User-Agent": "Mozilla/5.0", "Referer": "https://tonzhon.whamon.com/"},
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},  # no Referer (Meting)
    ]:
        try:
            r = req.get(cdn_url, timeout=25, headers=hdrs)
            if r.status_code == 200 and len(r.content) > 1024:
                # Accept any audio content (MP3/FLAC/AAC/etc)
                fmt = _detect_audio_format(r.content)
                if fmt in ("mp3", "flac"):
                    mp3_data = r.content
                    break
        except Exception:
            continue

    if not mp3_data:
        return None

    # Embed ID3 tags: title, artist, album, cover
    try:
        import tempfile as _tmp

        # ── Get metadata from NetEase API for richer tags ──
        album = ""
        cover_url = ""
        try:
            detail = api.get_song_detail_sync(song_id)
            if detail:
                if detail.album:
                    album = detail.album
                if detail.cover_url:
                    cover_url = detail.cover_url
        except Exception:
            pass

        # ── Download cover image (CDN now requires music-platform Referer) ──
        cover_data = None
        cover_mime = "image/jpeg"
        cover_urls_to_try = []
        if cover_url:
            cover_urls_to_try.append(cover_url)
        # Also try a high-res variant (NetEase CDN pattern: <id>?param=300y300)
        if cover_url and "music.126.net" in cover_url:
            cover_urls_to_try.append(cover_url.split("?")[0] + "?param=500y500")

        for cu in cover_urls_to_try:
            if cover_data:
                break
            for hdrs in [
                {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "Referer": "https://music.163.com/"},
                {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
                {"User-Agent": "NeteaseMusic/8.0.0", "Referer": "https://music.163.com/"},
            ]:
                try:
                    cr = req.get(cu, timeout=12, headers=hdrs)
                    if cr.status_code == 200 and len(cr.content) > 500:
                        cover_data = cr.content
                        if cover_data[:4] == b"\x89PNG":
                            cover_mime = "image/png"
                        break
                except Exception:
                    continue

        # ── Write audio to temp file for mutagen processing ──
        tf = _tmp.NamedTemporaryFile(delete=False, suffix=".mp3")
        try:
            tf.write(mp3_data)
            tf.close()

            # Detect format and embed tags
            audio_ext = _detect_audio_format(mp3_data)
            if audio_ext == "flac":
                _embed_flac_tags(tf.name, title, artist, album, cover_data, cover_mime)
            else:
                _embed_mp3_tags(tf.name, title, artist, album, cover_data, cover_mime)

            # Read back the tagged file
            with open(tf.name, "rb") as f:
                mp3_data = f.read()
        finally:
            try:
                os.unlink(tf.name)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[download] tag embedding failed for {title}: {e}")

    from urllib.parse import quote
    # HTTP headers are Latin-1 only. filename= must be ASCII;
    # filename*= (RFC 5987) handles Unicode.
    full_name = f"{artist} - {title}"
    ext = "flac" if _detect_audio_format(mp3_data) == "flac" else "mp3"
    mime_type = "audio/flac" if ext == "flac" else "audio/mpeg"
    return Response(mp3_data, content_type=mime_type,
        headers={"Content-Disposition": _make_content_disp(full_name, ext)})


def _is_allowed_direct_url(url: str) -> bool:
    """Only allow direct audio URLs from trusted providers (Meting)."""
    from urllib.parse import urlparse
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host == "api.i-meto.com" or host.endswith(".i-meto.com")


# NetEase quality tiers in descending order. User picks one; resolution falls
# back to lower tiers if the preferred one isn't available.
_QUALITY_ORDER = ["hires", "lossless", "exhigh", "higher", "standard"]


def _quality_tiers(preferred: str) -> list[str]:
    """Return an ordered list of quality tiers to try (preferred first)."""
    tiers = []
    if preferred in _QUALITY_ORDER:
        tiers.append(preferred)
    tiers += ["lossless", "exhigh", "standard"]
    seen = set()
    out = []
    for t in tiers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


@app.route("/api/download/<platform>/<song_id>")
def api_download(platform, song_id):
    """Download MP3 — tries multiple audio sources until success."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    import time
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    direct_url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "")  # hires/lossless/exhigh/higher/standard
    name = f"{artist} - {title}" if title else song_id
    artist = artist or "Unknown"
    title = title or "Unknown"

    # Each strategy returns (Response, None) on success or (None, str_error) on failure

    def try_direct_url():
        """Strategy 0: Download from a pre-resolved direct audio URL (e.g. Meting)."""
        if not direct_url:
            return None, "no direct url"
        if not _is_allowed_direct_url(direct_url):
            return None, "direct url host not allowed"
        resp = _download_mp3_from_cdn(direct_url, artist, title, song_id, platform)
        return (resp, None) if resp else (None, "direct url download failed")

    def try_netease_direct():
        """Strategy 1: Direct NetEase API for netease platform songs."""
        if platform != "netease":
            return None, "not netease platform"
        url = None
        for t in _quality_tiers(quality):
            url = api.get_song_url_sync(song_id, t)
            if url:
                break
        if not url:
            return None, "netease direct no url"
        resp = _download_mp3_from_cdn(url, artist, title, song_id, "netease")
        return (resp, None) if resp else (None, "netease direct download failed")

    def try_netease_cross_search():
        """Strategy 2: Cross-search NetEase by title+artist, use NetEase audio."""
        if not title or title == "Unknown":
            return None, "no title for cross-search"
        try:
            search_q = f"{title} {artist}" if artist else title
            result = api.search_sync(search_q, limit=5)
            if not result or not result.songs:
                return None, "cross-search: no netease match"
            # Try each match until we get a download
            for ns in result.songs:
                url = None
                for t in _quality_tiers(quality):
                    url = api.get_song_url_sync(ns.song_id, t)
                    if url:
                        break
                if url:
                    resp = _download_mp3_from_cdn(url, artist, title, ns.song_id, "netease")
                    if resp:
                        log.info(f"[download] cross-search matched: {ns.title} (id={ns.song_id})")
                        return (resp, None)
            return None, "cross-search: all matches failed"
        except Exception as e:
            return None, f"cross-search error: {e}"

    def try_myhkw_by_keyword():
        """Strategy 4: Search myhkw.cn by title + artist (may be down)."""
        if not title or title == "Unknown":
            return None, "no title to search"
        try:
            result = resolve_song_by_keyword(title, artist)
            if not result:
                return None, "myhkw keyword search failed"
            cdn_url, matched_id = result
            resp = _download_mp3_from_cdn(cdn_url, artist, title, matched_id, platform)
            return (resp, None) if resp else (None, "myhkw cdn download failed")
        except Exception as e:
            return None, f"myhkw keyword error: {e}"

    def try_myhkw_by_id():
        """Strategy 5: Resolve by song ID via myhkw.cn (netease only)."""
        try:
            cdn_url = resolve_song_url(song_id, platform)
        except Exception as e:
            return None, f"myhkw by-id error: {e}"
        if not cdn_url:
            return None, "myhkw by-id no url"
        resp = _download_mp3_from_cdn(cdn_url, artist, title, song_id, platform)
        return (resp, None) if resp else (None, "myhkw by-id cdn download failed")

    def try_gdstudio():
        """Strategy 6: GDStudio URL resolution (multi-platform last resort)."""
        try:
            info = gdstudio_get_url(song_id, platform)
        except Exception as e:
            return None, f"gdstudio error: {e}"
        if not info or not info.get("url"):
            return None, "gdstudio no url"
        resp = _download_mp3_from_cdn(info["url"], artist, title, song_id, platform)
        return (resp, None) if resp else (None, "gdstudio cdn download failed")

    # Order: direct URL (if provided) → NetEase direct → NetEase cross-search
    # → myhkw by keyword → myhkw by ID → GDStudio (last resort)
    strategies = [
        ("direct_url", try_direct_url),
        ("netease_direct", try_netease_direct),
        ("netease_cross", try_netease_cross_search),
        ("myhkw_keyword", try_myhkw_by_keyword),
        ("myhkw_id", try_myhkw_by_id),
        ("gdstudio", try_gdstudio),
    ]

    max_rounds = 2
    errors = []

    for round_num in range(1, max_rounds + 1):
        for strategy_name, strategy_fn in strategies:
            for attempt in (1, 2):
                try:
                    resp, err = strategy_fn()
                    if resp:
                        log.info(f"[download] SUCCESS: {strategy_name} (round={round_num}, attempt={attempt})")
                        try:
                            analytics.track_download(song_id, title, artist, platform, strategy_name, True, _get_request_visitor_id())
                        except Exception:
                            pass
                        return resp
                    errors.append(f"[R{round_num}/A{attempt}] {strategy_name}: {err}")
                except Exception as e:
                    errors.append(f"[R{round_num}/A{attempt}] {strategy_name}: {type(e).__name__}: {e}")
                if attempt == 1:
                    time.sleep(0.5)
            time.sleep(0.3)
        if round_num < max_rounds:
            log.info(f"[download] Round {round_num} failed, retrying...")
            time.sleep(1)

    log.error(f"[download] ALL FAILED for {name}: {'; '.join(errors[-10:])}")
    try:
        analytics.track_download(song_id, title, artist, platform, "", False, _get_request_visitor_id())
    except Exception:
        pass
    return jsonify({
        "error": "所有音源均无法下载",
        "detail": f"《{name}》经过了 {max_rounds} 轮共 {len(strategies)*2*max_rounds} 次尝试，所有音源均失败。",
        "errors": errors[-15:],
        "solutions": [
            {"title": "换一首歌试试"},
            {"title": "检查网络连接"},
        ],
    }), 403


def stream_download(url: str, artist: str, title: str, quality: str):
    """Stream a direct download URL to the browser."""
    ext = "flac" if quality in ("lossless", "hires") else "mp3"
    full_name = f"{artist} - {title}"

    resp = req.get(url, stream=True, timeout=60,
                   headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()

    return Response(
        resp.iter_content(8192),
        content_type=resp.headers.get("Content-Type", f"audio/{'flac' if ext == 'flac' else 'mpeg'}"),
        headers={"Content-Disposition": _make_content_disp(full_name, ext)},
    )


def stream_download_from_bytes(data: bytes, artist: str, title: str):
    """Stream raw audio bytes to browser as MP3."""
    ext = "mp3" if data[:3] == b"ID3" else ("flac" if data[:4] == b"fLaC" else "mp3")
    full_name = f"{artist} - {title}"
    return Response(
        data,
        content_type=f"audio/{'flac' if ext == 'flac' else 'mpeg'}",
        headers={"Content-Disposition": _make_content_disp(full_name, ext)},
    )


@app.route("/api/p/<song_id>")
def api_resolve_source(song_id):
    """Resolve audio source URL via myhkw.cn proxy (replaces dead Tonzhon).

    Accepts NetEase song IDs or legacy Tonzhon IDs (e.g. ``n186016``).
    """
    platform = request.args.get("platform", "")
    raw_id = request.args.get("id", "")

    if platform and raw_id:
        url = resolve_song_url(raw_id, platform)
    else:
        url = resolve_song_url_raw(song_id)

    if url:
        return jsonify({"success": True, "url": url})
    return jsonify({"success": False, "message": "no source"}), 404


def _looks_like_audio(head: bytes, content_type: str = "") -> bool:
    """True if the payload looks like audio rather than an error page."""
    ct = (content_type or "").lower()
    if ct.startswith("audio/"):
        return True
    if not head:
        return False
    if head[:4] == b"fLaC" or head[:4] == b"OggS" or head[:3] == b"ID3":
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    # MP3 frame sync (also covers AAC ADTS frames)
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False


def _stream_audio_response(downstream, first_chunk: bytes, iterator, range_header: str):
    """Wrap a verified audio stream in a Flask Response (Range-aware)."""
    content_type = downstream.headers.get("Content-Type", "audio/mpeg")
    content_length = downstream.headers.get("Content-Length")
    resp_headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=7200",
    }
    status = 200

    if range_header and content_length:
        try:
            raw_range = range_header.replace("bytes=", "")
            parts = raw_range.split("-")
            range_start = int(parts[0]) if parts[0] else 0
            range_end = int(parts[1]) if len(parts) > 1 and parts[1] else int(content_length) - 1

            resp_headers["Content-Range"] = f"bytes {range_start}-{range_end}/{content_length}"
            resp_headers["Content-Length"] = str(range_end - range_start + 1)
            status = 206

            full_data = first_chunk + b"".join(iterator)
            return Response(
                full_data[range_start:range_end + 1],
                status=206,
                headers=resp_headers,
            )
        except (ValueError, IndexError):
            pass

    if content_length:
        resp_headers["Content-Length"] = content_length
    return Response(
        itertools.chain([first_chunk], iterator),
        status=status,
        headers=resp_headers,
    )


@app.route("/api/stream/<song_id>")
def api_stream_audio(song_id):
    """Stream audio through the server so the browser never hits the CDN directly.

    Audio sources are tried in strict priority order. If one source fails to
    deliver playable audio (dead link, expired auth, non-audio body), the next
    source is tried — an error is returned only when every source fails.
    Only verified-good URLs are cached; a cached URL that fails is evicted.
    Supports HTTP Range requests for seeking.
    """
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503

    platform = request.args.get("platform", "")
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    direct_url = request.args.get("url", "").strip()
    pref_quality = request.args.get("quality", "")

    # ── Check audio URL cache first ──
    cache_key = f"{platform}|{song_id}"
    _cleanup_audio_url_cache()
    cached = _audio_url_cache.get(cache_key)
    cached_fresh = cached if cached and time.time() - cached[0] < _AUDIO_URL_CACHE_TTL else None

    def candidates():
        """Yield (label, url, quality) in priority order — lazily.

        Each source is only queried when every higher-priority candidate
        already failed to produce playable audio.
        """
        if cached_fresh:
            yield ("cache", cached_fresh[1], cached_fresh[2])

        # Direct audio URL (e.g. Meting link merged into search results).
        if direct_url and _is_allowed_direct_url(direct_url):
            yield ("direct", direct_url, "direct")

        # Strategy 1: NetEase direct (for netease platform), each quality tier.
        if platform == "netease":
            tried.append("netease")
            for t in _quality_tiers(pref_quality):
                try:
                    u = api.get_song_url_sync(song_id, t)
                except Exception:
                    u = None
                if u:
                    yield ("netease", u, t)

        # Strategy 2: NetEase cross-search by title+artist.
        if title and title != "Unknown":
            tried.append("netease_cross")
            try:
                search_q = f"{title} {artist}" if artist else title
                result = api.search_sync(search_q, limit=5)
                if result and result.songs:
                    for ns in result.songs:
                        for t in _quality_tiers(pref_quality):
                            try:
                                u = api.get_song_url_sync(ns.song_id, t)
                            except Exception:
                                u = None
                            if u:
                                yield ("netease_cross", u, t)
            except Exception as e:
                log.warning(f"[stream] cross-search failed: {e}")

        # Strategy 3: myhkw.cn by song ID (netease only, may be down).
        tried.append("myhkw_id")
        try:
            u = resolve_song_url(song_id, platform)
        except Exception:
            u = None
        if u:
            yield ("myhkw_id", u, "myhkw_id")

        # Strategy 4: myhkw keyword search.
        if title and title != "Unknown":
            tried.append("myhkw_kw")
            try:
                r = resolve_song_by_keyword(title, artist)
            except Exception:
                r = None
            if r:
                yield ("myhkw_kw", r[0], "myhkw_kw")

        # Strategy 5: GDStudio URL resolution (multi-platform last resort).
        tried.append("gdstudio")
        try:
            info = gdstudio_get_url(song_id, platform)
        except Exception:
            info = None
        if info and info.get("url"):
            yield ("gdstudio", info["url"], "gdstudio")

    headers_base = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
    }

    # ── Try each source in order until one delivers playable audio ──
    tried = []
    last_err = "no audio source available"
    for label, url, quality in candidates():
        headers = dict(headers_base)
        # Direct URLs (Meting) need no music-platform Referer; CDN URLs do.
        if quality != "direct":
            headers["Referer"] = "https://music.163.com/"
        try:
            downstream = req.get(url, stream=True, timeout=60, headers=headers)
            downstream.raise_for_status()
            it = downstream.iter_content(8192)
            try:
                first = next(it)
            except StopIteration:
                first = b""
            if not _looks_like_audio(first[:16], downstream.headers.get("Content-Type", "")):
                log.warning(f"[stream] '{label}' gave non-audio body for {song_id}, trying next source")
                downstream.close()
                if label == "cache":
                    _audio_url_cache.pop(cache_key, None)
                continue
        except Exception as e:
            last_err = f"{label}: {e}"
            log.warning(f"[stream] source '{label}' failed for {song_id}: {e}")
            if label == "cache":
                _audio_url_cache.pop(cache_key, None)
            continue

        # Verified playable — cache it, then stream.
        _audio_url_cache[cache_key] = (time.time(), url, quality)
        log.info(f"[stream] audio OK via '{label}' for {song_id}")
        return _stream_audio_response(downstream, first, it, request.headers.get("Range"))

    log.error(f"[stream] ALL SOURCES FAILED for {song_id}: {'; '.join(tried)}")
    return jsonify({"error": "no audio source available", "tried": tried, "detail": last_err}), 404


@app.route("/api/lrc/<platform>/<song_id>")
def api_lrc_download(platform, song_id):
    """Download LRC lyrics file. Named same as the song."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    # Prefer query params from frontend (same as MP3 download filename)
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    lrc_text = ""

    # Fallback: get metadata from API if not provided by frontend
    if not title and platform == "netease":
        detail = api.get_song_detail_sync(song_id)
        if detail:
            artist, title = detail.artist, detail.title
    if not title:
        title = song_id
    if not artist:
        artist = "Unknown"

    # Get lyrics from myhkw first, then direct NetEase API
    lrc_text = myhkw_lyrics(song_id, platform)

    if not lrc_text and platform == "netease":
        lrc_text = api.get_lyrics_sync(song_id)

    if not lrc_text:
        lrc_text = "[00:00.00] 暂无歌词"

    # --- Translation support ---
    translate_lang = request.args.get("translate", "").strip().lower()
    if translate_lang in ("zh", "en") and lrc_text and lrc_text != "[00:00.00] 暂无歌词":
        try:
            translated = translate_lrc(lrc_text, translate_lang)
            if translated and translated != lrc_text:
                lrc_text = translated
                log.info(f"[lrc] Translated to '{translate_lang}' for {artist} - {title}")
        except Exception as e:
            log.error(f"[lrc] Translation failed: {e}")

    safe_name = f"{artist} - {title}"

    headers = {"Content-Disposition": _make_content_disp(safe_name, "lrc")}
    if translate_lang in ("zh", "en"):
        headers["X-Translation"] = translate_lang

    return Response(
        lrc_text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Playlist import — parse URLs from mainstream music platforms
# ---------------------------------------------------------------------------

_PLAYLIST_URL_PATTERNS = [
    # NetEase: music.163.com/playlist?id=123  or  /#/playlist?id=123
    (re.compile(r'music\.163\.com/(?:#/)?playlist\?id=(\d+)', re.I), "netease"),
    (re.compile(r'music\.163\.com/playlist/(\d+)', re.I), "netease"),
    # QQ Music: y.qq.com/n/ryqq/playlist/123
    (re.compile(r'y\.qq\.com/n/ryqq/playlist/(\d+)', re.I), "qq"),
    (re.compile(r'[?&]id=(\d+)', re.I), "qq"),  # fallback if domain matches
    # Kugou
    (re.compile(r'kugou\.com/songlist/(\w+)', re.I), "kugou"),
    (re.compile(r't\d?\.kugou\.com/([a-zA-Z0-9]+)', re.I), "kugou"),
    # Kuwo
    (re.compile(r'kuwo\.cn/playlist_detail/(\d+)', re.I), "kuwo"),
    (re.compile(r'kuwo\.cn/album_detail/(\d+)', re.I), "kuwo"),
]

# Platforms covered by the yt-dlp universal fallback (shown in UI hints)
_YTDLP_HINT_PLATFORMS = (
    "网易云音乐 · QQ音乐 · 酷狗 · 酷我 · "
    "YouTube · B站 · Spotify · SoundCloud · "
    "Apple Music · Bandcamp · 抖音 · 小红书 · 微博 · 快手 · Vimeo · 等 1000+ 平台"
)


def parse_playlist_url(url: str) -> tuple[str, str] | None:
    """Extract (platform, playlist_id) from a music-platform share URL."""
    for pattern, platform in _PLAYLIST_URL_PATTERNS:
        if platform == "qq" and pattern.pattern == r'[?&]id=(\d+)':
            if "y.qq.com" not in url and "qq.com" not in url:
                continue
        m = pattern.search(url)
        if m:
            return (platform, m.group(1))
    return None


# ---------------------------------------------------------------------------
# yt-dlp universal playlist extractor — handles 1000+ sites as fallback
# ---------------------------------------------------------------------------

def _extractor_label(extractor_key: str) -> str:
    """Map yt-dlp extractor key to a user-friendly Chinese/English label."""
    _LABELS = {
        "youtube": "▶️ YouTube",
        "bilibili": "🎬 B站",
        "soundcloud": "☁️ SoundCloud",
        "spotify": "🟢 Spotify",
        "applemusic": "🎵 Apple Music",
        "bandcamp": "🎸 Bandcamp",
        "vimeo": "🎞️ Vimeo",
        "dailymotion": "📺 Dailymotion",
        "tiktok": "🎵 TikTok",
        "nicovideo": "📺 Niconico",
        "douyin": "🎵 抖音",
        "xiaohongshu": "📕 小红书",
        "weibo": "📢 微博",
        "kuaishou": "⚡ 快手",
        "qqmusic": "🎶 QQ音乐",
        "netease": "🎶 网易云",
        "kugou": "🎶 酷狗",
        "kuwo": "🎶 酷我",
    }
    for key, label in _LABELS.items():
        if key in extractor_key.lower():
            return label
    return "🔗 导入歌单"


def _extract_playlist_via_ytdlp(url: str) -> tuple[list[dict], str | None]:
    """Use yt-dlp to extract playlist entries from any supported site.

    Returns (songs, error_message).  When successful, songs is a list of
    standardised song dicts with platform='ytdl' and error_message is None.
    When yt-dlp is unavailable or extraction fails, songs is empty and
    error_message describes the problem.

    Requires yt-dlp to be installed (same dependency as video_extractor.py).
    """
    if not _HAS_YTDLP:
        return [], None  # None error = silently skip, let caller decide message

    import subprocess as _sp
    import json as _json

    try:
        result = _sp.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--flat-playlist", "--dump-json",
                "--no-warnings", "--no-check-certificate",
                "--socket-timeout", "30",
                "--extractor-args", "youtubetab:skip=auth",
                url,
            ],
            capture_output=True, text=True, timeout=90,
            encoding="utf-8", errors="replace",
        )

        if result.returncode != 0:
            stderr = (result.stderr or "")[:300]
            log.warning(f"[playlist] yt-dlp exited {result.returncode}: {stderr}")
            # Distinguish real errors from "no playlist found"
            if "not a valid URL" in stderr.lower():
                return [], "无效的链接格式"
            if "unsupported url" in stderr.lower():
                return [], None  # let caller show generic message
            if "no video" in stderr.lower() or "not found" in stderr.lower():
                return [], "未找到任何内容，链接可能已失效"
            return [], None

        songs = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            title = entry.get("title", "") or entry.get("fulltitle", "")
            if not title:
                continue

            # Strip common video-title suffixes
            title = re.sub(
                r'\s*[\[\(](?:Official\s*)?(?:MV|Music\s*Video|Official\s*Video|'
                r'Audio|Lyric\s*Video|Official\s*Audio|Visualizer|Live|Cover|'
                r'Performance|Remix|HD|4K|1080p|720p)[\]\)]\s*$',
                '', title, flags=re.I,
            ).strip()

            uploader = entry.get("uploader", "") or entry.get("channel", "") or entry.get("artist", "")
            duration = int(entry.get("duration", 0) or 0)
            webpage_url = entry.get("webpage_url", "") or entry.get("url", "") or url
            thumbnail = entry.get("thumbnail", "")
            if not thumbnail:
                thumbs = entry.get("thumbnails", [])
                if thumbs:
                    thumbnail = thumbs[0].get("url", "") or thumbs[-1].get("url", "")
            extractor = entry.get("extractor", "") or entry.get("ie_key", "")

            songs.append({
                "id": f"ytdl_{hashlib.md5((title + uploader).encode()).hexdigest()[:12]}",
                "title": title,
                "artist": uploader or "未知歌手",
                "cover": thumbnail,
                "duration": duration * 1000 if duration else 0,
                "platform": "ytdl",
                "platform_name": _extractor_label(extractor),
                "source_url": webpage_url,
                "extractor": extractor,
            })

        if not songs:
            return [], "歌单为空或无法读取"

        log.info(f"[playlist] yt-dlp extracted {len(songs)} songs from {url[:80]}")
        return songs, None

    except _sp.TimeoutExpired:
        log.warning("[playlist] yt-dlp timed out")
        return [], "解析超时，请检查网络后重试"
    except FileNotFoundError:
        log.warning("[playlist] yt-dlp binary not found")
        return [], None
    except Exception as e:
        log.warning(f"[playlist] yt-dlp extraction failed: {e}")
        return [], None


def _cross_search_songs(ytdl_songs: list[dict]) -> list[dict]:
    """Cross-reference yt-dlp songs against our music platforms.

    For each song, searches across netease/kugou/kuwo/qq to find a matching
    song with a proper platform ID (so streaming/download works).

    Only the first 20 songs are cross-searched to keep latency reasonable
    (the rest keep their yt-dlp metadata).  Songs that match get replaced
    with the platform version; unmatched songs stay as-is.
    """
    if not ytdl_songs:
        return ytdl_songs

    import concurrent.futures

    _CROSS_SEARCH_LIMIT = 20
    to_search = ytdl_songs[:_CROSS_SEARCH_LIMIT]
    skipped = ytdl_songs[_CROSS_SEARCH_LIMIT:]
    results: dict[int, dict] = {}

    def _match_one(idx: int, song: dict) -> tuple[int, dict | None]:
        query = f"{song['title']} {song['artist']}" if song.get("artist") != "未知歌手" else song["title"]
        norm_title = _normalize(song["title"])
        norm_artist = _normalize(song.get("artist", ""))

        best = None
        best_score = 0

        for plat in ["netease", "kugou", "kuwo", "qq"]:
            try:
                result = search_all_sources(query, plat, 1)
                if not result.get("songs"):
                    continue
                for s in result["songs"]:
                    nt = _normalize(s.title)
                    na = _normalize(s.artist)
                    if nt == norm_title:
                        score = 100
                    elif norm_title in nt or nt in norm_title:
                        score = 70
                    else:
                        # Fuzzy: shared character ratio
                        common = len(set(nt) & set(norm_title))
                        threshold = max(len(nt), len(norm_title)) * 0.6
                        if common > threshold:
                            score = 40
                        else:
                            continue
                    if norm_artist and na == norm_artist:
                        score += 50
                    elif norm_artist and (norm_artist in na or na in norm_artist):
                        score += 30
                    if score > best_score:
                        best_score = score
                        best = s
            except Exception:
                continue

        if best and best_score >= 100:
            return (idx, {
                "id": best.song_id,
                "title": best.title,
                "artist": best.artist,
                "cover": best.cover_url or song.get("cover", ""),
                "duration": best.duration_ms,
                "platform": best.platform,
                "platform_name": {
                    "netease": "网易云", "kugou": "酷狗",
                    "kuwo": "酷我", "qq": "QQ音乐",
                }.get(best.platform, best.platform),
                "matched_from": "ytdl_cross_search",
            })
        return (idx, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_match_one, i, s): i for i, s in enumerate(to_search)}
        for fut in concurrent.futures.as_completed(futures, timeout=30):
            try:
                idx, matched = fut.result()
                if matched is not None:
                    results[idx] = matched
            except Exception:
                pass

    # Rebuild in original order
    rebuilt = []
    for i in range(len(to_search)):
        rebuilt.append(results.get(i, to_search[i]))
    rebuilt.extend(skipped)

    matched = len(results)
    if matched:
        log.info(f"[playlist] Cross-search matched {matched}/{len(to_search)} songs → platform IDs")

    return rebuilt


# ---------------------------------------------------------------------------
# Per-platform playlist fetchers
# ---------------------------------------------------------------------------

def _fetch_qq_playlist(pid: str) -> list[dict]:
    """Fetch a QQ Music playlist by ID. Returns list of standard song dicts."""
    resp = req.get(
        "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
        params={"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": pid,
                "format": "json", "inCharset": "utf8", "outCharset": "utf8"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"},
        timeout=15,
    )
    data = resp.json()
    songs = []
    for cd in data.get("cdlist", []):
        for song in cd.get("songlist", []):
            singers = song.get("singer", [])
            artist = ", ".join(s.get("name", "") for s in singers) if singers else "Unknown"
            albummid = song.get("albummid", "")
            songs.append({
                "id": str(song.get("songid", song.get("id", ""))),
                "title": song.get("songname", song.get("name", "Unknown")),
                "artist": artist,
                "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg" if albummid else "",
                "duration": int(song.get("interval", 0)) * 1000,
                "platform": "qq",
                "platform_name": "QQ音乐",
            })
    return songs


def _fetch_kugou_playlist(pid: str) -> list[dict]:
    """Fetch a Kugou special/songlist by ID."""
    resp = req.get(
        "http://mobilecdn.kugou.com/api/v3/special/song",
        params={"specialid": pid, "page": 1, "pagesize": 500, "format": "json"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    data = resp.json()
    songs = []
    for item in data.get("data", {}).get("info", []):
        songs.append({
            "id": item.get("hash", ""),
            "title": item.get("songname", item.get("filename", "Unknown")),
            "artist": item.get("singername", "Unknown"),
            "cover": item.get("imgUrl", ""),
            "duration": int(item.get("duration", 0)) * 1000,
            "platform": "kugou",
            "platform_name": "酷狗",
        })
    return songs


def _fetch_kuwo_playlist(pid: str) -> list[dict]:
    """Fetch a Kuwo playlist/album by ID."""
    resp = req.get(
        f"http://www.kuwo.cn/api/www/playlist/playListInfo",
        params={"pid": pid, "pn": 1, "rn": 500, "httpsStatus": 1},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://www.kuwo.cn/",
            "csrf": "1", "Cookie": "kw_token=1",
        },
        timeout=15,
    )
    data = resp.json()
    songs = []
    for item in data.get("data", {}).get("musicList", []):
        songs.append({
            "id": str(item.get("rid", "")),
            "title": item.get("name", "Unknown"),
            "artist": item.get("artist", "Unknown"),
            "cover": item.get("pic", item.get("albumpic", "")),
            "duration": int(item.get("duration", 0)) * 1000,
            "platform": "kuwo",
            "platform_name": "酷我",
        })
    return songs


# ---------------------------------------------------------------------------
# Route — universal playlist import
# ---------------------------------------------------------------------------

@app.route("/api/playlist/import")
def api_playlist_import():
    """Import a playlist from a share URL — supports all major music platforms.

    Flow:
      1. Try known URL patterns (NetEase/QQ/Kugou/Kuwo) → direct API fetch.
      2. If no pattern matches, try yt-dlp universal extraction → cross-search
         the first 20 tracks against our platforms for proper song IDs.

    Returns JSON: {"songs": [...], "total": N, "platform": "...", "platform_name": "..."}
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing playlist URL"}), 400

    # ── Pass 1: known platform patterns ──
    parsed = parse_playlist_url(url)

    if parsed:
        platform, pid = parsed
        songs = []
        try:
            if platform == "netease":
                tracks = api.get_playlist_sync(pid)
                for t in tracks:
                    songs.append({
                        "id": t.song_id,
                        "title": t.title,
                        "artist": t.artist,
                        "cover": t.cover_url or "",
                        "duration": t.duration_ms,
                        "platform": "netease",
                        "platform_name": "网易云",
                    })
            elif platform == "qq":
                songs = _fetch_qq_playlist(pid)
            elif platform == "kugou":
                songs = _fetch_kugou_playlist(pid)
            elif platform == "kuwo":
                songs = _fetch_kuwo_playlist(pid)

            if not songs:
                return jsonify({"error": "歌单为空或无法读取"}), 404

            log.info(f"[playlist] Imported {len(songs)} songs from {platform} playlist {pid}")
            return jsonify({
                "songs": songs,
                "total": len(songs),
                "platform": platform,
                "platform_name": {
                    "netease": "网易云", "qq": "QQ音乐",
                    "kugou": "酷狗", "kuwo": "酷我",
                }.get(platform, platform),
            })

        except Exception as e:
            log.error(f"[playlist] Import failed for {platform}/{pid}: {e}")
            return jsonify({"error": f"导入失败：{e}"}), 502

    # ── Pass 2: yt-dlp universal fallback ──
    ytdl_songs, ytdl_err = _extract_playlist_via_ytdlp(url)

    if ytdl_songs:
        # Cross-search first 20 songs to get proper platform IDs
        songs = _cross_search_songs(ytdl_songs)
        matched = sum(1 for s in songs if s.get("matched_from"))
        platform_name = songs[0].get("platform_name", "导入歌单") if songs else "导入歌单"

        log.info(
            f"[playlist] yt-dlp imported {len(songs)} songs "
            f"({matched} cross-matched to platform IDs)"
        )
        return jsonify({
            "songs": songs,
            "total": len(songs),
            "platform": "ytdl",
            "platform_name": platform_name,
            "cross_matched": matched,
        })

    # ── Both passes failed ──
    if ytdl_err:
        return jsonify({"error": ytdl_err}), 400

    return jsonify({
        "error": f"无法识别的歌单链接\n\n支持：{_YTDLP_HINT_PLATFORMS}"
    }), 400


@app.route("/api/lyrics/<platform>/<song_id>")
def api_lyrics(platform, song_id):
    """Get lyrics — try myhkw first, then direct NetEase API."""
    if _is_site_paused():
        return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503
    lrc = myhkw_lyrics(song_id, platform)
    if lrc:
        return jsonify({"lyric": lrc})

    if platform == "netease":
        lrc = api.get_lyrics_sync(song_id)
        return jsonify({"lyric": lrc})
    return jsonify({"lyric": ""})


@app.route("/api/ncm/decrypt", methods=["POST"])
def api_ncm_decrypt():
    """Upload .ncm file → get decrypted audio back."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if not file.filename or not file.filename.lower().endswith(".ncm"):
        return jsonify({"error": "Only .ncm files"}), 400

    try:
        tmp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".ncm")
        file.save(tmp_in.name)
        tmp_in.close()
        result_path = decrypt_ncm(tmp_in.name, str(_TMP_DIR))
        if not result_path:
            return jsonify({"error": "Decryption failed"}), 400
        output = Path(result_path)
        return send_file(str(output), as_attachment=True, download_name=output.name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_in.name)
        except Exception:
            pass
        try:
            if result_path:
                os.unlink(result_path)
        except Exception:
            pass


@app.route("/api/status")
def api_status():
    import time as _time
    return jsonify({
        "authenticated": api.is_authenticated,
        "platforms": list(PLATFORMS.keys()),
        "audio_proxy": "myhkw.cn",
        "dead_apis": ["tonzhon.whamon.com", "xmsj.org", "luckxz.com", "gdstudio.xyz", "QQ音乐API", "酷我API", "咪咕API"],
        "geo_note": "搜索: 网易云直接API + myhkw.cn + 酷狗 | 下载: myhkw.cn 音频代理",
        "paused": _is_site_paused(),
        "search_cache_size": len(_search_cache),
        "audio_cache_size": len(_audio_url_cache),
    })


@app.route("/health")
def health_check():
    """Health check for Docker / monitoring — returns 200 when the app is alive."""
    return jsonify({"status": "ok", "timestamp": int(time.time())}), 200


@app.route("/robots.txt")
def robots_txt():
    """Tell crawlers to index the main page but stay away from API routes."""
    return Response(
        "User-agent: *\n"
        "Allow: /$\n"
        "Allow: /static/\n"
        "Disallow: /api/\n"
        "Disallow: /img/\n",
        content_type="text/plain",
    )


# ---------------------------------------------------------------------------
# Rate limiting — simple in-memory sliding-window per IP
# ---------------------------------------------------------------------------
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_rpm = int(os.environ.get("MD_RATE_LIMIT_RPM", "60"))
_rate_limit_enabled = os.environ.get("MD_RATE_LIMIT", "true").lower() in ("1", "true", "yes")

@app.before_request
def _rate_limit():
    """Reject requests that exceed the per-minute rate limit."""
    if not _rate_limit_enabled:
        return
    if request.path.startswith("/static/") or request.path.startswith("/img/"):
        return
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
    ip = ip.split(",")[0].strip()
    # Local users (the machine running the app) are never rate-limited —
    # the frontend alone fires dozens of requests per minute per page.
    if ip in ("127.0.0.1", "::1", "localhost"):
        return
    now = time.time()
    window = now - 60
    bucket = _rate_limit_store.get(ip, [])
    # Evict expired entries
    bucket = [t for t in bucket if t > window]
    if len(bucket) >= _rate_limit_rpm:
        return jsonify({"error": "请求过于频繁，请稍后再试", "retry_after": 60}), 429
    bucket.append(now)
    _rate_limit_store[ip] = bucket
    # Periodic cleanup: purge stale IP entries every 500 requests
    if len(_rate_limit_store) % 500 == 0:
        for k in list(_rate_limit_store):
            _rate_limit_store[k] = [t for t in _rate_limit_store[k] if t > window]
            if not _rate_limit_store[k]:
                del _rate_limit_store[k]

# ---------------------------------------------------------------------------
# CSRF protection — simple Origin/Referer check for mutating API endpoints
# ---------------------------------------------------------------------------

_CSRF_PROTECTED_PREFIXES = ("/api/extract/", "/api/batch/", "/api/download", "/api/ncm/")

@app.before_request
def _csrf_check():
    """Lightweight CSRF guard: verify Origin/Referer for POST/PUT/DELETE.

    Blocks cross-origin form submissions while allowing direct API calls
    (curl, scripts) that lack Origin/Referer headers.
    In development mode the check is lenient; in production it is strict.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return

    path = request.path
    if not any(path.startswith(p) for p in _CSRF_PROTECTED_PREFIXES):
        return

    # Build allowed-origin set from request host
    host = (request.host or "").split(":")[0]  # strip port
    origin = (request.headers.get("Origin", "") or "").strip()
    referer = (request.headers.get("Referer", "") or "").strip()

    # Both missing → likely a direct API call (curl, script); allow
    if not origin and not referer:
        return

    # Check Origin
    if origin:
        try:
            origin_host = origin.split("://", 1)[1].split(":")[0].split("/")[0]
        except (IndexError, ValueError):
            origin_host = ""
        if origin_host and origin_host != host and origin_host not in ("localhost", "127.0.0.1"):
            return jsonify({"error": "Cross-origin request rejected"}), 403

    # Check Referer
    if referer:
        try:
            ref_host = referer.split("://", 1)[1].split(":")[0].split("/")[0]
        except (IndexError, ValueError):
            ref_host = ""
        if ref_host and ref_host != host and ref_host not in ("localhost", "127.0.0.1"):
            return jsonify({"error": "Cross-origin request rejected"}), 403


# ---------------------------------------------------------------------------
# Visitor tracking middleware
# ---------------------------------------------------------------------------

def _get_request_visitor_id() -> str:
    """Extract a stable anonymous visitor fingerprint from the current request."""
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
        ip = ip.split(",")[0].strip()
        ua = request.headers.get("User-Agent", "")
        return analytics._make_visitor_id(ip, ua)
    except Exception:
        return "unknown"


@app.before_request
def _track_visitor():
    """Record every page / API visit (exclude static files). Uses buffered writes."""
    if request.path.startswith("/static/") or request.path.startswith("/img/"):
        return
    if request.path == "/robots.txt":
        return
    # Also skip pause-status polling (avoids inflating visitor counts)
    if request.path == "/api/admin/pause-status":
        return
    # Skip now-playing heartbeats and per-song stats/comment fetches — these
    # are auto-fired by the frontend and would otherwise bloat visitor counts.
    if request.path in ("/api/play/report", "/api/song/stats", "/api/song/comments"):
        return
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
        ip = ip.split(",")[0].strip()
        ua = request.headers.get("User-Agent", "")
        _enqueue_visitor(ip, ua)
    except Exception:
        pass  # never break the main app for analytics


# ---------------------------------------------------------------------------
# Pause gate — block API routes when site is in maintenance mode
# ---------------------------------------------------------------------------
@app.before_request
def _pause_gate():
    """Block all content-serving API routes when the site is paused.

    Allowed through (even when paused):
        /               — main page (JS checks status to show paused overlay)
        /api/status     — frontend needs to detect pause state
        /api/admin/*    — admin must be able to resume
        /static/*, /img/*, /robots.txt, /health, /favicon.ico
    """
    if not _is_site_paused():
        return

    path = request.path

    # Always allowed paths
    if path == "/" or path == "/api/status" or path == "/health" or path == "/robots.txt":
        return
    if path.startswith("/api/admin/") or path.startswith("/static/") or path.startswith("/img/"):
        return
    if path == "/favicon.ico":
        return

    # Block everything else
    return jsonify({"error": "网站因某些原因暂停使用", "paused": True}), 503


# ---------------------------------------------------------------------------
# Admin API routes
# ---------------------------------------------------------------------------

# ═══════════════════════════════════════════════════════════════════════════
# MULTI-LAYER ADMIN AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: PBKDF2-300K hash storage (password never in plaintext)
# Layer 2: Challenge-response protocol (password never sent over wire)
# Layer 3: Rate limiting (brute-force prevention)
# Layer 4: Timing-safe comparison (side-channel protection)
# Layer 5: XOR-split secret assembly (anti-grep obfuscation)
# Layer 6: HMAC-signed session tokens with expiry
#
# Production override: set MD_ADMIN_PASSWORD env var (plaintext, takes
# precedence over built-in credentials). Use only over HTTPS.

# --- XOR-split secret storage ---
# Each secret is split into 3 byte arrays XOR'd together at runtime.
# No single part is useful alone; grep-ers find nothing.

# FAST_HASH = SHA256(password) — for challenge-response verification
_F0 = bytes.fromhex("f086b198af50442f51ffc6bc50f57dd8b2aee6d1e0f6b0247c795bf60859afc4")
_F1 = bytes.fromhex("4a320c3d7f1bcf354dd110ff8ededd32773abe0b9ec28606469d60dff1426b55")
_F2 = bytes.fromhex("24e0b07415a01faa2f53ebf2d9ef3176f3a1b03d1b96d03ebc19ab262878c3c6")

# PBKDF2 salt parts (XOR → real salt)
_S0 = bytes.fromhex("b5a09428ef46eb359d6e52d91598e40a3b14f0edf11629186241b1882cab7922")
_S1 = bytes.fromhex("e23979847814c1df412ca39738a20527ac27f854b0a938d7ca7233006e593aa2")
_S2 = bytes.fromhex("95cd60e0921493913e6d610250cc3647508b030df72eb32c238560fb68a20081")

# PBKDF2 hash parts (XOR → real 300k-iteration PBKDF2 hash)
_H0 = bytes.fromhex("9a4ee22ba87a6ea63a8fe2c1bd03181e0448edfce59f4b6e4ba2d33261cfd97b")
_H1 = bytes.fromhex("232c318c0606c884ad8959df6dc00b5e49f307e027c72bb78e1df722feabe518")
_H2 = bytes.fromhex("414210913a7463c2f8d205f68fac629ab6c663dccf273c3f95d98111a74748b5")

_PBKDF2_ITERATIONS = 300000

# Admin session state
_admin_nonce_store = {}   # nonce → (timestamp, attempts)
_admin_attempt_ips = {}   # ip → [(timestamp, success)]
_admin_token_key = os.environ.get("MD_ADMIN_SECRET", os.urandom(32))
if isinstance(_admin_token_key, str):
    _admin_token_key = _admin_token_key.encode()

def _xor_bytes(*args):
    """Combine multiple byte arrays via XOR. All must be same length."""
    result = bytearray(len(args[0]))
    for a in args:
        for i in range(len(result)):
            result[i] ^= a[i]
    return bytes(result)

def _get_fast_hash():
    """Reassemble FAST_HASH = SHA256(password) from XOR-split parts.

    When MD_ADMIN_PASSWORD env var is set, derives the hash from it instead.
    """
    env_pwd = os.environ.get("MD_ADMIN_PASSWORD", "")
    if env_pwd:
        return hashlib.sha256(env_pwd.encode()).digest()
    return _xor_bytes(_F0, _F1, _F2)

def _get_pbkdf2_verifier():
    """Reassemble PBKDF2 salt & hash from XOR-split parts.

    Returns (salt, hash) for PBKDF2 verification.
    """
    salt = _xor_bytes(_S0, _S1, _S2)
    stored_hash = _xor_bytes(_H0, _H1, _H2)
    return salt, stored_hash, _PBKDF2_ITERATIONS

def _verify_password(pwd: str) -> bool:
    """Verify a plaintext password against the stored PBKDF2 hash.

    Uses constant-time comparison to prevent timing attacks.
    Environment variable MD_ADMIN_PASSWORD overrides the built-in hash.
    """
    env_pwd = os.environ.get("MD_ADMIN_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(pwd.encode(), env_pwd.encode())

    salt, stored_hash, iters = _get_pbkdf2_verifier()
    computed = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, iters)
    return hmac.compare_digest(computed, stored_hash)

def _admin_make_token() -> str:
    """Create a signed session token: version:timestamp:signature."""
    ts = str(int(time.time()))
    msg = f"v2:{ts}"
    sig = hmac.new(_admin_token_key, msg.encode(), "sha256").hexdigest()
    return f"{msg}:{sig}"

def _admin_verify_token(token: str) -> bool:
    """Verify a session token signature and check expiry (max 8 hours)."""
    parts = token.split(":")
    if len(parts) != 3 or parts[0] != "v2":
        return False
    ts_str, sig = parts[1], parts[2]
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if abs(time.time() - ts) > 28800:  # 8 hours
        return False
    expected = hmac.new(_admin_token_key, f"v2:{ts_str}".encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, expected)

def _admin_rate_check(ip: str) -> tuple[bool, str]:
    """Check rate limits for admin authentication.

    Returns (allowed, reason).
    - Max 5 attempts per IP per minute
    - Max 15 attempts per IP per hour
    - After 15 failures, IP blocked for 1 hour
    """
    now = time.time()
    attempts = _admin_attempt_ips.get(ip, [])

    # Clean old entries
    attempts = [a for a in attempts if now - a[0] < 3600]

    # Check hourly cap
    if len(attempts) >= 15:
        # Check if last attempt was recent (still blocked)
        if now - attempts[-1][0] < 3600:
            return False, "too many attempts, try again later"

    # Check per-minute cap (last 5 in < 60s)
    if len(attempts) >= 5:
        recent = sorted(a[0] for a in attempts[-5:])
        if recent[-1] - recent[0] < 60:
            return False, "too fast, slow down"

    attempts.append((now, False))
    _admin_attempt_ips[ip] = attempts
    return True, "ok"

def _admin_cleanup():
    """Purge expired nonces (older than 5 min) and old IP records (older than 2h)."""
    now = time.time()
    expired_nonces = [n for n, (ts, _) in _admin_nonce_store.items() if now - ts > 300]
    for n in expired_nonces:
        _admin_nonce_store.pop(n, None)
    for ip in list(_admin_attempt_ips.keys()):
        _admin_attempt_ips[ip] = [a for a in _admin_attempt_ips[ip] if now - a[0] < 7200]
        if not _admin_attempt_ips[ip]:
            del _admin_attempt_ips[ip]

# External APIs to monitor
APIS_TO_CHECK = [
    ("myhkw.cn (搜索)", "http://s.myhkw.cn/", 8),
    ("NetEase API", "https://music.163.com/api/search/get", 8),
    ("QQ Music API", "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w=test&n=1&p=1&format=json", 8),
    ("Kugou API", "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=test&page=1&pagesize=1", 10),
    ("Kuwo API", "http://search.kuwo.cn/r.s?all=test&ft=music&pn=0&rn=1&rformat=json", 9),
    ("Migu API", "https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do?text=test&pageNo=1&pageSize=1&searchSwitch={\"song\":1}", 10),
    ("Meting API", "https://api.i-meto.com/meting/api?server=netease&type=search&id=test&limit=1", 10),
    ("GDStudio API", "https://music-api.gdstudio.xyz/api.php?types=search&source=netease&name=test&page=1", 10),
    ("Xiageba API", "https://xiageba.liumingye.cn/api/music/search?q=test&page=1&pageSize=1", 10),
    ("Luckxz", "https://luckxz.com/", 8),
    ("Tonzhon (legacy)", "https://tonzhon.whamon.com/", 8),
]


def _ping_single(name: str, url: str, timeout: int) -> dict:
    """Ping one external API and return its status."""
    try:
        import requests as _r
        start = time.time()
        r = _r.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed = int((time.time() - start) * 1000)
        ok = r.status_code in (200, 301, 302, 307, 308)
        return {"available": ok, "status_code": r.status_code, "response_ms": elapsed}
    except Exception as e:
        return {"available": False, "error": str(e)[:100], "response_ms": 0}


# ---------------------------------------------------------------------------
# Batch download — ZIP all queue songs / lyrics, stream progress + serve zip
# ---------------------------------------------------------------------------

_batch_store: dict[str, str] = {}  # token → temp zip filepath
_batch_store_ts: dict[str, float] = {}  # token → creation timestamp
_batch_cancel: dict[str, threading.Event] = {}  # token → cancel event
_BATCH_TTL = 3600  # 1 hour — stale zips auto-deleted on next request


def _cleanup_batch_store():
    """Remove expired batch ZIPs (older than _BATCH_TTL seconds)."""
    now = time.time()
    stale = [t for t, ts in _batch_store_ts.items() if now - ts > _BATCH_TTL]
    for t in stale:
        path = _batch_store.pop(t, None)
        _batch_store_ts.pop(t, None)
        _batch_cancel.pop(t, None)
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def _download_single_mp3(song: dict) -> tuple[str | None, bytes | None]:
    """Download one song as MP3 bytes with embedded tags.

    Returns ``(filename, audio_bytes)`` or ``(None, None)`` on failure.
    """
    sid = song.get("id", "")
    platform = song.get("platform", "netease")
    title = sanitize_filename(song.get("title", "Unknown"))
    artist = sanitize_filename(song.get("artist", "Unknown"))
    filename = build_filename(artist, title, "mp3")

    url = None
    if platform == "netease":
        url = api.get_song_url_sync(sid, "lossless")
        if not url:
            url = api.get_song_url_sync(sid, "standard")
    if not url and title and title != "Unknown":
        try:
            search_q = f"{title} {artist}" if artist else title
            result = api.search_sync(search_q, limit=5)
            if result and result.songs:
                for ns in result.songs:
                    u = api.get_song_url_sync(ns.song_id, "lossless")
                    if not u: u = api.get_song_url_sync(ns.song_id, "standard")
                    if u: url = u; break
        except Exception: pass

    if not url:
        return None, None

    try:
        audio_data = None
        for hdrs in [
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://music.163.com/"},
            {"User-Agent": "NeteaseMusic/8.0.0", "Referer": "https://music.163.com/"},
        ]:
            try:
                r = req.get(url, timeout=30, headers=hdrs)
                if r.status_code == 200 and len(r.content) > 1024:
                    audio_data = r.content; break
            except Exception: continue
        if not audio_data: return None, None

        # Fetch song detail once for both cover + album
        cover_data = None; cover_mime = "image/jpeg"; album = ""
        try:
            detail = api.get_song_detail_sync(sid)
            if detail:
                if detail.album:
                    album = detail.album
                if detail.cover_url:
                    for hdrs in [
                        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://music.163.com/"},
                        {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
                    ]:
                        try:
                            cr = req.get(detail.cover_url, timeout=10, headers=hdrs)
                            if cr.status_code == 200 and len(cr.content) > 500:
                                cover_data = cr.content
                                if cover_data[:4] == b"\x89PNG": cover_mime = "image/png"
                                break
                        except Exception: continue
        except Exception: pass

        ext = _detect_audio_format(audio_data)
        import tempfile as _tmp
        tf = _tmp.NamedTemporaryFile(delete=False, suffix=".tmp")
        try:
            tf.write(audio_data); tf.close()
            if ext == "flac":
                _embed_flac_tags(tf.name, title, artist, album, cover_data, cover_mime)
            else:
                _embed_mp3_tags(tf.name, title, artist, album, cover_data, cover_mime)
            with open(tf.name, "rb") as f: audio_data = f.read()
        finally:
            try: os.unlink(tf.name)
            except Exception: pass

        return filename, audio_data
    except Exception:
        return None, None


def _download_single_lrc(song: dict) -> tuple[str | None, str | None]:
    """Download one song's LRC lyrics.

    Returns ``(filename, lrc_text)`` or ``(None, None)`` on failure.
    """
    sid = song.get("id", "")
    platform = song.get("platform", "netease")
    title = sanitize_filename(song.get("title", "Unknown"))
    artist = sanitize_filename(song.get("artist", "Unknown"))
    filename = build_filename(artist, title, "lrc")

    lrc_text = ""
    if platform == "netease":
        try: lrc_text = api.get_lyrics_sync(sid)
        except Exception: pass
    if not lrc_text:
        try: lrc_text = myhkw_lyrics(sid, platform)
        except Exception: pass

    if not lrc_text or lrc_text == "[00:00.00] 暂无歌词":
        return None, None
    return filename, lrc_text


@app.route("/api/batch/download-songs", methods=["POST"])
def api_batch_download_songs():
    """Download all songs as MP3, stream progress, return zip via token."""
    data = request.get_json(silent=True) or {}
    songs = data.get("songs", [])
    if not songs:
        return jsonify({"error": "No songs provided"}), 400

    token = secrets.token_hex(16)
    cancel = threading.Event()
    _batch_cancel[token] = cancel
    _batch_store_ts[token] = time.time()
    _cleanup_batch_store()

    def generate():
        try:
            files = []
            success = fail = 0
            for i, song in enumerate(songs):
                if cancel.is_set():
                    yield json.dumps({"cancelled": True}, ensure_ascii=False) + "\n"
                    return
                name, audio = _download_single_mp3(song)
                if name and audio:
                    files.append((name, audio)); success += 1
                else:
                    fail += 1
                yield json.dumps({
                    "index": i, "total": len(songs), "phase": "download",
                    "title": song.get("title", ""),
                    "successCount": success, "failCount": fail,
                }, ensure_ascii=False) + "\n"

            if not files:
                yield json.dumps({"done": True, "error": "All downloads failed"}, ensure_ascii=False) + "\n"
                return

            cancelled = cancel.is_set()

            # Build zip (partial if cancelled)
            yield json.dumps({"phase": "pack", "packing": True}, ensure_ascii=False) + "\n"
            today = datetime.now().strftime("%Y%m%d")
            zip_name = f"歌曲MP3_{today}"
            buf = io.BytesIO()
            seen = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in files:
                    base = name; i = 1
                    while name in seen:
                        stem, ext = base.rsplit(".", 1)
                        name = f"{stem}({i}).{ext}"; i += 1
                    seen.add(name)
                    zf.writestr(name, data)
            buf.seek(0)

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp.write(buf.getvalue())
            tmp.close()
            _batch_store[token] = tmp.name

            yield json.dumps({
                "done": True, "token": token, "filename": zip_name + ".zip",
                "success": success, "fail": fail, "total": len(songs),
                "cancelled": cancelled,
            }, ensure_ascii=False) + "\n"
        finally:
            _batch_cancel.pop(token, None)

    return Response(generate(), mimetype="text/plain; charset=utf-8")


@app.route("/api/batch/download-lyrics", methods=["POST"])
def api_batch_download_lyrics():
    """Download LRC lyrics, stream progress, return zip via token."""
    data = request.get_json(silent=True) or {}
    songs = data.get("songs", [])
    if not songs:
        return jsonify({"error": "No songs provided"}), 400

    token = secrets.token_hex(16)
    cancel = threading.Event()
    _batch_cancel[token] = cancel
    _batch_store_ts[token] = time.time()
    _cleanup_batch_store()

    def generate():
        try:
            files = []
            success = fail = 0
            for i, song in enumerate(songs):
                if cancel.is_set(): break
                name, text = _download_single_lrc(song)
                if name and text:
                    files.append((name, text.encode("utf-8"))); success += 1
                else:
                    fail += 1
                yield json.dumps({
                    "index": i, "total": len(songs), "phase": "download",
                    "title": song.get("title", ""),
                    "successCount": success, "failCount": fail,
                }, ensure_ascii=False) + "\n"

            cancelled = cancel.is_set()
            if not files:
                yield json.dumps({"cancelled": True, "msg": "未下载任何歌词"}, ensure_ascii=False) + "\n"
                return

            yield json.dumps({"phase": "pack", "packing": True}, ensure_ascii=False) + "\n"
            today = datetime.now().strftime("%Y%m%d")
            zip_name = f"歌词LRC_{today}"
            buf = io.BytesIO()
            seen = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in files:
                    base = name; i = 1
                    while name in seen:
                        stem, ext = base.rsplit(".", 1)
                        name = f"{stem}({i}).{ext}"; i += 1
                    seen.add(name)
                    zf.writestr(name, data)
            buf.seek(0)

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp.write(buf.getvalue())
            tmp.close()
            _batch_store[token] = tmp.name

            yield json.dumps({
                "done": True, "token": token, "filename": zip_name + ".zip",
                "success": success, "fail": fail, "total": len(songs),
                "cancelled": cancelled,
            }, ensure_ascii=False) + "\n"
        finally:
            _batch_cancel.pop(token, None)

    return Response(generate(), mimetype="text/plain; charset=utf-8")


@app.route("/api/batch/download-result")
def api_batch_download_result():
    """Serve a completed batch zip by token."""
    token = request.args.get("token", "")
    path = _batch_store.pop(token, None)
    _batch_store_ts.pop(token, None)
    if not path:
        return jsonify({"error": "Not found or expired"}), 404
    try:
        return send_file(path, as_attachment=True,
                         download_name=request.args.get("name", "download.zip"),
                         mimetype="application/zip")
    finally:
        try: os.unlink(path)
        except Exception: pass


@app.route("/api/batch/cancel", methods=["POST"])
def api_batch_cancel():
    """Cancel an in-progress batch download."""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    evt = _batch_cancel.get(token)
    if evt:
        evt.set()
        return jsonify({"cancelled": True})
    return jsonify({"error": "No such batch"}), 404


# ---------------------------------------------------------------------------
# Video Audio Extraction — yt-dlp powered
# ---------------------------------------------------------------------------

@app.route("/api/extract/status")
def api_extract_status():
    """Check if video extraction dependencies are available."""
    has_ytdlp = _HAS_YTDLP
    has_ffmpeg = False
    try:
        from platforms.video_extractor import VideoExtractor
        has_ffmpeg = VideoExtractor._find_ffmpeg() is not None
    except Exception:
        pass
    return jsonify({
        "available": has_ytdlp,
        "yt_dlp": has_ytdlp,
        "ffmpeg": has_ffmpeg,
        "message": "就绪" if (has_ytdlp and has_ffmpeg) else (
            "缺少 yt-dlp" if not has_ytdlp else "缺少 ffmpeg（将下载原始音频格式）"
        ),
    })


@app.route("/api/extract/info", methods=["POST"])
def api_extract_info():
    """Get video metadata from a URL without downloading."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url", "") or "").strip()

    if not url:
        return jsonify({"success": False, "error": "请输入视频链接"}), 400

    if not url.startswith("http"):
        return jsonify({"success": False, "error": "请输入有效的视频链接（以 http:// 或 https:// 开头）"}), 400

    # Quick URL validation
    if len(url) < 10:
        return jsonify({"success": False, "error": "视频链接格式不正确"}), 400

    try:
        extractor = get_extractor()
        info = extractor.get_info(url)
        return jsonify(info)
    except RuntimeError as e:
        return jsonify({"success": False, "error": f"提取器未就绪: {str(e)}"}), 503
    except Exception as e:
        log.error(f"[extract] info failed: {e}")
        return jsonify({"success": False, "error": f"解析失败: {str(e)[:200]}"}), 500


@app.route("/api/extract/download", methods=["POST"])
def api_extract_download():
    """Extract audio or video from a video URL and return the file."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url", "") or "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")
    mode = data.get("mode", "audio")  # 'audio' or 'video'

    if not url:
        return jsonify({"success": False, "error": "请输入视频链接"}), 400

    # Validate format based on mode
    if mode == "video":
        if fmt not in ("mp4", "mkv", "webm"):
            fmt = "mp4"
    else:
        if fmt not in ("mp3", "m4a", "opus", "aac"):
            fmt = "mp3"

    # Clean up stale files
    try:
        from platforms.video_extractor import _cleanup_extract_store
        _cleanup_extract_store()
    except Exception:
        pass

    try:
        extractor = get_extractor()
        result = extractor.extract(
            url,
            mode=mode,
            output_dir=str(_TMP_DIR),
            preferred_format=fmt,
            preferred_quality=quality,
        )

        if not result.get("success"):
            error_msg = result.get("error", "未知错误")
            return jsonify({"success": False, "error": error_msg}), 400

        # Generate a token and store the filepath
        token = secrets.token_hex(16)
        filepath = result["filepath"]

        raw_title = result.get("title", "audio")
        raw_artist = result.get("uploader", "") or ""

        from platforms.video_extractor import _extract_store, _extract_store_ts, _extract_store_info
        _extract_store[token] = filepath
        _extract_store_ts[token] = time.time()
        _extract_store_info[token] = {
            "title": raw_title,
            "artist": raw_artist,
            "ext": result.get("ext", "mp3"),
            "filesize": result.get("filesize", 0),
            "duration": result.get("duration", 0),
        }

        # Track download
        try:
            analytics.track_download(
                result.get("id", token),
                raw_title,
                raw_artist or "Unknown",
                result.get("extractor", "video"),
                "extract",
                True,
                _get_request_visitor_id(),
            )
        except Exception:
            pass

        filename = sanitize_filename(raw_title)
        artist = sanitize_filename(raw_artist) if raw_artist else ""

        # Build download filename: "上传者 - 标题.ext" when uploader is known,
        # otherwise just "标题.ext"
        ext = result.get("ext", "mp3")
        if artist:
            download_filename = f"{artist} - {filename}.{ext}"
        else:
            download_filename = f"{filename}.{ext}"

        return jsonify({
            "success": True,
            "token": token,
            "title": result["title"],
            "artist": raw_artist,
            "duration": result.get("duration", 0),
            "filesize": result.get("filesize", 0),
            "ext": ext,
            "filename": download_filename,
            "thumbnail": result.get("thumbnail", ""),
        })

    except RuntimeError as e:
        return jsonify({"success": False, "error": f"提取器未就绪: {str(e)}"}), 503
    except Exception as e:
        log.error(f"[extract] download failed: {e}")
        return jsonify({"success": False, "error": f"提取失败: {str(e)[:200]}"}), 500


@app.route("/api/extract/result")
def api_extract_result():
    """Serve the extracted audio file by token."""
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "Missing token"}), 400

    try:
        from platforms.video_extractor import _extract_store, _extract_store_ts, _extract_store_info
    except Exception:
        return jsonify({"error": "Server error"}), 500

    filepath = _extract_store.get(token)
    info = _extract_store_info.get(token, {})

    if not filepath or not os.path.isfile(filepath):
        # Clean up stale entry
        _extract_store.pop(token, None)
        _extract_store_ts.pop(token, None)
        _extract_store_info.pop(token, None)
        return jsonify({"error": "文件已过期或不存在，请重新提取"}), 404

    title = sanitize_filename(info.get("title", "audio"))
    raw_artist = info.get("artist", "") or ""
    artist = sanitize_filename(raw_artist) if raw_artist else ""
    ext = info.get("ext", "mp3")

    if artist:
        full_name = f"{artist} - {title}"
    else:
        full_name = title
    mime = {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "opus": "audio/opus",
        "aac": "audio/aac",
    }.get(ext, "audio/mpeg")

    try:
        return send_file(
            filepath,
            as_attachment=True,
            download_name=f"{full_name}.{ext}",
            mimetype=mime,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/challenge")
def api_admin_challenge():
    """Generate a cryptographic nonce for challenge-response auth.

    The client must compute: SHA256(nonce + SHA256(password))
    and send it to /api/admin/verify. The plaintext password is
    never transmitted over the network.
    """
    _admin_cleanup()
    nonce = secrets.token_hex(32)
    _admin_nonce_store[nonce] = (time.time(), 0)
    return jsonify({"nonce": nonce})


@app.route("/api/admin/verify", methods=["POST"])
def api_admin_verify():
    """Verify admin authentication via challenge-response.

    Request: {"proof": "hex", "nonce": "hex"}
    - proof = SHA256(nonce + SHA256(password))
    - nonce from /api/admin/challenge

    Returns a signed session token on success.
    """
    _admin_cleanup()
    ip = request.remote_addr or "127.0.0.1"

    # Layer 3: Rate limiting
    allowed, reason = _admin_rate_check(ip)
    if not allowed:
        log.warning(f"[admin] Rate limit blocked IP {ip}: {reason}")
        return jsonify({"success": False, "error": "请求太频繁，请稍后再试"}), 429

    data = request.get_json(silent=True) or {}
    proof = data.get("proof", "")
    nonce = data.get("nonce", "")

    if not proof or not nonce:
        return jsonify({"success": False, "error": "认证参数不完整"}), 400

    # Validate nonce
    nonce_entry = _admin_nonce_store.get(nonce)
    if not nonce_entry:
        return jsonify({"success": False, "error": "验证会话已过期，请重试"}), 403

    nonce_ts, nonce_attempts = nonce_entry
    now = time.time()

    # Nonce expires in 5 minutes
    if now - nonce_ts > 300:
        _admin_nonce_store.pop(nonce, None)
        return jsonify({"success": False, "error": "验证会话已过期，请重试"}), 403

    # Max 3 attempts per nonce
    if nonce_attempts >= 3:
        _admin_nonce_store.pop(nonce, None)
        return jsonify({"success": False, "error": "验证失败次数过多，请刷新重试"}), 403

    _admin_nonce_store[nonce] = (nonce_ts, nonce_attempts + 1)

    # Layer 2: Challenge-response verification
    # Expected: SHA256(nonce || hex(SHA256(password)))
    fast_hash_hex = _get_fast_hash().hex()
    expected = hashlib.sha256(nonce.encode() + fast_hash_hex.encode()).hexdigest()

    # Layer 4: Timing-safe comparison
    if not hmac.compare_digest(proof, expected):
        # Record failed attempt
        if ip in _admin_attempt_ips:
            _admin_attempt_ips[ip][-1] = (_admin_attempt_ips[ip][-1][0], False)
        log.warning(f"[admin] Failed auth attempt from {ip}")
        return jsonify({"success": False, "error": "密码错误"}), 403

    # Success — consume the nonce (prevent replay)
    _admin_nonce_store.pop(nonce, None)

    # Record success
    if ip in _admin_attempt_ips:
        _admin_attempt_ips[ip] = _admin_attempt_ips[ip][:-1]  # clear rate-limit record

    # Layer 6: Issue signed session token
    token = _admin_make_token()
    log.info(f"[admin] Successful auth from {ip}")
    return jsonify({"success": True, "token": token})


def _require_admin_token(f):
    """Decorator: require valid admin session token via Authorization header."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        else:
            token = request.args.get("token", "")
        if not token or not _admin_verify_token(token):
            return jsonify({"error": "未授权访问", "code": "unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


@app.route("/api/admin/stats")
@_require_admin_token
def api_admin_stats():
    """Return all analytics data for the dashboard."""
    try:
        stats = analytics.get_all_stats()
        # Add latest API status
        stats["api_status"] = analytics.get_latest_api_status()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/recent-downloads")
@_require_admin_token
def api_admin_recent_downloads():
    """Return paginated recent download records (max 200 total).

    Query params:
        limit  — records per page (default 40, max 40)
        offset — starting offset (default 0)
    """
    try:
        limit = request.args.get("limit", 40, type=int)
        offset = request.args.get("offset", 0, type=int)
        # Cap: max 40 per page, max offset 160 (so max 200 records total)
        limit = max(1, min(limit, 40))
        offset = max(0, min(offset, 160))
        downloads = analytics.get_recent_downloads(limit, offset)
        total = analytics.get_recent_downloads_count()
        return jsonify({
            "downloads": downloads,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total and (offset + limit) < 200,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/api-check")
@_require_admin_token
def api_admin_check():
    """Ping all external music APIs and return their status (batch)."""

    results = {}
    for name, url, timeout in APIS_TO_CHECK:
        result = _ping_single(name, url, timeout)
        results[name] = result
        analytics.record_api_check(name, result["available"], result.get("response_ms", 0))

    available = sum(1 for r in results.values() if r["available"])
    total = len(results)

    return jsonify({
        "apis": results,
        "available": available,
        "total": total,
        "ratio": f"{available}/{total}",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/admin/api-check-one")
@_require_admin_token
def api_admin_check_one():
    """Ping a single external API and return its status (for progressive updates)."""
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "Missing name"}), 400

    for n, url, timeout in APIS_TO_CHECK:
        if n == name:
            result = _ping_single(n, url, timeout)
            analytics.record_api_check(name, result["available"], result.get("response_ms", 0))
            return jsonify({"name": name, "result": result})

    return jsonify({"error": f"Unknown API: {name}"}), 404


@app.route("/api/admin/pause", methods=["POST"])
@_require_admin_token
def api_admin_pause():
    """Pause the site — visitors see only the header bar."""
    global _site_paused, _site_pause_time
    _site_paused = True
    _site_pause_time = time.time()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    log.info(f"[admin] Site PAUSED by {ip} at {datetime.now().isoformat(timespec='seconds')}")
    return jsonify({"success": True, "paused": True, "paused_at": datetime.now().isoformat(timespec="seconds")})


@app.route("/api/admin/resume", methods=["POST"])
@_require_admin_token
def api_admin_resume():
    """Resume normal site operation."""
    global _site_paused, _site_pause_time
    _site_paused = False
    _site_pause_time = 0.0
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    log.info(f"[admin] Site RESUMED by {ip} at {datetime.now().isoformat(timespec='seconds')}")
    return jsonify({"success": True, "paused": False})


@app.route("/api/admin/pause-status")
def api_admin_pause_status():
    """Public endpoint: check if site is paused (no auth needed)."""
    return jsonify({
        "paused": _is_site_paused(),
        "paused_at": datetime.fromtimestamp(_site_pause_time).isoformat(timespec="seconds") if _site_pause_time else None,
    })


@app.after_request
def add_security_headers(response):
    """Add basic security headers (safe in development)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "media-src 'self' https:; "
        "connect-src 'self' https:; "
        "font-src 'self' data:;",
    )
    # HSTS: only set when behind HTTPS (check X-Forwarded-Proto)
    if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# ---------------------------------------------------------------------------
# Visitor buffer flush worker (daemon thread)
# ---------------------------------------------------------------------------
def _visitor_flush_worker():
    """Background daemon that periodically flushes the visitor buffer."""
    import time as _time
    while True:
        _time.sleep(_VISITOR_FLUSH_INTERVAL)
        try:
            _flush_visitor_buffer()
        except Exception:
            pass


_visitor_flush_thread = threading.Thread(target=_visitor_flush_worker, daemon=True)
_visitor_flush_thread.start()


def _now_playing_cleanup_worker():
    """Background daemon that prunes expired now-playing entries."""
    import time as _time
    while True:
        _time.sleep(30)
        try:
            _prune_now_playing()
        except Exception:
            pass


_now_playing_cleanup_thread = threading.Thread(target=_now_playing_cleanup_worker, daemon=True)
_now_playing_cleanup_thread.start()

# Final flush on graceful shutdown
import atexit as _atexit


@_atexit.register
def _final_flush():
    _flush_visitor_buffer()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _print_warning_banner()
    import argparse
    p = argparse.ArgumentParser(description="Music Downloader Web UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7860)))
    p.add_argument("--api-base", default="", help="Custom API base for geo-unblock")
    p.add_argument("--cookie", "-c", default="")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    mod = sys.modules[__name__]
    if args.api_base:
        mod.api = NeteaseAPI(api_base=args.api_base)
    if args.cookie:
        mod.api.import_cookie_string(args.cookie)

    env_label = "PRODUCTION (Render)" if IS_PRODUCTION else "DEVELOPMENT"
    print(f"""
╔══════════════════════════════════════════════╗
║      音乐下载器 Web UI v3.0                   ║
║      支持: 网易云/QQ/酷狗/酷我/咪咕等12平台    ║
╠══════════════════════════════════════════════╣
║  模式: {env_label:37s} ║
║  地址: http://{args.host}:{args.port}                  ║
╚══════════════════════════════════════════════╝
    """)
    app.run(host=args.host, port=args.port, debug=args.debug)
