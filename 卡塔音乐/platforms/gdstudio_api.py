"""GDStudio Music API wrapper — multi-platform music search & download.

GDStudio (music-api.gdstudio.xyz) is a PHP-based music aggregation API
supporting NetEase, QQ, Kugou, Kuwo, and Migu platforms.

Endpoints:
    ?types=search&source={platform}&name={query}&page={n}
    ?types=url&source={platform}&id={song_id}
    ?types=lyric&source={platform}&id={lyric_id}
    ?types=pic&source={platform}&id={pic_id}&size={px}
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GDSTUDIO_BASE = "https://music-api.gdstudio.xyz/api.php"

PLATFORM_MAP = {
    "netease": "netease",
    "qq": "qq",
    "kugou": "kugou",
    "kuwo": "kuwo",
    "migu": "migu",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_gdstudio(
    query: str, source: str = "netease", page: int = 1
) -> list[dict]:
    """Search for songs via GDStudio API.

    Returns list of standardized song dicts with keys:
        id, title, artist, album, cover_url, lyric, url_id, lyric_id, pic_id
    """
    try:
        # Build URL manually — requests params may double-encode Chinese chars
        qs = f"types=search&source={source}&name={quote(query)}&page={page}"
        url = f"{GDSTUDIO_BASE}?{qs}"
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        data = resp.json()
        if not isinstance(data, list):
            return []

        songs = []
        for item in data:
            # Artist is a list, join it
            artist_raw = item.get("artist", ["Unknown"])
            if isinstance(artist_raw, list):
                artist = ", ".join(artist_raw)
            else:
                artist = str(artist_raw)

            songs.append({
                "id": str(item.get("id", "")),
                "title": item.get("name", "Unknown"),
                "artist": artist,
                "album": item.get("album", ""),
                "cover_url": "",  # resolved later via pic_id
                "lyric": "",      # resolved later via lyric_id
                "url_id": str(item.get("url_id", "")),
                "lyric_id": str(item.get("lyric_id", "")),
                "pic_id": str(item.get("pic_id", "")),
                "source": source,
                "platform": source,
                "platform_name": _platform_name(source),
                "from": "gdstudio",
            })
        return songs
    except Exception:
        return []


def get_song_url(song_id: str, source: str = "netease") -> Optional[dict]:
    """Get download URL and metadata for a song.

    Returns dict with keys: url, br (bitrate in kbps), size (bytes),
    or None if not available.
    """
    try:
        url = f"{GDSTUDIO_BASE}?types=url&source={source}&id={song_id}"
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        data = resp.json()
        if data.get("url"):
            return {
                "url": data["url"],
                "br": data.get("br", 0),
                "size": data.get("size", 0),
            }
        return None
    except Exception:
        return None


def get_lyrics(lyric_id: str, source: str = "netease") -> str:
    """Get LRC lyrics text."""
    try:
        url = f"{GDSTUDIO_BASE}?types=lyric&source={source}&id={lyric_id}"
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        data = resp.json()
        return data.get("lyric", "")
    except Exception:
        return ""


def get_cover_url(pic_id: str, source: str = "netease", size: int = 500) -> str:
    """Get cover image URL."""
    try:
        url = f"{GDSTUDIO_BASE}?types=pic&source={source}&id={pic_id}&size={size}"
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        data = resp.json()
        return data.get("url", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _platform_name(source: str) -> str:
    names = {
        "netease": "网易云", "qq": "QQ音乐", "kugou": "酷狗",
        "kuwo": "酷我", "migu": "咪咕",
    }
    return names.get(source, source)
