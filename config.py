"""
config.py
---------
Central configuration module for Vid-to-Link.
All sensitive settings are loaded from environment variables (Railway Variables
in production, or a local `.env` file for local development).

Built for Railway's Free Tier:
- Integrated HTTP file server on 0.0.0.0:$PORT
- Direct HTTP download links with automatic public domain detection
- Configurable file expiration and automatic disk cleanup
- Support for large files (>2GB) via zero-copy streaming
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


# ---------------------------------------------------------------------------
# Telegram Bot MTProto Credentials
# ---------------------------------------------------------------------------
API_ID: int = _get_int("API_ID", 0)
API_HASH: str = os.getenv("API_HASH", "").strip()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
SESSION_STRING: str = os.getenv("SESSION_STRING", "").strip()

# ---------------------------------------------------------------------------
# Admin / Owner
# ---------------------------------------------------------------------------
OWNER_ID: int = _get_int("OWNER_ID", 0)
MANAGER_ADMIN_IDS: list[int] = _get_int_list("MANAGER_ADMIN_IDS")
MANAGER_CHAT_ID: int = _get_int("MANAGER_CHAT_ID", 0) or OWNER_ID

# NexiLink / Vid-to-Link Manager Bot (optional dedicated admin bot)
MANAGER_BOT_TOKEN: str = os.getenv("MANAGER_BOT_TOKEN", "").strip()
MANAGER_ENABLED: bool = _get_bool("MANAGER_ENABLED", True) and bool(MANAGER_BOT_TOKEN)

# ---------------------------------------------------------------------------
# Direct Link HTTP File Server & Railway Networking
# ---------------------------------------------------------------------------
PORT: int = _get_int("PORT", 8080)
BASE_URL: str = os.getenv("BASE_URL", "").strip().rstrip("/")


def get_base_url() -> str:
    """
    Determine the public base URL for direct download links.
    1. Custom BASE_URL if explicitly set (e.g. https://mybot.up.railway.app).
    2. RAILWAY_PUBLIC_DOMAIN injected by Railway when public networking is generated.
    3. RAILWAY_STATIC_URL injected by Railway.
    4. Fallback to http://localhost:{PORT} for local testing.
    """
    if BASE_URL:
        return BASE_URL

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        if not railway_domain.startswith("http://") and not railway_domain.startswith("https://"):
            return f"https://{railway_domain}"
        return railway_domain.rstrip("/")

    railway_static = os.getenv("RAILWAY_STATIC_URL", "").strip()
    if railway_static:
        if not railway_static.startswith("http://") and not railway_static.startswith("https://"):
            return f"https://{railway_static}"
        return railway_static.rstrip("/")

    return f"http://localhost:{PORT}"


# ---------------------------------------------------------------------------
# File Expiration & Storage (Railway Free Tier)
# ---------------------------------------------------------------------------
# Default link validity before automatic deletion: 7200 seconds = 2 hours.
# Runtime changeable by owner via /expiration or manager bot without restart.
DEFAULT_FILE_EXPIRATION_SECONDS: int = _get_int("FILE_EXPIRATION_SECONDS", 7200)

DOWNLOAD_PATH: str = os.getenv("DOWNLOAD_PATH", "/tmp/downloads").strip() or "/tmp/downloads"
DB_PATH: str = os.getenv("DB_PATH", "bot_data.db").strip() or "bot_data.db"

# Max file size limit: 10 GB default. Supports files > 2GB (Telegram's 2GB cap
# is no longer relevant because files are served directly over HTTP).
# Set to 0 in environment variables for unlimited (bounded only by disk).
MAX_FILE_SIZE: int = _get_int("MAX_FILE_SIZE", 10 * 1024 * 1024 * 1024)

# Railway Free Plan has limited RAM (typically 512MB) and CPU: keep concurrency low.
MAX_CONCURRENT_DOWNLOADS: int = _get_int("MAX_CONCURRENT_DOWNLOADS", 2)

# Rate limiting per user
RATE_LIMIT_COUNT: int = _get_int("RATE_LIMIT_COUNT", 5)
RATE_LIMIT_WINDOW: int = _get_int("RATE_LIMIT_WINDOW", 60)

# Cooldown system
COOLDOWN_ENABLED: bool = _get_bool("COOLDOWN_ENABLED", False)
COOLDOWN_COUNT: int = _get_int("COOLDOWN_COUNT", 5)
COOLDOWN_SECONDS: int = _get_int("COOLDOWN_SECONDS", 60)

# Carousel / playlist ZIP threshold
ZIP_THRESHOLD_ITEMS: int = _get_int("ZIP_THRESHOLD_ITEMS", 6)

# ---------------------------------------------------------------------------
# Supported Platforms & Optional Features
# ---------------------------------------------------------------------------
PORNHUB_ENABLED: bool = _get_bool("PORNHUB_ENABLED", True)
PORNHUB_NOTIFY_ADMIN: bool = _get_bool("PORNHUB_NOTIFY_ADMIN", True)
PINTEREST_ENABLED: bool = _get_bool("PINTEREST_ENABLED", True)
INSTAGRAM_ENABLED: bool = _get_bool("INSTAGRAM_ENABLED", True)
TWITTER_ENABLED: bool = _get_bool("TWITTER_ENABLED", True)

# Optional Netscape cookies
INSTAGRAM_COOKIES: str = os.getenv("INSTAGRAM_COOKIES", "").strip()
YOUTUBE_COOKIES: str = os.getenv("YOUTUBE_COOKIES", "").strip()
EXTRA_COOKIES: str = os.getenv("EXTRA_COOKIES", "").strip()

# SoundCloud playlist track limit
MAX_PLAYLIST_TRACKS: int = _get_int("MAX_PLAYLIST_TRACKS", 100)

# Reporting
DEFAULT_REPORT_INTERVAL_MINUTES: int = _get_int("DEFAULT_REPORT_INTERVAL_MINUTES", 10)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
DEVICE_MODEL = "Vid-to-Link Downloader Bot"
APP_VERSION = "1.0.0"

Path(DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    """Configure application-wide logging."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)


def validate_config() -> list[str]:
    """Validate required environment variables."""
    errors: list[str] = []
    if not API_ID:
        errors.append("API_ID is not set.")
    if not API_HASH:
        errors.append("API_HASH is not set.")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set.")
    if not OWNER_ID:
        errors.append("OWNER_ID is not set.")
    return errors
