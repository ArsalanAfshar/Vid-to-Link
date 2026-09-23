"""
utils.py
--------
Shared helper functions: URL validation, byte/speed/duration formatting,
progress bars, rate limiting, safe file deletion, ZIP archiving, media sniffing,
ffprobe stream validation, and expiration time helpers.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_URL_REGEX = re.compile(
    r"^(https?://)"
    r"([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}"
    r"(:\d{1,5})?"
    r"(/[^\s]*)?$",
    re.IGNORECASE,
)

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def is_valid_url(text: str) -> bool:
    """Validate a URL and reject internal/SSRF-prone hosts."""
    if not text or len(text) > 2000:
        return False
    text = text.strip()
    if not _URL_REGEX.match(text):
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _BLOCKED_HOSTS:
        return False
    if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("169.254."):
        return False
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", hostname):
        return False
    return True


def extract_first_url(text: str) -> Optional[str]:
    """Extract the first valid URL found in an arbitrary text string."""
    for token in text.split():
        token = token.strip()
        if is_valid_url(token):
            return token
    return None


def format_bytes(size: float | int | None) -> str:
    """Format byte count into human-readable string (B, KB, MB, GB, TB)."""
    if size is None or size < 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    val = float(size)
    while val >= 1024 and index < len(units) - 1:
        val /= 1024
        index += 1
    return f"{val:.1f} {units[index]}"


def format_speed(bytes_per_second: float | int | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "unknown"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    sec = int(seconds)
    hours, remainder = divmod(sec, 3600)
    minutes, s = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{s:02d}"
    return f"{minutes:02d}:{s:02d}"


def format_eta(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    return format_duration(seconds)


def format_time_remaining(seconds: float | int | None) -> str:
    """Format duration in Persian for user-facing expiration texts."""
    if seconds is None:
        return "نامشخص"
    sec = int(max(0, seconds))
    if sec < 60:
        return f"{sec} ثانیه"
    minutes, s = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    return " و ".join(parts) if parts else f"{s} ثانیه"


def format_time_remaining_en(seconds: float | int | None) -> str:
    """Format duration in English for admin panels."""
    if seconds is None:
        return "unknown"
    sec = int(max(0, seconds))
    if sec < 60:
        return f"{sec}s"
    minutes, s = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else f"{s}s"


def format_timestamp_utc(ts: int) -> str:
    """Format a UNIX timestamp as YYYY-MM-DD HH:MM UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_time_duration(text: str) -> Optional[int]:
    """Parse time string like '30m', '2h', '1d', '3600' into total seconds."""
    text = text.strip().lower()
    if not text:
        return None
    if text.isdigit():
        val = int(text)
        return val if val > 0 else None
    m = re.match(
        r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
        text,
    )
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    if unit.startswith("s"):
        seconds = int(val)
    elif unit.startswith("m"):
        seconds = int(val * 60)
    elif unit.startswith("h"):
        seconds = int(val * 3600)
    elif unit.startswith("d"):
        seconds = int(val * 86400)
    else:
        return None
    return seconds if seconds > 0 else None


def progress_bar(percent: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(length * percent / 100)
    return "🟩" * filled + "⬜️" * (length - filled)


def safe_delete(path: str | Path) -> None:
    """Delete a file or directory without raising."""
    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Strip characters unsafe for filenames while preserving readability."""
    name = re.sub(r'[\\/*?:"<>|\n\r\t]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    if not name:
        name = "file"
    return name[:max_length]


def build_track_filename(title: str, artist: Optional[str] = None, ext: str = "mp3") -> str:
    """Build a filename for audio tracks."""
    safe_title = sanitize_filename(title or "track", max_length=120)
    return f"{safe_title}.{ext.lstrip('.')}"


def unique_path(directory: str | Path, filename: str) -> Path:
    """Avoid overwriting an existing file by appending an incrementing counter."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def create_zip(files: list[str | Path], zip_path: str | Path) -> Path:
    """Create a ZIP archive streamed from a list of files with low RAM usage."""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, f in enumerate(files, start=1):
            f = Path(f)
            if f.exists() and f.is_file():
                zf.write(f, arcname=f"{index:02d}_{f.name}")
    return zip_path


_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _read_header(path: str | Path, length: int = 32) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(length)
    except OSError:
        return b""


def sniff_kind(path: str | Path) -> str:
    """Best-effort detection of a file's type from its magic bytes."""
    header = _read_header(path, 32)
    if not header:
        return "unknown"

    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image"

    for magic, _ext in _IMAGE_SIGNATURES:
        if header.startswith(magic):
            return "image"

    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "video"

    if header.startswith(b"\x1a\x45\xdf\xa3"):  # WebM / Matroska
        return "video"

    if header.startswith(b"\x47"):  # MPEG-TS
        return "video"

    if header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"\xff\xf3"):
        return "audio"

    return "unknown"


def sniff_image_extension(path: str | Path) -> Optional[str]:
    """Return correct file extension for image based on magic bytes."""
    header = _read_header(path, 16)
    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    for magic, ext in _IMAGE_SIGNATURES:
        if header.startswith(magic):
            return ext
    return None


def guess_mime(path: str | Path, default: str = "application/octet-stream") -> str:
    """Guess MIME type based on file extension."""
    m_type, _ = mimetypes.guess_type(str(path))
    return m_type or default


async def has_video_stream(path: str) -> Optional[bool]:
    """Use ffprobe to verify whether a file genuinely contains a video stream."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None
        return "video" in stdout.decode(errors="ignore").strip().lower()
    except FileNotFoundError:
        logger.debug("ffprobe not found; falling back to magic byte sniffing.")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe error: %s", exc)
        return None


class RateLimiter:
    """Sliding-window per-user request rate limiter."""

    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = {}

    def check(self, user_id: int, max_count: int, window_seconds: int) -> tuple[bool, float]:
        now = time.monotonic()
        hits = self._hits.setdefault(user_id, [])
        hits[:] = [t for t in hits if now - t < window_seconds]
        if len(hits) >= max_count:
            retry_after = window_seconds - (now - hits[0])
            return False, max(retry_after, 0.0)
        hits.append(now)
        return True, 0.0


def throttle(min_interval: float = 3.0):
    """Decorator limiting how often a function can run to prevent Telegram flood limits."""

    def decorator(func):
        last_called: dict[str, float] = {"t": 0.0}

        def wrapper(*args, **kwargs):
            now = time.monotonic()
            force = kwargs.pop("force", False)
            if not force and now - last_called["t"] < min_interval:
                return None
            last_called["t"] = now
            return func(*args, **kwargs)

        return wrapper

    return decorator
