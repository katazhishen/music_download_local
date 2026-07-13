"""Abstract base class for music platform integrations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SongInfo:
    """Standardized song metadata across all platforms."""
    song_id: str
    title: str
    artist: str
    album: str = ""
    duration_ms: int = 0
    cover_url: str = ""
    lyric: str = ""
    # Available quality tiers for this song
    qualities: list["SongQuality"] = field(default_factory=list)


@dataclass
class SongQuality:
    """A specific quality tier of a song with its download URL."""
    bitrate: int          # kbps
    format: str           # "mp3", "flac", etc.
    size_bytes: int       # estimated file size, 0 if unknown
    url: str = ""         # direct download URL (may expire)
    quality_label: str = ""  # "标准", "较高", "极高", "无损"
    is_vip: bool = False  # whether this tier requires VIP
    is_available: bool = True  # whether this tier is downloadable now


@dataclass
class SearchResult:
    """Container for search results."""
    songs: list[SongInfo]
    total: int
    has_more: bool = False


class BasePlatform(ABC):
    """Abstract base for a music platform.

    Subclass this and implement the abstract methods to add a new platform.
    """

    # Override these in subclasses
    platform_name: str = "base"
    platform_id: str = "base"

    def __init__(self):
        self.cookies: dict = {}

    def set_cookies(self, cookies: dict):
        """Set cookies for authenticated requests."""
        self.cookies = cookies

    @abstractmethod
    async def search(self, keyword: str, page: int = 1, limit: int = 20) -> SearchResult:
        """Search for songs by keyword. Returns a SearchResult."""
        ...

    @abstractmethod
    async def get_song_detail(self, song_id: str) -> Optional[SongInfo]:
        """Get full song metadata including all quality tiers."""
        ...

    @abstractmethod
    async def get_song_url(self, song_id: str, quality: SongQuality) -> Optional[str]:
        """Get a fresh download URL for a specific quality tier.

        URLs often expire, so this should be called right before downloading.
        Returns a direct download URL or None.
        """
        ...

    @abstractmethod
    async def get_playlist(self, playlist_id: str) -> list[SongInfo]:
        """Get all songs in a playlist."""
        ...

    def supports_url(self, url: str) -> bool:
        """Check if this platform can handle the given URL.

        Override to recognize platform-specific URLs (e.g. music.163.com).
        """
        return False

    def extract_id_from_url(self, url: str) -> Optional[tuple[str, str]]:
        """Extract (type, id) from a URL. Types: 'song', 'playlist', 'album'.

        Override in platform subclasses.
        """
        return None
