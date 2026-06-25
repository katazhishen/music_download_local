"""NetEase Cloud Music API parameter encryption (weapi + eapi)."""

import os
import re
import json
import base64
import hashlib
from Cryptodome.Cipher import AES

# ---------------------------------------------------------------------------
# WEAPI constants — the RSA public key used by music.163.com web player
# ---------------------------------------------------------------------------
_WEAPI_RSA_N = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b7"
    "25152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e"
    "0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cc"
    "e10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece04"
    "62db0a22b8e7",
    16,
)
_WEAPI_RSA_E = 0x10001

# EAPI constants
_EAPI_KEY = b"e82ckenh8dichen8"  # fixed AES key for eapi


# ---------------------------------------------------------------------------
# WEAPI — used by most web API endpoints
# ---------------------------------------------------------------------------

def weapi(data: dict) -> dict:
    """Encrypt request data using the weapi scheme.

    Returns a dict with ``params`` (base64 AES ciphertext) and ``encSecKey``
    (RSA-encrypted AES key as a 256-char hex string).
    """
    text = json.dumps(data, separators=(",", ":"))

    # 1. Generate random 16-byte AES key
    sec_key = os.urandom(16)

    # 2. AES-128-ECB encrypt with PKCS7-like padding
    pad_length = 16 - len(text) % 16
    padded = (text + chr(pad_length) * pad_length).encode("utf-8")
    cipher = AES.new(sec_key, AES.MODE_ECB)
    encrypted = cipher.encrypt(padded)
    params = base64.b64encode(encrypted).decode("utf-8")

    # 3. RSA encrypt the reversed AES key (raw RSA, no padding!)
    _reversed = sec_key[::-1]
    key_int = int.from_bytes(_reversed, "big")
    cipher_int = pow(key_int, _WEAPI_RSA_E, _WEAPI_RSA_N)
    enc_sec_key = format(cipher_int, "x").zfill(256)

    return {"params": params, "encSecKey": enc_sec_key}


# ---------------------------------------------------------------------------
# EAPI — used by some newer / Linux-API endpoints
# ---------------------------------------------------------------------------

def _hex_upper(data: bytes) -> str:
    """Return uppercase hex of *data*."""
    return data.hex().upper()


def eapi(url: str, data: dict) -> dict:
    """Encrypt request data using the eapi scheme.

    *url* should be just the path portion (e.g. ``/api/linux/forward``).
    Returns a dict with a single ``params`` key.
    """
    text = json.dumps(data, separators=(",", ":"))
    message = f"nobody{url}use{text}md5forencrypt"
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    payload = f"{url}-36cd479b6b5-{text}-36cd479b6b5-{digest}"

    # PKCS7-like pad
    pad_length = 16 - len(payload) % 16
    padded = payload + chr(pad_length) * pad_length

    cipher = AES.new(_EAPI_KEY, AES.MODE_ECB)
    encrypted = cipher.encrypt(padded.encode("utf-8"))

    params = _hex_upper(encrypted)
    return {"params": params}


# ---------------------------------------------------------------------------
# Linux API helper (eapi style with fixed cookie)
# ---------------------------------------------------------------------------

def linuxapi(data: dict) -> dict:
    """Encrypt for the ``/api/linux/forward`` endpoint."""
    return eapi("/api/linux/forward", data)


# ---------------------------------------------------------------------------
# Utility: generate a device ID for NetEase
# ---------------------------------------------------------------------------

def generate_device_id() -> str:
    """Return a random device-id string in NetEase's expected format."""
    return f"{int.from_bytes(os.urandom(4), 'big'):08x}-{int.from_bytes(os.urandom(6), 'big'):012x}"


# ---------------------------------------------------------------------------
# URL / ID parsing helpers
# ---------------------------------------------------------------------------

_NETEASE_URL_PATTERNS = [
    # Song: https://music.163.com/song?id=xxx  or  /song/xxx/
    re.compile(r"music\.163\.com/(?:#/)?song\?id=(\d+)", re.IGNORECASE),
    re.compile(r"music\.163\.com/(?:#/)?song/(\d+)", re.IGNORECASE),
    # Playlist: /playlist?id=xxx  or  /playlist/xxx/
    re.compile(r"music\.163\.com/(?:#/)?playlist\?id=(\d+)", re.IGNORECASE),
    re.compile(r"music\.163\.com/(?:#/)?playlist/(\d+)", re.IGNORECASE),
    # Album: /album?id=xxx
    re.compile(r"music\.163\.com/(?:#/)?album\?id=(\d+)", re.IGNORECASE),
    re.compile(r"music\.163\.com/(?:#/)?album/(\d+)", re.IGNORECASE),
]


def parse_netease_url(url: str) -> tuple[str, str] | None:
    """Return ``(type, id)`` if *url* is a recognised NetEase Music URL,
    otherwise ``None``.  Types: ``"song"``, ``"playlist"``, ``"album"``.
    """
    for i, pat in enumerate(_NETEASE_URL_PATTERNS):
        m = pat.search(url)
        if m:
            # first two patterns are song, next two playlist, last two album
            type_ = ["song", "song", "playlist", "playlist", "album", "album"][i]
            return (type_, m.group(1))
    return None
