"""NetEase Cloud Music API client.

Covers: search, song detail, playback URL, playlist, lyrics,
and VIP-quality negotiation.

Supports two operating modes:
  1. Direct API access (works inside mainland China, or with a VPN/proxy)
  2. Custom API base URL (for using community-deployed proxy servers)

Public /api/ endpoints are used for search, detail, and lyrics where possible
(they work globally without encryption).  Playback URLs require either a
Chinese IP address or a proxy API server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, Any

import aiohttp
import requests

from core.platform_base import SongInfo, SongQuality, SearchResult
from core.utils import log, format_duration
from .crypto import weapi, eapi, generate_device_id, parse_netease_url


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE_URL = "https://music.163.com"
_WEAPI_URL = f"{_BASE_URL}/weapi"

# Quality tiers ordered best → worst
_QUALITY_TIERS: list[dict] = [
    {"key": "hires",    "level": "hires",    "label": "Hi-Res FLAC",   "br": 2304000, "fmt": "flac"},
    {"key": "lossless", "level": "lossless", "label": "无损 FLAC",      "br": 900000,  "fmt": "flac"},
    {"key": "exhigh",   "level": "exhigh",   "label": "极高 320kbps",   "br": 320000,  "fmt": "mp3"},
    {"key": "higher",   "level": "higher",   "label": "较高 192kbps",   "br": 192000,  "fmt": "mp3"},
    {"key": "standard", "level": "standard", "label": "标准 128kbps",   "br": 128000,  "fmt": "mp3"},
]
_QUALITY_FALLBACK_ORDER = [t["key"] for t in _QUALITY_TIERS]

# Map quality key → info dict
_QUALITY_MAP: dict[str, dict] = {t["key"]: t for t in _QUALITY_TIERS}


# ---------------------------------------------------------------------------
# HTTP session — handles both public GET and weapi POST
# ---------------------------------------------------------------------------

class NeteaseSession:
    """Manages HTTP sessions and cookies for NetEase API calls."""

    def __init__(self, proxy: str = "", api_base: str = ""):
        """
        Args:
            proxy: HTTP proxy URL (e.g. ``http://127.0.0.1:7890``).
            api_base: Custom API base URL for proxy deployments
                      (e.g. ``https://netease-api.vercel.app``).
        """
        self.cookies: dict = {}
        self._csrf_token: str = ""
        self._device_id: str = generate_device_id()
        self._api_base: str = api_base.rstrip("/") if api_base else _BASE_URL
        self._proxy: str = proxy

        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        }

    # ---- cookie management ----

    def set_cookies(self, cookies: dict):
        self.cookies.update(cookies)
        self._csrf_token = cookies.get("__csrf", self._csrf_token)

    def import_cookie_string(self, cookie_string: str):
        """Parse a raw cookie header string."""
        for item in cookie_string.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                self.cookies[k.strip()] = v.strip()
        self._csrf_token = self.cookies.get("__csrf", self._csrf_token)
        log.info(f"Imported {len(self.cookies)} cookie(s)")

    @property
    def is_authenticated(self) -> bool:
        return "MUSIC_U" in self.cookies and bool(self.cookies["MUSIC_U"])

    @property
    def using_custom_api(self) -> bool:
        """True when requests go through a proxy API server."""
        return self._api_base != _BASE_URL

    # ---- raw HTTP (async) ----

    def _full_url(self, path: str) -> str:
        """Prepend the API base URL to *path* (which must start with ``/``)."""
        return f"{self._api_base}{path}"

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        """Async GET to a public /api/ endpoint.  Returns parsed JSON."""
        url = self._full_url(path)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, params=params, headers=self._headers, cookies=self.cookies,
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
                if not text:
                    raise ValueError(f"Empty response from {url}")
                return json.loads(text)

    async def _post_weapi(self, path: str, data: dict) -> dict:
        """Async POST using weapi encryption."""
        url = f"{_BASE_URL}{path}"  # weapi always goes to music.163.com
        payload = weapi(data)
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded"}
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, data=payload, headers=headers, cookies=self.cookies,
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
                if not text:
                    raise ValueError(f"Empty response from {url}")
                return json.loads(text)

    # ---- raw HTTP (sync) ----

    def _get_json_sync(self, path: str, params: dict | None = None) -> dict:
        """Sync GET to a public /api/ endpoint."""
        url = self._full_url(path)
        resp = requests.get(
            url, params=params, headers=self._headers,
            cookies=self.cookies, timeout=30,
        )
        resp.raise_for_status()
        if not resp.text:
            raise ValueError(f"Empty response from {url}")
        return resp.json()

    def _post_weapi_sync(self, path: str, data: dict) -> dict:
        """Sync POST using weapi encryption."""
        url = f"{_BASE_URL}{path}"
        payload = weapi(data)
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(
            url, data=payload, headers=headers,
            cookies=self.cookies, timeout=30,
        )
        resp.raise_for_status()
        if not resp.text:
            raise ValueError(f"Empty response from {url}")
        return resp.json()


# ---------------------------------------------------------------------------
# NeteaseAPI — public interface
# ---------------------------------------------------------------------------

class NeteaseAPI:
    """High-level wrapper around the NetEase Cloud Music API.

    Typical usage::

        api = NeteaseAPI()
        # Optional: use cookies for VIP access
        api.import_cookie_string("MUSIC_U=...; __csrf=...")
        # Optional: use a proxy API server
        api = NeteaseAPI(api_base="https://your-proxy.vercel.app")

        result = await api.search("周杰伦 晴天")
        detail = await api.get_song_detail(result.songs[0].song_id)
        url = await api.get_song_url(detail.song_id, "exhigh")
    """

    def __init__(
        self,
        session: Optional[NeteaseSession] = None,
        proxy: str = "",
        api_base: str = "",
    ):
        self._api = session or NeteaseSession(proxy=proxy, api_base=api_base)

    # ---- configuration ----

    def set_cookies(self, cookies: dict):
        self._api.set_cookies(cookies)

    def import_cookie_string(self, cookie_string: str):
        self._api.import_cookie_string(cookie_string)

    @property
    def is_authenticated(self) -> bool:
        return self._api.is_authenticated

    # ------------------------------------------------------------------
    #  Search
    # ------------------------------------------------------------------

    async def search(self, keyword: str, page: int = 1, limit: int = 20) -> SearchResult:
        """Search songs by keyword."""
        params = {
            "s": keyword,
            "type": 1,
            "limit": limit,
            "offset": (page - 1) * limit,
        }
        # Try public API first (works globally), fall back to weapi
        try:
            resp = await self._api._get_json("/api/search/get", params)
        except Exception:
            resp = await self._api._post_weapi("/weapi/cloudsearch/get/web", params)

        result = resp.get("result", {})
        songs_raw = result.get("songs", [])
        total = result.get("songCount", 0)
        songs = [_parse_song_summary(s) for s in songs_raw]
        return SearchResult(songs=songs, total=total, has_more=len(songs) >= limit)

    def search_sync(self, keyword: str, page: int = 1, limit: int = 20) -> SearchResult:
        """Synchronous search."""
        params = {"s": keyword, "type": 1, "limit": limit, "offset": (page - 1) * limit}
        try:
            resp = self._api._get_json_sync("/api/search/get", params)
        except Exception:
            resp = self._api._post_weapi_sync("/weapi/cloudsearch/get/web", params)
        result = resp.get("result", {})
        songs_raw = result.get("songs", [])
        total = result.get("songCount", 0)
        return SearchResult(
            songs=[_parse_song_summary(s) for s in songs_raw],
            total=total,
            has_more=len(songs_raw) >= limit,
        )

    # ------------------------------------------------------------------
    #  Song detail
    # ------------------------------------------------------------------

    async def get_song_detail(self, song_id: str) -> Optional[SongInfo]:
        """Get full metadata + available quality tiers for a song."""
        ids = f"[{song_id}]"
        try:
            # Public API: GET with query params
            resp = await self._api._get_json(
                "/api/song/detail", {"id": song_id, "ids": ids}
            )
        except Exception:
            # Fallback: weapi POST
            resp = await self._api._post_weapi(
                "/weapi/v3/song/detail", {"c": json.dumps([{"id": int(song_id)}])}
            )

        songs = resp.get("songs", [])
        if not songs:
            return None
        return _parse_song_detail(songs[0])

    def get_song_detail_sync(self, song_id: str) -> Optional[SongInfo]:
        """Synchronous song detail."""
        ids = f"[{song_id}]"
        try:
            resp = self._api._get_json_sync(
                "/api/song/detail", {"id": song_id, "ids": ids}
            )
        except Exception:
            resp = self._api._post_weapi_sync(
                "/weapi/v3/song/detail", {"c": json.dumps([{"id": int(song_id)}])}
            )
        songs = resp.get("songs", [])
        return _parse_song_detail(songs[0]) if songs else None

    # ------------------------------------------------------------------
    #  Song URL (the tricky one — geo-restricted outside China)
    # ------------------------------------------------------------------

    async def get_song_url(self, song_id: str, level: str = "exhigh") -> Optional[str]:
        """Get a download URL for *song_id* at the requested quality.

        Falls back through lower quality tiers if the requested one isn't
        available.  Returns ``None`` if no URL could be obtained (common
        outside mainland China — see notes below).

        .. note::

            Song audio URLs are geo-restricted to mainland China.  If you
            are outside China you will likely get ``None`` unless you:

            - use a Chinese VPN/proxy, OR
            - provide ``api_base`` pointing to a community proxy server.

            Community proxy servers (deploy your own or find a public one):
            ``netease-cloud-music-api`` on Vercel / Railway / Render.
        """
        for try_level in _quality_fallbacks_from(level):
            url = await self._try_get_url(song_id, try_level)
            if url:
                return url
        return None

    def get_song_url_sync(self, song_id: str, level: str = "exhigh") -> Optional[str]:
        """Synchronous song URL."""
        for try_level in _quality_fallbacks_from(level):
            url = self._try_get_url_sync(song_id, try_level)
            if url:
                return url
        return None

    async def _try_get_url(self, song_id: str, level: str) -> Optional[str]:
        """Try one quality tier.  Uses public API first, then weapi."""
        q = _QUALITY_MAP[level]
        br = q["br"]

        # --- Strategy 1: public GET endpoint ---
        try:
            resp = await self._api._get_json(
                "/api/song/enhance/player/url",
                {"id": song_id, "ids": f"[{song_id}]", "br": br},
            )
            url = _extract_url(resp)
            if url:
                return url
        except Exception:
            pass

        # --- Strategy 2: weapi POST v1 (preferred for quality options) ---
        try:
            resp = await self._api._post_weapi(
                "/weapi/song/enhance/player/url/v1",
                {
                    "ids": f"[{song_id}]",
                    "level": level,
                    "encodeType": q["fmt"],
                },
            )
            url = _extract_url(resp)
            if url:
                return url
        except Exception:
            pass

        # --- Strategy 3: weapi POST v2 (older endpoint) ---
        try:
            resp = await self._api._post_weapi(
                "/weapi/song/enhance/player/url",
                {"ids": f"[{song_id}]", "br": br},
            )
            url = _extract_url(resp)
            if url:
                return url
        except Exception:
            pass

        return None

    def _try_get_url_sync(self, song_id: str, level: str) -> Optional[str]:
        """Synchronous single-quality URL fetch."""
        q = _QUALITY_MAP[level]
        br = q["br"]

        for fetch in [
            lambda: self._api._get_json_sync(
                "/api/song/enhance/player/url",
                {"id": song_id, "ids": f"[{song_id}]", "br": br},
            ),
            lambda: self._api._post_weapi_sync(
                "/weapi/song/enhance/player/url/v1",
                {"ids": f"[{song_id}]", "level": level, "encodeType": q["fmt"]},
            ),
            lambda: self._api._post_weapi_sync(
                "/weapi/song/enhance/player/url",
                {"ids": f"[{song_id}]", "br": br},
            ),
        ]:
            try:
                url = _extract_url(fetch())
                if url:
                    return url
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    #  Playlist
    # ------------------------------------------------------------------

    async def get_playlist(self, playlist_id: str) -> list[SongInfo]:
        """Get all songs in a playlist.

        .. note::
            For playlists with >1000 songs, only the first 1000 are returned
            by the API.  Use pagination with ``offset`` if needed.
        """
        if self._api.using_custom_api:
            # Proxy servers usually expose playlist endpoints directly
            resp = await self._api._get_json(
                "/api/playlist/detail", {"id": playlist_id}
            )
            playlist = resp.get("playlist", resp.get("result", {}))
        else:
            # Use weapi (more reliable within China)
            try:
                resp = await self._api._post_weapi(
                    "/weapi/v6/playlist/detail",
                    {"id": int(playlist_id), "n": 100000, "s": 8},
                )
                playlist = resp.get("playlist", {})
            except Exception:
                # Fallback to public API
                resp = await self._api._get_json(
                    "/api/playlist/detail", {"id": playlist_id}
                )
                playlist = resp.get("result", {})

        tracks = playlist.get("tracks", [])
        return [_parse_song_summary(t) for t in tracks]

    def get_playlist_sync(self, playlist_id: str) -> list[SongInfo]:
        """Synchronous playlist fetch."""
        if self._api.using_custom_api:
            resp = self._api._get_json_sync(
                "/api/playlist/detail", {"id": playlist_id}
            )
            playlist = resp.get("playlist", resp.get("result", {}))
        else:
            try:
                resp = self._api._post_weapi_sync(
                    "/weapi/v6/playlist/detail",
                    {"id": int(playlist_id), "n": 100000, "s": 8},
                )
                playlist = resp.get("playlist", {})
            except Exception:
                resp = self._api._get_json_sync(
                    "/api/playlist/detail", {"id": playlist_id}
                )
                playlist = resp.get("result", {})

        tracks = playlist.get("tracks", [])
        return [_parse_song_summary(t) for t in tracks]

    # ------------------------------------------------------------------
    #  Lyrics
    # ------------------------------------------------------------------

    async def get_lyrics(self, song_id: str) -> str:
        """Get lyrics (LRC format) for a song."""
        try:
            resp = await self._api._get_json(
                "/api/song/lyric", {"id": song_id, "lv": -1, "tv": -1}
            )
        except Exception:
            resp = await self._api._post_weapi(
                "/weapi/song/lyric", {"id": int(song_id), "lv": -1, "tv": -1}
            )
        lrc = resp.get("lrc", {}) or {}
        return lrc.get("lyric", "")

    def get_lyrics_sync(self, song_id: str) -> str:
        """Synchronous lyrics fetch."""
        try:
            resp = self._api._get_json_sync(
                "/api/song/lyric", {"id": song_id, "lv": -1, "tv": -1}
            )
        except Exception:
            resp = self._api._post_weapi_sync(
                "/weapi/song/lyric", {"id": int(song_id), "lv": -1, "tv": -1}
            )
        lrc = resp.get("lrc", {}) or {}
        return lrc.get("lyric", "")

    # ------------------------------------------------------------------
    #  URL recognition
    # ------------------------------------------------------------------

    @staticmethod
    def supports_url(url: str) -> bool:
        return parse_netease_url(url) is not None

    @staticmethod
    def extract_id(url: str) -> Optional[tuple[str, str]]:
        return parse_netease_url(url)

    # ------------------------------------------------------------------
    #  Bulk helpers
    # ------------------------------------------------------------------

    async def get_all_song_urls(
        self, song_ids: list[str], level: str = "exhigh"
    ) -> dict[str, Optional[str]]:
        """Get download URLs for multiple songs."""
        results: dict[str, Optional[str]] = {}
        for sid in song_ids:
            results[sid] = await self.get_song_url(sid, level)
        return results


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _extract_url(resp: dict) -> Optional[str]:
    """Pull the first usable URL from a player/url response."""
    for entry in resp.get("data", []):
        url = entry.get("url", "")
        if url and url.startswith("http"):
            return url
    return None


def _parse_song_summary(raw: dict) -> SongInfo:
    """Build a SongInfo from a search-result / playlist-track entry.

    Handles both the abbreviated weapi format (``ar``, ``al``, ``dt``)
    and the full public-API format (``artists``, ``album``, ``duration``).
    """
    # Artist list: weapi uses "ar", public API uses "artists"
    artist_list = raw.get("ar") or raw.get("artists") or []
    artists = ", ".join(a["name"] for a in artist_list if a.get("name"))

    # Album: weapi uses "al", public API uses "album"
    album = raw.get("al") or raw.get("album") or {}

    # Duration: weapi uses "dt", public API uses "duration"
    duration_ms = raw.get("dt") or raw.get("duration") or 0

    return SongInfo(
        song_id=str(raw["id"]),
        title=raw.get("name", "Unknown"),
        artist=artists or "Unknown Artist",
        album=album.get("name", ""),
        duration_ms=duration_ms,
        cover_url=(album.get("picUrl", "") or ""),
        qualities=[],
    )


def _parse_song_detail(raw: dict) -> SongInfo:
    """Build a SongInfo from a song-detail response.

    Handles both abbreviated weapi format (``ar``, ``al``, ``dt``) and
    the full public-API format (``artists``, ``album``, ``duration``).
    """
    # Artist list
    artist_list = raw.get("ar") or raw.get("artists") or []
    artists = ", ".join(a["name"] for a in artist_list if a.get("name"))

    # Album
    album = raw.get("al") or raw.get("album") or {}

    # Duration
    duration_ms = raw.get("dt") or raw.get("duration") or 0

    # Fee (only in detail responses)
    fee = raw.get("fee", 0)  # 0=free, 1=VIP, 4/8=paid album

    info = SongInfo(
        song_id=str(raw["id"]),
        title=raw.get("name", "Unknown"),
        artist=artists or "Unknown Artist",
        album=album.get("name", ""),
        duration_ms=duration_ms,
        cover_url=(album.get("picUrl", "") or ""),
        qualities=[],
    )

    # Infer available quality tiers from privilege + fee flags
    priv = raw.get("privilege", {})
    max_br = priv.get("maxbr", 320000)  # 0 = can't play at all

    for key in _QUALITY_FALLBACK_ORDER:
        q_info = _QUALITY_MAP[key]
        br = q_info["br"]
        is_available = br <= max_br if max_br > 0 else (fee == 0 and br <= 128000)
        info.qualities.append(SongQuality(
            bitrate=br // 1000,
            format=q_info["fmt"],
            size_bytes=0,
            quality_label=q_info["label"],
            is_vip=(fee > 0 and br > 128000),
            is_available=is_available,
        ))

    # Mark fee status
    if fee > 0:
        info.title += " [VIP]" if fee == 1 else " [付费]"

    return info


def _quality_fallbacks_from(start_level: str) -> list[str]:
    """Return quality keys from *start_level* down to the lowest."""
    try:
        idx = _QUALITY_FALLBACK_ORDER.index(start_level)
    except ValueError:
        idx = 2  # default to exhigh
    return _QUALITY_FALLBACK_ORDER[idx:]
