"""General utility functions."""

import re
import os
import logging
from pathlib import Path


def sanitize_filename(name: str) -> str:
    """Remove or replace characters illegal in Windows/Mac filenames."""
    illegal = r'[<>:"/\\|?*]'
    name = re.sub(illegal, "_", name)
    name = name.strip(". ")
    if not name:
        name = "unknown"
    # Limit length to avoid path-too-long issues
    if len(name) > 200:
        name = name[:200]
    return name


def build_filename(artist: str, title: str, ext: str) -> str:
    """Build a clean filename: 'Artist - Title.ext'."""
    artist = sanitize_filename(artist) if artist else "Unknown Artist"
    title = sanitize_filename(title) if title else "Unknown Title"
    ext = ext.lstrip(".")
    return f"{artist} - {title}.{ext}"


def format_duration(ms: int) -> str:
    """Convert milliseconds to mm:ss string."""
    seconds = ms // 1000
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def format_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def setup_logger(name: str = "music_dl", level: int = logging.INFO) -> logging.Logger:
    """Create a console logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s] %(message)s"
        ))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


# Default logger instance
log = setup_logger()
