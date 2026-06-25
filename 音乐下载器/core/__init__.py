"""Core framework for multi-platform music downloader."""

from .platform_base import BasePlatform
from .downloader import DownloadManager

__all__ = ["BasePlatform", "DownloadManager"]
