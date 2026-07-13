"""NetEase Cloud Music platform."""

from .api import NeteaseAPI
from .ncm import decrypt_ncm, decrypt_ncm_batch, decrypt_ncm_directory
from .crypto import parse_netease_url

__all__ = [
    "NeteaseAPI",
    "decrypt_ncm",
    "decrypt_ncm_batch",
    "decrypt_ncm_directory",
    "parse_netease_url",
]
