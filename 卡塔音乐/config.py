"""Configuration for the Music Downloader web application.

Settings are loaded from environment variables with sensible defaults.
In production, set these via your hosting platform's environment variables.

Environment Variables
---------------------
MD_HOST : str
    Host to bind to (default: "0.0.0.0").
MD_PORT : int
    Port to listen on (default: 5000).
MD_DEBUG : str
    Set to "true"/"1"/"yes" to enable Flask debug mode.
MD_SECRET_KEY : str
    Flask secret key for sessions. Auto-generated if not set.
MD_DOWNLOAD_DIR : str
    Directory for downloaded files (default: current working directory).
MD_NETEASE_API_BASE : str
    Custom NetEase API base URL (for geo-unblock).
MD_NETEASE_COOKIE : str
    NetEase cookie string for VIP authentication.
MD_RATE_LIMIT : str
    Set to "false" to disable rate limiting (default: "true").
MD_RATE_LIMIT_RPM : int
    Max requests per minute per IP (default: 30).
MD_TRANSLATION : str
    Set to "false" to disable LRC translation (default: "true").
"""

import os
from pathlib import Path


class Config:
    """Application configuration loaded from environment variables."""

    # --- Server ---
    HOST: str = os.environ.get("MD_HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("MD_PORT", "5000"))
    DEBUG: bool = os.environ.get("MD_DEBUG", "false").lower() in ("1", "true", "yes")
    SECRET_KEY: str = os.environ.get("MD_SECRET_KEY", os.urandom(24).hex())

    # --- Download ---
    MAX_CONTENT_LENGTH: int = (
        int(os.environ.get("MD_MAX_UPLOAD_MB", "200")) * 1024 * 1024
    )
    DOWNLOAD_DIR: Path = Path(
        os.environ.get("MD_DOWNLOAD_DIR", str(Path.cwd()))
    )

    # --- API / Authentication ---
    NETEASE_API_BASE: str = os.environ.get("MD_NETEASE_API_BASE", "")
    NETEASE_COOKIE: str = os.environ.get("MD_NETEASE_COOKIE", "")

    # --- Rate Limiting ---
    RATE_LIMIT_ENABLED: bool = os.environ.get(
        "MD_RATE_LIMIT", "true"
    ).lower() in ("1", "true", "yes")
    RATE_LIMIT_PER_MINUTE: int = int(os.environ.get("MD_RATE_LIMIT_RPM", "30"))

    # --- Translation ---
    TRANSLATION_ENABLED: bool = os.environ.get(
        "MD_TRANSLATION", "true"
    ).lower() in ("1", "true", "yes")


# Singleton for convenience
config = Config()
