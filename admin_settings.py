"""
admin_settings.py
------------------
Runtime-configurable admin settings for Vid-to-Link.
Allows the owner/admins to configure without code changes or restarts:
 1. File expiration duration (e.g. 15m, 30m, 1h, 2h, 6h, 24h, custom).
 2. Download limits (max file size, max concurrent downloads).
 3. User cooldown settings (count before cooldown, cooldown duration).
 4. User-facing message overrides.

All changes are persisted immediately in SQLite and take effect dynamically.
"""

from __future__ import annotations

import logging
from typing import Optional

import config
import database
import messages

logger = logging.getLogger(__name__)

# Settings keys stored in the database
_KEY_MAX_FILE_SIZE = "admin_max_file_size"
_KEY_MAX_CONCURRENT = "admin_max_concurrent_downloads"
_KEY_COOLDOWN_ENABLED = "admin_cooldown_enabled"
_KEY_COOLDOWN_COUNT = "admin_cooldown_count"
_KEY_COOLDOWN_SECONDS = "admin_cooldown_seconds"
_KEY_FILE_EXPIRATION = "file_expiration_seconds"
_MSG_OVERRIDE_PREFIX = "msg_override:"

# Quick selection presets for file expiration in admin menus
FILE_EXPIRATION_PRESETS: list[tuple[int, str, str]] = [
    (900, "15 دقیقه", "15m"),
    (1800, "30 دقیقه", "30m"),
    (3600, "1 ساعت", "1h"),
    (7200, "2 ساعت", "2h"),
    (14400, "4 ساعت", "4h"),
    (21600, "6 ساعت", "6h"),
    (43200, "12 ساعت", "12h"),
    (86400, "24 ساعت", "24h"),
    (172800, "48 ساعت", "48h"),
]


# ---------------------------------------------------------------------------
# File Expiration
# ---------------------------------------------------------------------------
async def get_file_expiration_seconds() -> int:
    return await database.get_file_expiration_seconds()


async def set_file_expiration_seconds(seconds: int) -> None:
    await database.set_file_expiration_seconds(seconds)
    logger.info("Admin updated file expiration to %d seconds", seconds)


# ---------------------------------------------------------------------------
# Download Limits
# ---------------------------------------------------------------------------
async def get_max_file_size() -> int:
    return await database.get_max_file_size()


async def set_max_file_size(size_bytes: int) -> None:
    await database.set_max_file_size(size_bytes)
    logger.info("Admin updated max file size to %d bytes", size_bytes)


async def get_max_concurrent_downloads() -> int:
    return await database.get_max_concurrent_downloads()


async def set_max_concurrent_downloads(value: int) -> None:
    value = max(1, int(value))
    await database.set_max_concurrent_downloads(value)
    try:
        import asyncio
        import concurrency

        concurrency.download_semaphore = asyncio.Semaphore(value)
        logger.info("Download semaphore resized to %d at runtime", value)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to resize download semaphore")


async def get_cooldown_config() -> dict:
    return await database.get_cooldown_settings()


async def set_cooldown_config(
    *,
    enabled: Optional[bool] = None,
    count: Optional[int] = None,
    seconds: Optional[int] = None,
) -> dict:
    await database.set_cooldown_settings(enabled=enabled, count=count, seconds=seconds)
    return await database.get_cooldown_settings()


# ---------------------------------------------------------------------------
# Message Overrides
# ---------------------------------------------------------------------------
def customizable_keys() -> tuple[str, ...]:
    return getattr(messages, "CUSTOMIZABLE_KEYS", ())


def get_default_text(key: str) -> str:
    return messages.MESSAGES.get(key, "")


def get_current_text(key: str) -> str:
    overrides = getattr(messages, "_OVERRIDES", {})
    return overrides.get(key) or messages.MESSAGES.get(key, "")


async def set_message_override(key: str, text: str) -> bool:
    if key not in customizable_keys():
        return False
    await database.set_message_override(key, text)
    messages._OVERRIDES[key] = text
    return True


async def reset_message_override(key: str) -> bool:
    if key not in customizable_keys():
        return False
    await database.reset_message_override(key)
    messages._OVERRIDES.pop(key, None)
    return True


async def load_overrides() -> None:
    """Call once on startup after database.init_db()."""
    overrides = await database.get_all_message_overrides()
    for key, val in overrides.items():
        if key in customizable_keys() and val:
            messages._OVERRIDES[key] = val
    logger.info("Loaded %d custom message override(s)", len(messages._OVERRIDES))
