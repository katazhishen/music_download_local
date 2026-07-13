"""myhkw.cn API wrapper — resolves song audio sources via their Chinese server.

myhkw.cn (s.myhkw.cn / 明月浩空音乐) provides:
  1. Search across multiple platforms (netease/qq/kugou)
  2. Audio URL proxy via api.php?get=url — returns a 302 redirect to
     the actual NetEase/QQ CDN, bypassing geo-blocking.

This replaces the now-dead Tonzhon (tonzhon.whamon.com) service.

Song ID format: plain NetEase numeric ID (e.g. 186016).
"""

from __future__ import annotations

import re
import json
from typing import Optional
from urllib.parse import urljoin

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MYHKW_BASE = "http://s.myhkw.cn/"
MYHKW_SEARCH = MYHKW_BASE
MYHKW_AUDIO_PROXY = f"{MYHKW_BASE}api.php"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": MYHKW_BASE,
    "X-Requested-With": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_song_url(song_id: str, platform: str = "netease") -> Optional[str]:
    """Get a playable audio URL for a song via myhkw.cn audio proxy.

    Searches myhkw for the song, then uses the returned proxy URL
    to get a real CDN audio URL.

    Args:
        song_id: NetEase song ID (e.g. "186016").
        platform: Platform name (only netease is supported).

    Returns:
        A direct MP3/FLAC CDN URL, or None if not found.
    """
    if platform not in ("netease",):
        # myhkw only reliably proxies netease
        return None

    # First: try searching by ID directly
    try:
        resp = requests.post(
            MYHKW_SEARCH,
            data={"input": song_id, "filter": "id", "type": "netease", "page": 1},
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            song = data["data"][0]
            proxy_url = song.get("url", "")
            if proxy_url:
                return _resolve_proxy_url(proxy_url)
    except Exception:
        pass

    return None


def resolve_song_by_keyword(title: str, artist: str = "") -> Optional[tuple[str, str]]:
    """Search myhkw by song title/artist and return (audio_url, netease_id).

    Returns:
        Tuple of (direct_audio_url, netease_song_id), or None.
    """
    query = f"{title} {artist}" if artist else title
    try:
        resp = requests.post(
            MYHKW_SEARCH,
            data={"input": query, "filter": "name", "type": "netease", "page": 1},
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            song = data["data"][0]
            proxy_url = song.get("url", "")
            song_id = str(song.get("songid", ""))
            if proxy_url:
                audio_url = _resolve_proxy_url(proxy_url)
                if audio_url:
                    return (audio_url, song_id)
    except Exception:
        pass
    return None


def resolve_song_url_raw(tonzhon_id: str) -> Optional[str]:
    """Legacy compatibility — accepts raw ID for backward compat.

    Tonzhon used format like ``n186016``. This strips the prefix and
    delegates to :func:`resolve_song_url`.
    """
    # Strip platform prefix if present (n/q/m/k)
    clean_id = re.sub(r'^[nqmk]', '', tonzhon_id)
    return resolve_song_url(clean_id, "netease")


def get_lyrics(song_id: str, platform: str = "netease") -> str:
    """Get lyrics via myhkw.

    Args:
        song_id: NetEase song ID.
        platform: Platform name.

    Returns:
        LRC lyrics text, or empty string.
    """
    try:
        resp = requests.post(
            MYHKW_SEARCH,
            data={"input": song_id, "filter": "id", "type": platform, "page": 1},
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            lrc_field = data["data"][0].get("lrc", "")
            if lrc_field:
                # lrc field is a relative URL: api.php?get=lrc&type=wy&id=...
                if lrc_field.startswith("api.php"):
                    return _fetch_proxy_text(lrc_field)
                # If it's already text (some versions), return directly
                if "[" in lrc_field and "]" in lrc_field:
                    return lrc_field
    except Exception:
        pass
    return ""


def search_myhkw(query: str, platform: str = "netease", page: int = 1) -> list[dict]:
    """Search myhkw and return standardized song dicts.

    Returns list of dicts with keys: id, title, artist, cover, url, lrc, platform.
    The ``url`` field contains the relative audio proxy path
    (e.g. ``api.php?get=url&type=wy&id=123&sign=xxx``).
    """
    try:
        resp = requests.post(
            MYHKW_SEARCH,
            data={"input": query, "filter": "name", "type": platform, "page": page},
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 200:
            return []

        songs = []
        for item in data.get("data", []):
            title = item.get("title") or item.get("name") or "Unknown"
            artist = item.get("author") or item.get("artist") or "Unknown"
            artist = artist.replace("/", ", ")
            song_id = str(item.get("songid", ""))

            # Resolve cover URL (myhkw returns relative proxy path)
            cover_raw = item.get("cover") or item.get("pic") or ""
            if cover_raw and cover_raw.startswith("api.php"):
                cover_raw = urljoin(MYHKW_BASE, cover_raw)

            songs.append({
                "id": song_id,
                "title": title,
                "artist": artist,
                "cover": cover_raw,
                "lyric": item.get("lrc", ""),
                "url": item.get("url", ""),           # relative proxy URL
                "link": item.get("link", "") or f"https://music.163.com/#/song?id={song_id}",
                "platform": platform,
                "platform_name": "网易云" if platform == "netease" else platform,
                "source": "myhkw",
            })
        return songs
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _resolve_proxy_url(proxy_path: str) -> Optional[str]:
    """Follow the myhkw audio proxy to get the real CDN URL.

    Args:
        proxy_path: Relative path like ``api.php?get=url&type=wy&id=123&sign=xxx``.

    Returns:
        The final CDN URL after redirect, or None.
    """
    full_url = urljoin(MYHKW_BASE, proxy_path)
    try:
        # Use HEAD + allow_redirects to follow the proxy without downloading
        resp = requests.head(
            full_url,
            headers={**_HEADERS, "X-Requested-With": ""},  # not an XHR for audio
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            # Check if we got redirected to a CDN
            if "music.126.net" in resp.url or "music.163.com" in resp.url:
                return resp.url
            # Some proxies return the audio directly without redirect
            content_type = resp.headers.get("Content-Type", "")
            if "audio" in content_type:
                return full_url  # Return the proxy URL itself (streams audio)
        return None
    except Exception:
        return None


def _fetch_proxy_text(proxy_path: str) -> str:
    """Fetch text content from a myhkw proxy (used for lyrics).

    Args:
        proxy_path: Relative path like ``api.php?get=lrc&type=wy&id=123&sign=xxx``.

    Returns:
        Text content (LRC lyrics), or empty string.
    """
    full_url = urljoin(MYHKW_BASE, proxy_path)
    try:
        resp = requests.get(
            full_url,
            headers={**_HEADERS, "X-Requested-With": ""},
            timeout=15,
        )
        if resp.status_code == 200:
            text = resp.text
            # Check if it's LRC content
            if "[" in text and "]" in text:
                return text
    except Exception:
        pass
    return ""
