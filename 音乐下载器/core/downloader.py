"""Download manager with progress, retry, and concurrency support."""

import os
import sys
import time
import asyncio
import hashlib
from pathlib import Path
from typing import Optional, Callable

import aiohttp
import requests

from .utils import log, format_size


class DownloadManager:
    """Handles file downloads with progress display, retry, and validation."""

    def __init__(
        self,
        download_dir: str = ".",
        max_retries: int = 3,
        chunk_size: int = 8192,
        concurrent: int = 3,
        timeout: int = 30,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.concurrent = concurrent
        self.timeout = timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        }

    def set_cookies(self, cookies: dict):
        """Set cookies for authenticated downloads."""
        self._cookies = cookies

    async def download(
        self,
        url: str,
        filepath: str,
        headers: Optional[dict] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """Download a file to the given path with progress and retry.

        Args:
            url: Direct download URL
            filepath: Destination path (relative to download_dir or absolute)
            headers: Extra HTTP headers
            on_progress: Callback(downloaded_bytes, total_bytes)

        Returns:
            True on success, False on failure after all retries.
        """
        output_path = self._resolve_path(filepath)

        for attempt in range(1, self.max_retries + 1):
            try:
                result = await self._do_download(url, output_path, headers, on_progress)
                if result:
                    return True
                log.warning(f"Download failed (attempt {attempt}/{self.max_retries})")
            except aiohttp.ClientError as e:
                log.warning(f"Network error (attempt {attempt}/{self.max_retries}): {e}")
            except asyncio.TimeoutError:
                log.warning(f"Download timed out (attempt {attempt}/{self.max_retries})")
            except OSError as e:
                log.error(f"File error: {e}")
                return False

            if attempt < self.max_retries:
                wait = 2 ** attempt
                log.info(f"Retrying in {wait}s...")
                await asyncio.sleep(wait)

        log.error(f"Failed to download after {self.max_retries} attempts: {url}")
        return False

    async def download_sync(self, url: str, filepath: str) -> bool:
        """Synchronous wrapper using requests (for non-async contexts)."""
        output_path = self._resolve_path(filepath)

        for attempt in range(1, self.max_retries + 1):
            try:
                headers = self._headers.copy()
                cookies = getattr(self, "_cookies", {})
                resp = requests.get(
                    url, headers=headers, cookies=cookies,
                    timeout=self.timeout, stream=True,
                )
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=self.chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            self._print_progress(downloaded, total)

                if total > 0 and downloaded < total:
                    log.warning(f"Incomplete download ({downloaded}/{total})")
                    continue

                self._print_progress(total, total)
                print()  # newline after progress
                log.info(f"Downloaded: {output_path.name}")
                return True

            except requests.RequestException as e:
                log.warning(f"Download error (attempt {attempt}/{self.max_retries}): {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)

        return False

    async def _do_download(
        self,
        url: str,
        output_path: Path,
        extra_headers: Optional[dict],
        on_progress: Optional[Callable],
    ) -> bool:
        """Internal async download implementation."""
        headers = self._headers.copy()
        if extra_headers:
            headers.update(extra_headers)
        cookies = getattr(self, "_cookies", {})

        timeout = aiohttp.ClientTimeout(total=self.timeout * 10, connect=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, cookies=cookies) as resp:
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(output_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(self.chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(downloaded, total)
                        else:
                            self._print_progress(downloaded, total)

                if total > 0 and downloaded < total:
                    return False

                if not on_progress:
                    self._print_progress(total, total)
                    print()
                log.info(f"Downloaded: {output_path.name}")
                return True

    def _resolve_path(self, filepath: str) -> Path:
        """Resolve a filepath to an absolute path."""
        p = Path(filepath)
        if p.is_absolute():
            return p
        return self.download_dir / p

    def _print_progress(self, downloaded: int, total: int):
        """Print a compact progress bar to stdout."""
        if total <= 0:
            # Unknown size — show a spinner-like indicator
            bar = f"\r  [{downloaded // 1024}KB] downloading..."
            sys.stdout.write(bar)
            sys.stdout.flush()
            return

        pct = min(downloaded / total, 1.0)
        bar_len = 30
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)

        d_size = format_size(downloaded)
        t_size = format_size(total)

        sys.stdout.write(f"\r  [{bar}] {pct*100:.0f}% {d_size}/{t_size}")
        sys.stdout.flush()


# Singleton for convenience
_downloader: Optional[DownloadManager] = None


def get_downloader(download_dir: str = ".") -> DownloadManager:
    global _downloader
    if _downloader is None:
        _downloader = DownloadManager(download_dir=download_dir)
    else:
        _downloader.download_dir = Path(download_dir)
    return _downloader
