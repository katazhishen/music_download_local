#!/usr/bin/env python3
"""
Music Downloader — Web UI with multi-platform support.
Uses xmsj.org as search proxy (works globally), plus direct API fallback.

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
    from platforms.tonzhon_api import resolve_song_url, resolve_song_url_raw, get_lyrics as tonzhon_lyrics
except ImportError as e:
    print(f"缺少依赖: {e}")
    print("请运行: pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml")
    sys.exit(1)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True  # always use latest template

api = NeteaseAPI()
DOWNLOAD_DIR = Path.cwd()

# ---------------------------------------------------------------------------
# Multi-platform search via xmsj.org proxy API
# ---------------------------------------------------------------------------
XMSJ_URL = "http://xmsj.org/"
PLATFORMS = {
    "netease":  {"name": "网易云", "icon": "🎵"},
    "qq":       {"name": "QQ音乐", "icon": "🐧"},
    "kugou":    {"name": "酷狗",   "icon": "🐶"},
}


# XMSJ-like sites (same open-source project: github.com/maicong/music)
_XMSJ_SITES = [
    ("xmsj", "http://xmsj.org/"),
    ("myhkw", "http://s.myhkw.cn/"),
]


def _search_xmsj_like(base_url: str, source_name: str, query: str, platform: str = "netease", page: int = 1) -> dict:
    """Generic search for xmsj-based sites (maicong/music project).
    Always queries netease internally since other platforms may not be supported."""
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
                songs.append({
                    "id": str(item.get("songid", "")),
                    "title": title,
                    "artist": artist,
                    "cover": item.get("pic") or item.get("cover", ""),
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


def search_xmsj(query: str, platform: str = "netease", page: int = 1) -> dict:
    """Search via xmsj.org."""
    return _search_xmsj_like("http://xmsj.org/", "xmsj", query, platform, page)


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
                "cover": "",
                "lyric": "",
                "url": "",
                "link": f"https://www.kugou.com/song/#hash={item.get('hash','')}",
                "platform": "kugou",
                "platform_name": "酷狗",
                "source": "kugou",
                "filename": item.get("filename", ""),  # e.g. "周杰伦 - 晴天"
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
        for s in result.songs:
            songs.append({
                "id": s.song_id,
                "title": s.title,
                "artist": s.artist,
                "cover": s.cover_url or "",
                "lyric": "",
                "url": "",
                "link": f"https://music.163.com/#/song?id={s.song_id}",
                "platform": "netease",
                "platform_name": "网易云",
                "source": "direct",
            })
        return {"songs": songs, "total": result.total, "error": None}
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

    # Source 1: myhkw QQ search (works!) — for qq platform
    SEARCH_SOURCES.append(("myhkw", search_myhkw, ["qq"]))

    # Source 2: Kugou native API — for kugou platform
    SEARCH_SOURCES.append(("kugou", search_kugou, ["kugou"]))

    # Source 3: direct NetEase API — for netease
    SEARCH_SOURCES.append(("direct", search_direct, ["netease"]))

    # Source 4: myhkw netease — additional netease results
    SEARCH_SOURCES.append(("myhkw_ne", search_myhkw, ["netease"]))

    # Source 5: luckxz.com (幸运小猪) — generic
    SEARCH_SOURCES.append(("luckxz", search_luckxz, ["netease"]))

    # Source 6: xiageba (下歌吧) — generic
    SEARCH_SOURCES.append(("xiageba", search_xiageba, ["netease"]))

def _normalize(text: str) -> str:
    """Normalize text for dedup: lowercase, strip punctuation/spaces."""
    text = re.sub(r'[^\w\s]', '', text.lower())
    return re.sub(r'\s+', ' ', text).strip()


def _dedup_songs(all_songs: list[dict]) -> list[dict]:
    """Remove duplicate songs across sources. Keeps first occurrence (highest priority)."""
    seen = set()
    result = []
    for s in all_songs:
        key = (_normalize(s["title"]), _normalize(s["artist"]))
        if key not in seen and s["title"] != "Unknown":
            seen.add(key)
            result.append(s)
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
    """Get song detail — try xmsj.org first, then direct API."""
    # For now, search by ID via xmsj
    try:
        resp = req.post(
            XMSJ_URL,
            data={"input": song_id, "filter": "id", "type": platform, "page": 1},
            headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", "Referer": XMSJ_URL},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            item = data["data"][0]
            return jsonify({
                "id": str(item.get("songid", "")),
                "title": item.get("title", "Unknown"),
                "artist": item.get("author", "Unknown"),
                "cover": item.get("pic", ""),
                "lyric": item.get("lrc", ""),
                "url": item.get("url", ""),
                "link": item.get("link", ""),
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


@app.route("/api/download/<platform>/<song_id>")
def _cross_search_netease(title: str, artist: str) -> tuple[str, str, str] | None:
    """Find a matching song on NetEase. Returns (song_id, artist, title) or None."""
    try:
        q = f"{title} {artist}" if artist else title
        result = api.search_sync(q, limit=3)
        if result.songs:
            for ns in result.songs:
                cdn_url = resolve_song_url(ns.song_id, "netease")
                if cdn_url:
                    return (ns.song_id, ns.artist, ns.title)
    except Exception:
        pass
    return None


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
            if r.status_code == 200 and (r.content[:3] == b"ID3" or r.content[:4] == b"fLaC"):
                mp3_data = r.content
                break
        except Exception:
            continue

    if not mp3_data:
        return None

    # Embed cover
    try:
        cover_url = ""
        if platform == "netease" or True:  # always try netease detail for cover
            detail = api.get_song_detail_sync(song_id)
            if detail and detail.cover_url:
                cover_url = detail.cover_url
        if cover_url:
            cr = req.get(cover_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if cr.status_code == 200 and len(cr.content) > 500:
                from mutagen.mp3 import MP3
                from mutagen.id3 import ID3, APIC
                import tempfile as _tmp
                tf = _tmp.NamedTemporaryFile(delete=False, suffix=".mp3")
                tf.write(mp3_data); tf.close()
                audio = MP3(tf.name, ID3=ID3)
                if audio.tags is None: audio.add_tags()
                mime = "image/png" if cr.content[:4] == b"\x89PNG" else "image/jpeg"
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cr.content))
                audio.save()
                with open(tf.name, "rb") as f: mp3_data = f.read()
                os.unlink(tf.name)
    except Exception:
        pass

    from urllib.parse import quote
    safe = f"{artist} - {title}".encode("ascii", "ignore").decode().strip() or "song"
    safe = safe.replace('"', '').replace("'", "")[:80]
    return Response(mp3_data, content_type="audio/mpeg",
        headers={"Content-Disposition": f"attachment; filename=\"{safe}.mp3\"; filename*=UTF-8''{quote(f'{artist} - {title}.mp3')}"})


@app.route("/api/download/<platform>/<song_id>")
def api_download(platform, song_id):
    """Download MP3. Cross-searches NetEase for non-netease platforms."""
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    name = f"{artist} - {title}" if title else song_id

    try:
        # Get netease song ID (cross-search if needed)
        ne_id = song_id if platform == "netease" else None
        if not ne_id:
            matched = _cross_search_netease(title, artist)
            if matched:
                ne_id, artist, title = matched
                log.info(f"Cross-matched: {platform}/{song_id} → netease/{ne_id}")

        # Try Tonzhon download
        if ne_id:
            cdn_url = resolve_song_url(ne_id, "netease")
            if cdn_url:
                resp = _download_mp3_from_cdn(cdn_url, artist or "Unknown", title or "Unknown", ne_id, "netease")
                if resp:
                    return resp

        # Try direct API (netease only)
        if platform == "netease":
            url = api.get_song_url_sync(song_id, "standard")
            if url:
                return stream_download(url, artist or "Unknown", title or "Unknown", "standard")

        return jsonify({
            "error": "无法下载",
            "detail": f"《{name}》在所有音源中未找到可下载版本。",
            "solutions": [
                {"title": "换到网易云平台搜索同名歌曲再下载"},
                {"title": "部署 Vercel 代理", "url": "https://github.com/Binaryify/NeteaseCloudMusicApi",
                 "note": "python web.py --api-base https://你的项目.vercel.app"},
            ]
        }), 403
    except Exception as e:
        log.error(f"Download error: {e}")
        return jsonify({"error": str(e)}), 500


def stream_download(url: str, artist: str, title: str, quality: str):
    """Stream a direct download URL to the browser."""
    ext = "flac" if quality in ("lossless", "hires") else "mp3"
    filename = build_filename(artist, title, ext)

    resp = req.get(url, stream=True, timeout=60,
                   headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()

    return Response(
        resp.iter_content(8192),
        content_type=resp.headers.get("Content-Type", f"audio/{'flac' if ext == 'flac' else 'mpeg'}"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def stream_download_from_bytes(data: bytes, artist: str, title: str):
    """Stream raw audio bytes to browser as MP3."""
    ext = "mp3" if data[:3] == b"ID3" else ("flac" if data[:4] == b"fLaC" else "mp3")
    filename = build_filename(artist, title, ext)
    return Response(
        data,
        content_type=f"audio/{'flac' if ext == 'flac' else 'mpeg'}",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/p/<song_id>")
def api_resolve_source(song_id):
    """Resolve audio source URL via Tonzhon proxy (server in China).

    Accepts raw Tonzhon IDs (e.g. ``n186016``, ``m6009...``) or
    platform/id pairs via query params.
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
    artist, title = "Unknown", song_id
    lrc_text = ""

    # Get metadata
    if platform == "netease":
        detail = api.get_song_detail_sync(song_id)
        if detail:
            artist, title = detail.artist, detail.title

    # Get lyrics from all available sources
    lrc_text = tonzhon_lyrics(song_id, platform)
    if not lrc_text:
        try:
            resp = req.post(
                XMSJ_URL,
                data={"input": song_id, "filter": "id", "type": platform, "page": 1},
                headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", "Referer": XMSJ_URL},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 200 and data.get("data"):
                lrc_text = data["data"][0].get("lrc", "")
        except Exception:
            pass

    if not lrc_text and platform == "netease":
        lrc_text = api.get_lyrics_sync(song_id)

    if not lrc_text:
        lrc_text = "[00:00.00] 暂无歌词"

    from urllib.parse import quote
    safe_name = f"{artist} - {title}"
    safe_name = safe_name.encode("ascii", "ignore").decode().strip() or "song"
    safe_name = safe_name.replace('"', '').replace("'", "")[:80]

    return Response(
        lrc_text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"{safe_name}.lrc\"; filename*=UTF-8''{quote(f'{artist} - {title}.lrc')}"
        }
    )


@app.route("/api/lyrics/<platform>/<song_id>")
def api_lyrics(platform, song_id):
    """Get lyrics — try Tonzhon first, then xmsj, then direct API."""
    # Tonzhon
    lrc = tonzhon_lyrics(song_id, platform)
    if lrc:
        return jsonify({"lyric": lrc})

    # xmsj
    try:
        resp = req.post(
            XMSJ_URL,
            data={"input": song_id, "filter": "id", "type": platform, "page": 1},
            headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", "Referer": XMSJ_URL},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            return jsonify({"lyric": data["data"][0].get("lrc", "")})
    except Exception:
        pass

    # Direct NetEase
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
        "tonzhon": True,
        "geo_note": "搜索+xmsj(12平台) | 下载+Tonzhon中国服务器解析 | 音源: 网易云/QQ/咪咕",
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
