#!/usr/bin/env python3
"""Music Downloader — multi-platform music download CLI.

Usage:
    python main.py                          # interactive mode
    python main.py search <keyword>         # search and download
    python main.py url <url>                # download from URL
    python main.py ncm <file.ncm>           # decrypt NCM file
    python main.py ncm-dir <directory>      # batch decrypt NCM files
    python main.py --cookie "key=value"     # set authentication cookie
    python main.py --quality lossless       # preferred quality tier
    python main.py --output <dir>           # download directory
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path

# Make sure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent))

from core.downloader import DownloadManager
from core.utils import log, sanitize_filename, build_filename, format_duration
from platforms.netease import (
    NeteaseAPI,
    decrypt_ncm,
    decrypt_ncm_batch,
    decrypt_ncm_directory,
    parse_netease_url,
)


# ---------------------------------------------------------------------------
# CLI front-end
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Music Downloader — 多平台音乐下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                Interactive mode
  python main.py search "周杰伦 晴天"           Search and download
  python main.py url https://music.163.com/song?id=186016
  python main.py url https://music.163.com/playlist?id=123456
  python main.py ncm encrypted.ncm              Decrypt one NCM file
  python main.py ncm-dir ./ncm_files/           Decrypt all NCM files in dir
  python main.py --cookie "MUSIC_U=xxx;" --quality lossless
        """,
    )
    p.add_argument(
        "action", nargs="*",
        help="Action: 'search <kw>', 'url <url>', 'ncm <path>', 'ncm-dir <path>' (omit for interactive)",
    )
    p.add_argument(
        "--cookie", "-c",
        help="Cookie string for authentication (e.g. 'MUSIC_U=xxx; __csrf=yyy')",
    )
    p.add_argument(
        "--cookie-file",
        help="Path to a file containing a cookie string",
    )
    p.add_argument(
        "--quality", "-q",
        default="exhigh",
        choices=["standard", "higher", "exhigh", "lossless", "hires"],
        help="Preferred audio quality (default: exhigh / 320kbps)",
    )
    p.add_argument(
        "--output", "-o",
        default=".",
        help="Download directory (default: current directory)",
    )
    p.add_argument(
        "--api-base",
        help="Custom API base URL (e.g. https://your-proxy.vercel.app) for geo-unblock",
    )
    p.add_argument(
        "--proxy",
        help="HTTP proxy URL (e.g. http://127.0.0.1:7890)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return p


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _print_song_list(songs: list, start_idx: int = 1):
    """Print a formatted song list."""
    print()
    for i, song in enumerate(songs, start_idx):
        duration = format_duration(song.duration_ms)
        print(f"  [{i:2d}]  {song.title}")
        print(f"         {song.artist}  |  {duration}  |  {song.album}")
        if song.song_id:
            print(f"         ID: {song.song_id}")
    print()


def _select_songs(songs: list, prompt: str = "Enter song numbers") -> list:
    """Let the user pick songs by number. Supports ranges like '1-5' and commas."""
    _print_song_list(songs)
    raw = input(f"{prompt} (e.g. 1,3,5-7, or 'all'): ").strip()
    if raw.lower() == "all":
        return songs

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                selected.extend(range(int(a.strip()), int(b.strip()) + 1))
            except ValueError:
                pass
        else:
            try:
                selected.append(int(part))
            except ValueError:
                pass

    return [s for i, s in enumerate(songs, 1) if i in selected]


def _pick_quality(available: list) -> str:
    """Let the user pick a quality tier."""
    from core.platform_base import SongQuality
    print("\nAvailable quality tiers:")
    for i, q in enumerate(available, 1):
        status = "✓ available" if q.is_available else ("✗ VIP required" if q.is_vip else "✗ unavailable")
        print(f"  [{i}] {q.quality_label}  ({q.format.upper()})  [{status}]")
    print()

    while True:
        choice = input("Choose quality [1] or press Enter for best available: ").strip()
        if not choice:
            # Pick the best available tier
            for q in available:
                if q.is_available:
                    return q.quality_label.split()[0]  # "标准", "极高", etc.
            return "standard"
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                return available[idx].quality_label.split()[0]
        except ValueError:
            pass
        print("Invalid choice, try again.")


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------

async def download_song(
    api: NeteaseAPI,
    song_id: str,
    quality_level: str,
    output_dir: str,
    downloader: DownloadManager,
) -> bool:
    """Fetch metadata + URL, then download one song.

    Returns True on success.
    """
    # 1. Get song detail (for filename metadata)
    detail = await api.get_song_detail(song_id)
    if detail is None:
        print(f"[ERROR] Could not fetch song detail for ID: {song_id}")
        return False

    # 2. Get download URL (with quality fallback)
    url = await api.get_song_url(song_id, quality_level)
    if not url:
        print(f"[ERROR] Could not get download URL for: {detail.title} - {detail.artist}")
        print(f"        Song audio URLs are geo-restricted to mainland China.")
        print(f"        Options:")
        print(f"          1. Use --api-base with a community proxy server")
        print(f"          2. Use --proxy with a Chinese HTTP proxy")
        print(f"          3. Use --cookie with a VIP account (for VIP songs)")
        return False

    # 3. Build filename
    ext = "flac" if quality_level in ("lossless", "hires") else "mp3"
    filename = build_filename(detail.artist, detail.title, ext)

    # 4. Download
    print(f"\n  Downloading: {detail.title} - {detail.artist}")
    print(f"  Quality: {quality_level}  |  File: {filename}")

    success = await downloader.download(url, filename)
    return success


async def download_playlist(
    api: NeteaseAPI,
    playlist_id: str,
    quality_level: str,
    output_dir: str,
    downloader: DownloadManager,
):
    """Download all songs from a playlist."""
    print(f"\nFetching playlist {playlist_id}...")
    songs = await api.get_playlist(playlist_id)
    if not songs:
        print("[ERROR] No songs found or playlist is empty/private.")
        return

    print(f"\nPlaylist contains {len(songs)} songs.\n")

    # Show and let user select
    selected = _select_songs(
        songs,
        prompt="Enter numbers to download"
        if len(songs) <= 30
        else f"Found {len(songs)} songs. Enter numbers to download",
    )

    if not selected:
        print("No songs selected.")
        return

    print(f"\nDownloading {len(selected)} song(s)...\n")
    success = 0
    for i, song in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {song.title} - {song.artist}")
        ok = await download_song(api, song.song_id, quality_level, output_dir, downloader)
        if ok:
            success += 1
        print()

    print(f"\nDone: {success}/{len(selected)} downloaded successfully.")


async def handle_url(
    api: NeteaseAPI,
    url: str,
    quality_level: str,
    output_dir: str,
    downloader: DownloadManager,
):
    """Handle a NetEase URL — song, playlist, or album."""
    parsed = parse_netease_url(url)
    if not parsed:
        print(f"[ERROR] Unrecognized URL: {url}")
        print("        Supported: music.163.com/song, /playlist, /album")
        return

    type_, id_ = parsed
    if type_ == "song":
        await download_song(api, id_, quality_level, output_dir, downloader)
    elif type_ == "playlist":
        await download_playlist(api, id_, quality_level, output_dir, downloader)
    elif type_ == "album":
        # Albums use playlist endpoint with album ID
        print(f"Album downloads use playlist-style fetch for {id_}")
        await download_playlist(api, id_, quality_level, output_dir, downloader)


async def handle_search(
    api: NeteaseAPI,
    keyword: str,
    quality_level: str,
    output_dir: str,
    downloader: DownloadManager,
):
    """Interactive search → select → download flow."""
    print(f'\nSearching for: "{keyword}" ...\n')
    result = await api.search(keyword)
    if not result.songs:
        print("No results found.")
        return

    print(f"Found {result.total} results (showing top {len(result.songs)}):")
    selected = _select_songs(result.songs)

    if not selected:
        print("No songs selected.")
        return

    # Get full details for each selected song
    for song in selected:
        print(f"\n--- {song.title} - {song.artist} ---")
        detail = await api.get_song_detail(song.song_id)
        if detail and detail.qualities:
            q = _pick_quality(detail.qualities)
            await download_song(api, song.song_id, q, output_dir, downloader)
        else:
            await download_song(api, song.song_id, quality_level, output_dir, downloader)


async def interactive_mode(
    api: NeteaseAPI,
    quality_level: str,
    output_dir: str,
    downloader: DownloadManager,
):
    """Main interactive menu."""
    print("\n" + "=" * 50)
    print("        音乐下载器 - Music Downloader")
    print("=" * 50)

    auth_status = "✓ Authenticated" if api.is_authenticated else "✗ Not logged in"
    api_info = api._api._api_base
    api_status = f"Custom: {api_info}" if api_info != "https://music.163.com" else "Direct (may need CN IP for downloads)"
    print(f"  Platform: NetEase Cloud Music (网易云音乐)")
    print(f"  Auth:     {auth_status}")
    print(f"  API:      {api_status}")
    print(f"  Quality:  {quality_level}")
    print(f"  Output:   {os.path.abspath(output_dir)}")
    print("=" * 50)

    while True:
        print("\n--- Menu ---")
        print("  [1] Search for songs")
        print("  [2] Download from URL (song / playlist / album)")
        print("  [3] Decrypt NCM file")
        print("  [4] Batch decrypt NCM files (directory)")
        print("  [5] Import cookies")
        print("  [q] Quit")
        print()

        choice = input("Choice: ").strip().lower()

        if choice == "1":
            kw = input("\nSearch keyword: ").strip()
            if kw:
                await handle_search(api, kw, quality_level, output_dir, downloader)

        elif choice == "2":
            url = input("\nURL: ").strip()
            if url:
                await handle_url(api, url, quality_level, output_dir, downloader)

        elif choice == "3":
            path = input("\nNCM file path: ").strip()
            if path:
                result = decrypt_ncm(path, output_dir)
                if result:
                    print(f"Decrypted: {result}")

        elif choice == "4":
            path = input("\nDirectory path: ").strip()
            if path:
                decrypt_ncm_directory(path, output_dir)

        elif choice == "5":
            print("\nPaste your cookie string (from browser DevTools):")
            print("  e.g. MUSIC_U=xxx; __csrf=yyy; ...")
            cookie_str = input("> ").strip()
            if cookie_str:
                api.import_cookie_string(cookie_str)
                print(f"Cookies imported. Authenticated: {api.is_authenticated}")

        elif choice in ("q", "quit", "exit"):
            print("\nGoodbye!")
            break

        else:
            print("Invalid choice.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = build_parser().parse_args()

    if args.verbose:
        import logging
        log.setLevel(logging.DEBUG)

    # Set up download directory
    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    # Build API and downloader
    api = NeteaseAPI(api_base=args.api_base or "", proxy=args.proxy or "")
    downloader = DownloadManager(download_dir=output_dir)

    # Load cookies
    cookies = {}
    if args.cookie_file:
        try:
            cookie_str = Path(args.cookie_file).read_text().strip()
            args.cookie = cookie_str
        except Exception as e:
            log.error(f"Cannot read cookie file: {e}")

    if args.cookie:
        api.import_cookie_string(args.cookie)
        log.info(f"Cookies loaded. Authenticated: {api.is_authenticated}")

    # Determine action
    action_words = args.action if args.action else []

    # Dispatch
    if not action_words:
        # Interactive mode
        asyncio.run(interactive_mode(api, args.quality, output_dir, downloader))

    elif action_words[0] == "search":
        keyword = " ".join(action_words[1:])
        if not keyword:
            keyword = input("Search keyword: ").strip()
        if keyword:
            asyncio.run(handle_search(api, keyword, args.quality, output_dir, downloader))
        else:
            print("No search keyword provided.")

    elif action_words[0] == "url":
        url = action_words[1] if len(action_words) > 1 else input("URL: ").strip()
        if url:
            asyncio.run(handle_url(api, url, args.quality, output_dir, downloader))

    elif action_words[0] == "ncm":
        path = action_words[1] if len(action_words) > 1 else input("NCM file: ").strip()
        if path:
            result = decrypt_ncm(path, output_dir)
            if result:
                print(f"Decrypted: {result}")

    elif action_words[0] == "ncm-dir":
        path = action_words[1] if len(action_words) > 1 else input("Directory: ").strip()
        if path:
            decrypt_ncm_directory(path, output_dir)

    else:
        print(f"Unknown action: {action_words[0]}")
        print("Available: search, url, ncm, ncm-dir")
        print("Or run without arguments for interactive mode.")


if __name__ == "__main__":
    main()
