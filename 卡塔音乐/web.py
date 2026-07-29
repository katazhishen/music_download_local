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

import os, sys, json, tempfile, time, threading, secrets, io, zipfile, re, hashlib, hmac
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
IS_PRODUCTION = os.environ.get("RENDER", "").lower() == "true"

app = Flask(__name__)
app.secret_key = os.environ.get("MD_SECRET_KEY") or os.urandom(24).hex()
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = not IS_PRODUCTION

api = NeteaseAPI()

if IS_PRODUCTION:
    DOWNLOAD_DIR = Path("/tmp/downloads")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
else:
    DOWNLOAD_DIR = Path.cwd()


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
    """Search via QQ Music official API."""
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
        for item in data.get("data", {}).get("song", {}).get("list", []):
            singer_list = item.get("singer", [])
            artist = ", ".join(s.get("name", "") for s in singer_list) if singer_list else "Unknown"
            albummid = item.get("albummid", "")
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
            })
        total = data.get("data", {}).get("song", {}).get("totalnum", len(songs))
        return {"songs": songs, "total": total, "error": None}
    except Exception as e:
        return {"songs": [], "total": 0, "error": str(e)}


def search_migu(query: str, platform: str = "migu", page: int = 1) -> dict:
    """Search via Migu Music (咪咕音乐) official API."""
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
        for item in data.get("songResultData", {}).get("result", []):
            singers = [s.get("name", "") for s in item.get("singers", [])]
            artist = ", ".join(singers) if singers else "Unknown"
            cover = ""
            album_imgs = item.get("albumImgs") or item.get("imgItems") or []
            if album_imgs:
                cover = album_imgs[0].get("img", "")
            songs.append({
                "id": item.get("contentId", str(item.get("id", ""))),
                "title": item.get("name", "Unknown"),
                "artist": artist,
                "cover": cover,
                "duration": 0,
                "heat": 0,
                "lyric": "",
                "url": "",
                "link": f"https://music.migu.cn/v3/music/song/{item.get('copyrightId', '')}",
                "platform": "migu",
                "platform_name": "咪咕音乐",
                "source": "migu",
                "_copyright_id": item.get("copyrightId", ""),
                "_content_id": item.get("contentId", ""),
            })
        total = int(data.get("songResultData", {}).get("totalCount", "0"))
        return {"songs": songs, "total": total, "error": None}
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

    # Source 7: Xiageba (下歌吧) — supplementary results for netease/qq/migu
    SEARCH_SOURCES.append(("xiageba", search_xiageba, ["netease", "qq", "migu"]))

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
    errors = []
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


@app.route("/api/download/<platform>/<song_id>")
def api_download(platform, song_id):
    """Download MP3 — tries multiple audio sources until success."""
    import time
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    name = f"{artist} - {title}" if title else song_id
    artist = artist or "Unknown"
    title = title or "Unknown"

    # Each strategy returns (Response, None) on success or (None, str_error) on failure

    def try_netease_direct():
        """Strategy 1: Direct NetEase API for netease platform songs."""
        if platform != "netease":
            return None, "not netease platform"
        url = api.get_song_url_sync(song_id, "lossless")
        if not url:
            url = api.get_song_url_sync(song_id, "standard")
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
                url = api.get_song_url_sync(ns.song_id, "lossless")
                if not url:
                    url = api.get_song_url_sync(ns.song_id, "standard")
                if url:
                    resp = _download_mp3_from_cdn(url, artist, title, ns.song_id, "netease")
                    if resp:
                        log.info(f"[download] cross-search matched: {ns.title} (id={ns.song_id})")
                        return (resp, None)
            return None, "cross-search: all matches failed"
        except Exception as e:
            return None, f"cross-search error: {e}"

    def try_myhkw_by_keyword():
        """Strategy 3: Search myhkw.cn by title + artist (may be down)."""
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

    # Order: NetEase direct (fastest) → NetEase cross-search (reliable) → myhkw (fallback)
    strategies = [
        ("netease_direct", try_netease_direct),
        ("netease_cross", try_netease_cross_search),
        ("myhkw_keyword", try_myhkw_by_keyword),
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
                            analytics.track_download(song_id, title, artist, platform, strategy_name, True)
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
        analytics.track_download(song_id, title, artist, platform, "", False)
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


@app.route("/api/stream/<song_id>")
def api_stream_audio(song_id):
    """Stream audio through the server so the browser never hits the CDN directly.

    Resolves audio URL via NetEase API (direct or cross-search), then fetches
    server-side with the correct Referer header and streams to the browser.
    Supports HTTP Range requests for seeking.
    """
    platform = request.args.get("platform", "")
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")

    # ── Resolve audio URL ──
    url = None

    # Strategy 1: NetEase direct (for netease platform)
    if platform == "netease":
        url = api.get_song_url_sync(song_id, "lossless")
        if not url:
            url = api.get_song_url_sync(song_id, "standard")

    # Strategy 2: NetEase cross-search by title+artist
    if not url and title:
        try:
            search_q = f"{title} {artist}" if artist else title
            result = api.search_sync(search_q, limit=5)
            if result and result.songs:
                for ns in result.songs:
                    u = api.get_song_url_sync(ns.song_id, "lossless")
                    if not u:
                        u = api.get_song_url_sync(ns.song_id, "standard")
                    if u:
                        url = u
                        log.info(f"[stream] cross-search matched: {ns.title} (id={ns.song_id})")
                        break
        except Exception as e:
            log.warning(f"[stream] cross-search failed: {e}")

    # Strategy 3: myhkw.cn (may be down)
    if not url:
        if platform:
            url = resolve_song_url(song_id, platform)
        else:
            url = resolve_song_url_raw(song_id)

    # Strategy 4: myhkw keyword search
    if not url and title:
        result = resolve_song_by_keyword(title, artist)
        if result:
            url, _ = result

    if not url:
        return jsonify({"error": "no audio source available"}), 404

    # ── Fetch audio server-side with music-platform Referer ──
    try:
        downstream = req.get(
            url,
            stream=True,
            timeout=60,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": "https://music.163.com/",
            },
        )
        downstream.raise_for_status()
    except Exception as e:
        log.error(f"[stream] fetch failed for {song_id}: {e}")
        return jsonify({"error": f"upstream fetch failed: {e}"}), 502

    content_type = downstream.headers.get("Content-Type", "audio/mpeg")
    content_length = downstream.headers.get("Content-Length")

    # ── Range-request support (for seeking) ──
    range_header = request.headers.get("Range")
    status = 200
    resp_headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=7200",
    }

    if range_header and content_length:
        try:
            raw_range = range_header.replace("bytes=", "")
            parts = raw_range.split("-")
            range_start = int(parts[0]) if parts[0] else 0
            range_end = int(parts[1]) if len(parts) > 1 and parts[1] else int(content_length) - 1

            resp_headers["Content-Range"] = f"bytes {range_start}-{range_end}/{content_length}"
            resp_headers["Content-Length"] = str(range_end - range_start + 1)
            status = 206

            full_data = downstream.content
            return Response(
                full_data[range_start:range_end + 1],
                status=206,
                headers=resp_headers,
            )
        except (ValueError, IndexError):
            pass
    else:
        if content_length:
            resp_headers["Content-Length"] = content_length

    return Response(
        downstream.iter_content(8192),
        status=status,
        headers=resp_headers,
    )


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


@app.route("/api/playlist/import")
def api_playlist_import():
    """Import a playlist from a share URL.

    Returns a list of songs with available metadata (covers fetched where
    possible).  The frontend adds these to its play queue.
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing playlist URL"}), 400

    parsed = parse_playlist_url(url)
    if not parsed:
        return jsonify({"error": "无法识别的歌单链接，支持：网易云音乐、QQ音乐、酷狗、酷我"}), 400

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
            "platform_name": {"netease": "网易云", "qq": "QQ音乐", "kugou": "酷狗", "kuwo": "酷我"}.get(platform, platform),
        })

    except Exception as e:
        log.error(f"[playlist] Import failed for {platform}/{pid}: {e}")
        return jsonify({"error": f"导入失败：{e}"}), 502


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
# Visitor tracking middleware
# ---------------------------------------------------------------------------

@app.before_request
def _track_visitor():
    """Record every page / API visit (exclude static files)."""
    if request.path.startswith("/static/") or request.path.startswith("/img/"):
        return
    if request.path == "/robots.txt":
        return
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
        ip = ip.split(",")[0].strip()
        ua = request.headers.get("User-Agent", "")
        analytics.track_visit(ip, ua)
    except Exception:
        pass  # never break the main app for analytics


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
    ("GDStudio API", "https://gdstudio.xyz/api.php?types=search&source=netease&name=test&page=1", 10),
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


@app.after_request
def add_security_headers(response):
    """Add basic security headers (safe in development)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
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
    return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _print_warning_banner()
    import argparse
    p = argparse.ArgumentParser(description="Music Downloader Web UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
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

    env_label = "PRODUCTION (Render)" if IS_PRODUCTION else "DEVELOPMENT"
    print(f"""
╔══════════════════════════════════════════════╗
║      音乐下载器 Web UI v3.0                   ║
║      支持: 网易云/QQ/酷狗/酷我/咪咕等12平台    ║
╠══════════════════════════════════════════════╣
║  模式: {env_label:37s} ║
║  地址: http://{args.host}:{args.port}                  ║
║  输出: {str(DOWNLOAD_DIR)[:35]:35s} ║
╚══════════════════════════════════════════════╝
    """)
    app.run(host=args.host, port=args.port, debug=args.debug)
