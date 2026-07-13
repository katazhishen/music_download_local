#!/usr/bin/env python3
"""
Music Downloader — Web UI with multi-platform support.
Search powered by myhkw.cn + NetEase direct API + multiple fallbacks.
Audio download via myhkw.cn proxy (replaces dead tonzhon.whamon.com).

Usage:
    pip install flask requests
    python web.py
    # Open http://127.0.0.1:5000
"""

import os, sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

try:
    import requests as req
    from flask import Flask, render_template, request, jsonify, send_file, Response

    from core.utils import log, sanitize_filename, build_filename, format_duration
    from platforms.netease import NeteaseAPI, decrypt_ncm, parse_netease_url
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
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True  # always use latest template

api = NeteaseAPI()
DOWNLOAD_DIR = Path.cwd()


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
    "netease":  {"name": "网易云", "icon": "🎵"},
    "kugou":    {"name": "酷狗",   "icon": "🐶"},
    "kuwo":     {"name": "酷我",   "icon": "🎤"},
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


def search_kugou(query: str, platform: str = "kugou", page: int = 1) -> dict:
    """Search via Kugou mobile API (works globally)."""
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
            songs.append({
                "id": item.get("hash", ""),
                "title": item.get("songname", "Unknown"),
                "artist": item.get("singername", "Unknown"),
                "cover": item.get("imgUrl", "") or "",  # may be empty, frontend will retry
                "heat": item.get("ownercount", 0),  # listen/owner count
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
                    "http://music.163.com/api/song/detail",
                    params={"id": need_cover_ids[0], "ids": ids_str},
                    headers={
                        "User-Agent": "Mozilla/5.0",
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
                    f"http://music.163.com/api/v1/resource/comments/R_SO_4_{sid}",
                    params={"limit": 0},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
                    timeout=8,
                )
                return sid, r.json().get("total", 0)
            except Exception:
                return sid, 0

        all_ids = [s["id"] for s in songs]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_fetch_comment_count, sid): sid for sid in all_ids}
            for fut in concurrent.futures.as_completed(futures, timeout=15):
                try:
                    sid, count = fut.result()
                    for sng in songs:
                        if sng["id"] == sid and count > 0:
                            sng["heat"] = count
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
# Multi-source search framework — add new sites here
# ---------------------------------------------------------------------------
import re

# Source: (name, search_fn, platforms_supported)
SEARCH_SOURCES: list[tuple[str, callable, list[str]]] = []

def _register_sources():
    """Register all search sources in priority order. Add new sites here."""
    SEARCH_SOURCES.clear()

    # Source 1: direct NetEase API — best quality (covers, duration)
    SEARCH_SOURCES.append(("direct", search_direct, ["netease"]))

    # Source 2: myhkw netease — additional results with audio proxy URLs
    SEARCH_SOURCES.append(("myhkw_ne", search_myhkw, ["netease"]))

    # Source 3: Kugou native API — for kugou platform tab
    SEARCH_SOURCES.append(("kugou", search_kugou, ["kugou"]))

    # Source 4: Kuwo (酷我音乐) search API
    SEARCH_SOURCES.append(("kuwo", search_kuwo, ["kuwo"]))

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


def search_all_sources(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search across all configured sources, merge and dedup results."""
    import concurrent.futures

    all_songs = []
    max_total = 0
    results_by_source = {}  # name → [songs]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {}
        for name, fn, platforms in SEARCH_SOURCES:
            if platform in platforms:
                futures[ex.submit(fn, query, platform, page)] = name

        for fut in concurrent.futures.as_completed(futures, timeout=30):
            name = futures[fut]
            try:
                result = fut.result()
                if result.get("songs"):
                    results_by_source[name] = result["songs"]
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
    return {"songs": deduped[:20], "total": display_total, "error": None if deduped else "No results from any source"}


# Register sources now that all functions are defined
_register_sources()


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
    return render_template("index.html", platforms=PLATFORMS)


@app.route("/api/search")
def api_search():
    """Unified search across platforms."""
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "netease")
    page = request.args.get("page", 1, type=int)
    if not q:
        return jsonify({"error": "Missing query"}), 400

    # Search all configured sources, merge & dedup
    result = search_all_sources(q, platform, page)
    return jsonify(result)


@app.route("/api/song/<platform>/<song_id>")
def api_song_detail(platform, song_id):
    """Get song detail with cover URL. Cross-searches NetEase if needed."""
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

    # For qq/kugou: cross-search netease for cover
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

    # Fallback to direct NetEase API
    if platform == "netease":
        detail = api.get_song_detail_sync(song_id)
        if detail:
            return jsonify({
                "id": detail.song_id,
                "title": detail.title,
                "artist": detail.artist,
                "cover": detail.cover_url,
                "lyric": "",
                "url": "",
                "link": f"https://music.163.com/#/song?id={detail.song_id}",
            })

    return jsonify({"error": "Song not found"}), 404


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
    """Detect audio format from magic bytes. Returns 'mp3', 'flac', or 'mp3'."""
    if data[:4] == b"fLaC":
        return "flac"
    # MP3: ID3 tag header or MPEG sync bytes
    if data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    # Default to mp3
    return "mp3"


def _embed_mp3_tags(
    filepath: str, title: str, artist: str, album: str,
    cover_data: bytes | None, cover_mime: str,
):
    """Embed ID3v2 tags into an MP3 file."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC

    audio = MP3(filepath, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    if album:
        audio.tags.add(TALB(encoding=3, text=album))
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


def _is_purely_structural(text: str) -> bool:
    """Return True if text is just spaces/dashes/separators (not translatable)."""
    cleaned = text.strip().replace(" ", "").replace("-", "").replace("~", "").replace("·", "")
    return len(cleaned) == 0


def _batch_translate(texts: list[str], target_lang: str) -> list[str]:
    """Translate a list of strings using Google Translate via deep-translator.

    Joins with `` ||| `` delimiter for batch efficiency (fewer API calls).
    Falls back to line-by-line translation if batch fails.
    """
    if not texts:
        return []

    lang_map = {"zh": "chinese (simplified)", "en": "english"}
    target_full = lang_map.get(target_lang, target_lang)
    delimiter = " ||| "

    results = []
    chunk_size = 50

    for chunk_start in range(0, len(texts), chunk_size):
        chunk = texts[chunk_start:chunk_start + chunk_size]

        # Try batch translation first
        try:
            joined = delimiter.join(chunk)
            translator = GoogleTranslator(source="auto", target=target_full)
            translated_joined = translator.translate(joined)
            if translated_joined:
                parts = translated_joined.split(delimiter)
                if len(parts) == len(chunk):
                    results.extend(parts)
                    continue
        except Exception:
            pass

        # Fallback: translate each line individually
        for text in chunk:
            try:
                translator = GoogleTranslator(source="auto", target=target_full)
                result = translator.translate(text)
                results.append(result if result else text)
            except Exception:
                results.append(text)

    return results


def translate_lrc(lrc_text: str, target_lang: str) -> str:
    """Translate LRC lyrics, preserving all timestamps and metadata tags.

    Only the text portions are translated; ``[mm:ss.xx]`` brackets and
    metadata tags like ``[ti:...]`` / ``[ar:...]`` are kept intact.
    """
    if not _HAS_TRANSLATOR:
        return lrc_text
    if not lrc_text or not lrc_text.strip():
        return lrc_text

    lines = lrc_text.replace("\r\n", "\n").split("\n")

    # Collect translatable texts and their positions
    texts_to_translate = []
    # Each entry: (line_index, is_timestamp, prefix, original_text)
    line_map = []

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
            if text and not _is_purely_structural(text):
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
            if value and not _is_purely_structural(value):
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

        # Get metadata from Netease API for richer tags
        album = ""
        cover_url = ""
        if platform == "netease" or True:
            detail = api.get_song_detail_sync(song_id)
            if detail:
                if detail.album:
                    album = detail.album
                if detail.cover_url:
                    cover_url = detail.cover_url

        # Download cover image
        cover_data = None
        cover_mime = "image/jpeg"
        if cover_url:
            try:
                cr = req.get(cover_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if cr.status_code == 200 and len(cr.content) > 500:
                    cover_data = cr.content
                    if cover_data[:4] == b"\x89PNG":
                        cover_mime = "image/png"
            except Exception:
                pass

        # Write audio to temp file for mutagen processing
        tf = _tmp.NamedTemporaryFile(delete=False, suffix=".mp3")
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
        os.unlink(tf.name)
    except Exception:
        pass

    from urllib.parse import quote
    # HTTP headers are Latin-1 only. filename= must be ASCII;
    # filename*= (RFC 5987) handles Unicode.
    full_name = f"{artist} - {title}"
    ext = "flac" if _detect_audio_format(mp3_data) == "flac" else "mp3"
    mime_type = "audio/flac" if ext == "flac" else "audio/mpeg"
    return Response(mp3_data, content_type=mime_type,
        headers={"Content-Disposition": _make_content_disp(full_name, ext)})


@app.route("/api/download/<platform>/<song_id>")
def api_download(platform, song_id):
    """Download MP3 — loops through all API sources with retries until success."""
    import time
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    name = f"{artist} - {title}" if title else song_id
    artist = artist or "Unknown"
    title = title or "Unknown"

    # Each strategy returns (Response, None) on success or (None, str_error) on failure
    def try_myhkw_cached():
        """Strategy 1: Use myhkw proxy URL from search results."""
        cached_url = request.args.get("url", "")
        if not cached_url:
            return None, "no cached url"
        from platforms.myhkw_api import _resolve_proxy_url
        full_url = _resolve_proxy_url(cached_url)
        if not full_url:
            return None, "resolve_proxy failed"
        resp = _download_mp3_from_cdn(full_url, artist, title, song_id, platform)
        return (resp, None) if resp else (None, "cdn download failed")

    def try_myhkw_by_id():
        """Strategy 2: Search myhkw by NetEase song ID."""
        cdn_url = resolve_song_url(song_id, platform)
        if not cdn_url:
            return None, "resolve by id failed"
        resp = _download_mp3_from_cdn(cdn_url, artist, title, song_id, platform)
        return (resp, None) if resp else (None, "cdn download failed")

    def try_myhkw_by_keyword():
        """Strategy 3: Search myhkw by title + artist keyword."""
        if not title or title == "Unknown":
            return None, "no title to search"
        result = resolve_song_by_keyword(title, artist)
        if not result:
            return None, "keyword search failed"
        cdn_url, matched_id = result
        resp = _download_mp3_from_cdn(cdn_url, artist, title, matched_id, platform)
        return (resp, None) if resp else (None, "cdn download failed")

    def try_netease_direct():
        """Strategy 4: Direct NetEase API (geo-restricted)."""
        if platform != "netease":
            return None, "not netease"
        url = api.get_song_url_sync(song_id, "standard")
        if not url:
            return None, "netease direct no url"
        return (stream_download(url, artist, title, "standard"), None)

    strategies = [
        ("myhkw_cached", try_myhkw_cached),
        ("myhkw_by_id", try_myhkw_by_id),
        ("myhkw_keyword", try_myhkw_by_keyword),
        ("netease_direct", try_netease_direct),
    ]

    max_rounds = 2  # loop all strategies up to 2 times
    errors = []

    for round_num in range(1, max_rounds + 1):
        for strategy_name, strategy_fn in strategies:
            for attempt in (1, 2):
                try:
                    resp, err = strategy_fn()
                    if resp:
                        log.info(f"[download] SUCCESS: {strategy_name} (round={round_num}, attempt={attempt})")
                        return resp
                    errors.append(f"[R{round_num}/A{attempt}] {strategy_name}: {err}")
                except Exception as e:
                    errors.append(f"[R{round_num}/A{attempt}] {strategy_name}: {type(e).__name__}: {e}")
                if attempt == 1:
                    time.sleep(0.5)  # brief pause between attempts
            time.sleep(0.3)  # brief pause between strategies
        if round_num < max_rounds:
            log.info(f"[download] Round {round_num} failed, retrying all strategies...")
            time.sleep(1)

    log.error(f"[download] ALL FAILED for {name}: {'; '.join(errors[-10:])}")
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


@app.route("/api/lrc/<platform>/<song_id>")
def api_lrc_download(platform, song_id):
    """Download LRC lyrics file. Named same as the song."""
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


@app.route("/api/lyrics/<platform>/<song_id>")
def api_lyrics(platform, song_id):
    """Get lyrics — try myhkw first, then direct NetEase API."""
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
        result_path = decrypt_ncm(tmp_in.name, str(DOWNLOAD_DIR))
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


@app.route("/api/status")
def api_status():
    return jsonify({
        "authenticated": api.is_authenticated,
        "platforms": list(PLATFORMS.keys()),
        "audio_proxy": "myhkw.cn",
        "dead_apis": ["tonzhon.whamon.com", "xmsj.org", "luckxz.com", "gdstudio.xyz", "QQ音乐API", "酷我API", "咪咕API"],
        "geo_note": "搜索: 网易云直接API + myhkw.cn + 酷狗 | 下载: myhkw.cn 音频代理",
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Music Downloader Web UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--api-base", default="", help="Custom API base for geo-unblock")
    p.add_argument("--cookie", "-c", default="")
    p.add_argument("--output", "-o", default=".")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    mod = sys.modules[__name__]
    mod.DOWNLOAD_DIR = Path(args.output).resolve()
    mod.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if args.api_base:
        mod.api = NeteaseAPI(api_base=args.api_base)
    if args.cookie:
        mod.api.import_cookie_string(args.cookie)

    print(f"""
╔══════════════════════════════════════════════╗
║      音乐下载器 Web UI v2.0                   ║
║      支持: 网易云/QQ/酷狗/酷我/咪咕等12平台    ║
╠══════════════════════════════════════════════╣
║  地址: http://{args.host}:{args.port}                  ║
║  输出: {str(DOWNLOAD_DIR)[:35]:35s} ║
╚══════════════════════════════════════════════╝
    """)
    app.run(host=args.host, port=args.port, debug=args.debug)
