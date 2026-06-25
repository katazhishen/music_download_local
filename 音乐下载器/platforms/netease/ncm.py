"""NCM file decryption — convert .ncm to .flac or .mp3.

NCM (NetEase Cloud Music encrypted file) format:
    Offset  Size  Description
    ------  ----  -----------
    0       8     Magic: b"CTENFDAM"
    8       2     Gap (always 0x00?)
    10      4     Key length (little-endian int)
    14      N     XOR-obfuscated AES key (N = key_length bytes)
    14+N    4     Meta-info length (little-endian int)
    18+N    M     Meta-info (JSON, M = meta_length bytes)
    18+N+M  5     CRC / gap
    23+N+M  *     Encrypted audio data (AES-128-ECB)

The XOR key for deobfuscating the AES key is 0x64.
The rest of the file (> header) is decrypted with AES-128-ECB using
the extracted key.

Some NCM files also contain an embedded cover image; we extract that too.
"""

import os
import json
import struct
import base64
from pathlib import Path
from Cryptodome.Cipher import AES

from core.utils import log


# Constants
_NCM_MAGIC = b"CTENFDAM"
_XOR_KEY = 0x64


def _read_exact(f, size: int) -> bytes:
    """Read exactly *size* bytes, raising on short read."""
    data = f.read(size)
    if len(data) < size:
        raise ValueError(f"Truncated NCM file: expected {size} bytes, got {len(data)}")
    return data


def _dump_cover(output_path: Path, meta: dict) -> bool:
    """Extract embedded cover image from NCM metadata (if present).

    Returns True if a cover was saved.
    """
    cover_data = meta.get("albumCover")
    if not cover_data:
        cover_data = meta.get("musicCover")
    if not cover_data:
        return False

    try:
        data = base64.b64decode(cover_data)
    except Exception:
        return False

    # Determine image format from magic bytes
    ext = ".jpg"
    if data[:4] == b"\x89PNG":
        ext = ".png"
    elif data[:4] == b"RIFF":
        ext = ".webp"

    cover_path = output_path.with_suffix(ext)
    cover_path.write_bytes(data)
    log.info(f"Cover saved: {cover_path.name}")
    return True


def decrypt_ncm(input_path: str, output_dir: str | None = None) -> str | None:
    """Decrypt a .ncm file and save it as .flac or .mp3.

    Args:
        input_path: Path to the .ncm file.
        output_dir: Directory for the decrypted file (default: same as input).

    Returns:
        Path to the decrypted file, or None on failure.
    """
    input_path = Path(input_path)
    if not input_path.suffix.lower() == ".ncm":
        log.warning(f"{input_path.name} doesn't have .ncm extension — skipping")
        return None

    if not input_path.exists():
        log.error(f"File not found: {input_path}")
        return None

    out_dir = Path(output_dir) if output_dir else input_path.parent

    try:
        with open(input_path, "rb") as f:
            # --- Parse header ---
            magic = f.read(8)
            if magic != _NCM_MAGIC:
                log.error(f"Not a valid NCM file: {input_path.name}")
                return None

            f.read(2)  # gap

            # Key length (4 bytes LE)
            key_len = struct.unpack("<I", _read_exact(f, 4))[0]
            if key_len < 16 or key_len > 256:
                log.error(f"Suspicious NCM key length ({key_len})")
                return None

            # XOR-obfuscated AES key
            key_data = bytearray(_read_exact(f, key_len))
            for i in range(len(key_data)):
                key_data[i] ^= _XOR_KEY
            aes_key = bytes(key_data)

            # Meta-info length
            meta_len = struct.unpack("<I", _read_exact(f, 4))[0]
            if meta_len > 64 * 1024:  # 64 KiB sanity check
                log.error(f"Suspicious NCM meta length ({meta_len})")
                return None

            meta_json = _read_exact(f, meta_len)
            meta = json.loads(meta_json.decode("utf-8", errors="replace"))

            # Skip CRC/gap bytes (5 bytes)
            f.read(5)

            # --- Determine output format ---
            # The encrypted data starts here
            enc_data = f.read()

        # --- Decrypt ---
        cipher = AES.new(aes_key, AES.MODE_ECB)

        # The first block may be garbage (due to how NetEase pads/encrypts)
        # For FLAC, first 4 bytes are "fLaC"; for MP3, first bytes are ID3 or sync
        raw = cipher.decrypt(enc_data)

        # Detect audio format from decrypted data
        fmt = _detect_audio_format(raw)
        if fmt == "flac":
            ext = ".flac"
        else:
            ext = ".mp3"

        # Build output filename from metadata
        artist = meta.get("artist", [["Unknown Artist"]])
        if isinstance(artist, list) and len(artist) > 0:
            artist = artist[0][0] if isinstance(artist[0], list) else str(artist[0])
        else:
            artist = str(artist) if artist else "Unknown Artist"

        title = meta.get("musicName", input_path.stem)

        # Sanitize for filenames
        safe_artist = "".join(c for c in str(artist) if c not in r'<>:"/\|?*').strip()
        safe_title = "".join(c for c in str(title) if c not in r'<>:"/\|?*').strip()
        filename = f"{safe_artist} - {safe_title}{ext}"

        output_path = out_dir / filename

        # Avoid overwriting existing files
        counter = 1
        while output_path.exists():
            output_path = out_dir / f"{safe_artist} - {safe_title} ({counter}){ext}"
            counter += 1

        output_path.write_bytes(raw)
        log.info(f"Decrypted: {output_path.name}")

        # Save cover if embedded
        _dump_cover(output_path, meta)

        return str(output_path)

    except Exception as e:
        log.error(f"NCM decryption failed: {e}")
        return None


def _detect_audio_format(data: bytes) -> str:
    """Try to determine whether *data* is FLAC or MP3.

    Returns ``"flac"`` or ``"mp3"``.
    """
    # FLAC magic: "fLaC" at offset 0 (but might be shifted by ECB padding residue)
    if data[:4] == b"fLaC":
        return "flac"

    # MP3: ID3v2 header starts with "ID3"
    if data[:3] == b"ID3":
        return "mp3"

    # Try to find fLaC in first few bytes
    if b"fLaC" in data[:128]:
        return "flac"
    if b"ID3" in data[:128]:
        return "mp3"

    # Default to MP3
    return "mp3"


def decrypt_ncm_batch(input_paths: list[str], output_dir: str | None = None) -> int:
    """Decrypt multiple .ncm files.

    Returns the number of successfully decrypted files.
    """
    success = 0
    for path in input_paths:
        result = decrypt_ncm(path, output_dir)
        if result:
            success += 1
    log.info(f"Decrypted {success}/{len(input_paths)} files")
    return success


def decrypt_ncm_directory(dir_path: str, output_dir: str | None = None) -> int:
    """Decrypt all .ncm files in a directory recursively.

    Returns the number of successfully decrypted files.
    """
    ncm_files = list(Path(dir_path).rglob("*.ncm"))
    if not ncm_files:
        log.warning(f"No .ncm files found in {dir_path}")
        return 0
    log.info(f"Found {len(ncm_files)} .ncm file(s)")
    return decrypt_ncm_batch([str(f) for f in ncm_files], output_dir)
