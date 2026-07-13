"""Tonzhon API wrapper — resolves song audio sources via their Chinese server.

Tonzhon (tonzhon.whamon.com) is an Express.js server hosted on Alibaba Cloud
China (47.116.28.58).  Its ``/api/p/{songId}`` endpoint returns real,
playable audio URLs from NetEase / QQ / Migu CDNs — bypassing the geo-block
that prevents our local Flask instance from fetching audio directly.

Song ID format: ``<platform_prefix><id>``
    n = NetEase (网易云)    e.g. n186016
    q = QQ Music            e.g. q002B2EAA3brD5b
    m = Migu (咪咕)          e.g. m600929000008092647
"""

from __future__ import annotations

import json
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TONZHON_BASE = "https://tonzhon.whamon.com"
TONZHON_API_P = f"{TONZHON_BASE}/api/p"       # song audio source
TONZHON_API_L = f"{TONZHON_BASE}/api/l"       # lyrics
TONZHON_NEW_SONGS = f"{TONZHON_BASE}/api/new-songs"

# Platform prefix mapping (our name → tonzhon prefix)
PLATFORM_PREFIX = {
    "netease": "n",
    "qq":       "q",
    "migu":     "m",
    "kugou":    "k",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": TONZHON_BASE,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_song_url(song_id: str, platform: str = "netease") -> Optional[str]:
    """Get a playable audio URL for a song via Tonzhon.

    Args:
        song_id: Platform-native song ID (e.g. ``"186016"`` for NetEase).
        platform: One of ``netease``, ``qq``, ``migu``, ``kugou``.

    Returns:
        A direct MP3/FLAC URL, or ``None`` if the song isn't cached.
    """
    prefix = PLATFORM_PREFIX.get(platform, "n")
    tonzhon_id = f"{prefix}{song_id}"

    try:
        resp = requests.get(
            f"{TONZHON_API_P}/{tonzhon_id}",
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("success") and data.get("data"):
            return data["data"]
        return None
    except Exception:
        return None


def resolve_song_url_raw(tonzhon_id: str) -> Optional[str]:
    """Like :func:`resolve_song_url` but accepts a raw Tonzhon ID
    (e.g. ``"n186016"``, ``"m6009..."``)."""
    try:
        resp = requests.get(
            f"{TONZHON_API_P}/{tonzhon_id}",
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("success") and data.get("data"):
            return data["data"]
        return None
    except Exception:
        return None


def get_lyrics(song_id: str, platform: str = "netease") -> str:
    """Get lyrics (LRC format) for a song via Tonzhon.

    Args:
        song_id: Platform-native song ID.
        platform: Platform name.

    Returns:
        LRC lyrics text, or empty string.
    """
    prefix = PLATFORM_PREFIX.get(platform, "n")
    tonzhon_id = f"{prefix}{song_id}"

    try:
        resp = requests.get(
            f"{TONZHON_API_L}/{tonzhon_id}",
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("success") and data.get("data"):
            return data["data"]
        return ""
    except Exception:
        return ""


def get_new_songs() -> list[dict]:
    """Get the latest songs from Tonzhon's front page."""
    try:
        resp = requests.get(TONZHON_NEW_SONGS, headers=_HEADERS, timeout=15)
        data = resp.json()
        if data.get("success"):
            return data.get("songs", [])
        return []
    except Exception:
        return []
