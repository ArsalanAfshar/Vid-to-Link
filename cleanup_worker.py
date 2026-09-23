"""
cleanup_worker.py
-----------------
Automated background task that handles file expiration and cleanup:
- Runs continuously on a periodic schedule (every 60 seconds).
- Identifies download links in SQLite that have passed their `expires_at` timestamp.
- Safely deletes the physical file from disk to free up Railway free tier storage.
- Cleans up empty task directories.
- Marks expired links as deleted in SQLite.
- Scans for orphaned temporary files (.part, .tmp) left by cancelled downloads.
- Exposes `get_disk_usage_info()` for storage monitoring in admin panels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import config
import database
import utils

logger = logging.getLogger(__name__)


async def run_cleanup_cycle() -> dict[str, Any]:
    """Execute one pass of expired-file cleanup."""
    now = int(time.time())
    expired_links = await database.get_expired_file_links(now)

    freed_bytes = 0
    deleted_count = 0

    download_root = Path(config.DOWNLOAD_PATH).resolve()

    for row in expired_links:
        token = row["token"]
        raw_path = row["file_path"]
        try:
            file_path = Path(raw_path).resolve()
            # Ensure path is inside download_root for security
            if download_root in file_path.parents or file_path.parent == download_root:
                if file_path.exists() and file_path.is_file():
                    size = file_path.stat().st_size
                    file_path.unlink(missing_ok=True)
                    freed_bytes += size

                # Clean up parent directory if empty
                parent_dir = file_path.parent
                if parent_dir != download_root and parent_dir.exists() and parent_dir.is_dir():
                    try:
                        if not any(parent_dir.iterdir()):
                            parent_dir.rmdir()
                    except OSError:
                        pass
        except Exception as exc:
            logger.warning("Error deleting expired file %s: %s", raw_path, exc)

        try:
            await database.mark_file_link_deleted(token)
            deleted_count += 1
        except Exception as exc:
            logger.warning("Error marking link %s as deleted: %s", token, exc)

    # Sweep orphaned temp files older than 2 hours
    try:
        orphaned_freed = _cleanup_orphaned_temp_files(download_root, max_age_seconds=7200)
        freed_bytes += orphaned_freed
    except Exception as exc:
        logger.debug("Orphaned file cleanup sweep error: %s", exc)

    return {
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "timestamp": now,
    }


def _cleanup_orphaned_temp_files(download_root: Path, max_age_seconds: int = 7200) -> int:
    """Scan DOWNLOAD_PATH for orphaned .part, .tmp files older than max_age_seconds."""
    freed = 0
    now = time.time()
    if not download_root.exists():
        return 0

    for item in download_root.rglob("*"):
        try:
            if item.is_file() and (item.suffix in (".part", ".tmp", ".ytdl") or item.name.endswith(".temp")):
                mtime = item.stat().st_mtime
                if now - mtime > max_age_seconds:
                    size = item.stat().st_size
                    item.unlink(missing_ok=True)
                    freed += size
            elif item.is_dir() and item != download_root:
                mtime = item.stat().st_mtime
                if now - mtime > max_age_seconds and not any(item.iterdir()):
                    item.rmdir()
        except Exception:
            pass
    return freed


def get_disk_usage_info() -> dict[str, Any]:
    """Retrieve disk space statistics for DOWNLOAD_PATH."""
    try:
        stat = shutil.disk_usage(config.DOWNLOAD_PATH)
        total = stat.total
        free = stat.free
        used = stat.used
        percent = (used / total * 100) if total else 0.0
        return {
            "total_bytes": total,
            "free_bytes": free,
            "used_bytes": used,
            "percent": percent,
            "total_formatted": utils.format_bytes(total),
            "free_formatted": utils.format_bytes(free),
            "used_formatted": utils.format_bytes(used),
        }
    except Exception as exc:
        logger.debug("Failed to get disk usage: %s", exc)
        return {
            "total_bytes": 0,
            "free_bytes": 0,
            "used_bytes": 0,
            "percent": 0.0,
            "total_formatted": "unknown",
            "free_formatted": "unknown",
            "used_formatted": "unknown",
        }


async def startup_cleanup() -> None:
    """Run an initial cleanup pass when the bot starts up."""
    logger.info("Performing startup cleanup...")
    Path(config.DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)
    stats = await run_cleanup_cycle()
    if stats["deleted_count"] > 0:
        logger.info(
            "Startup cleanup: removed %d expired files, freed %s",
            stats["deleted_count"],
            utils.format_bytes(stats["freed_bytes"]),
        )


async def start_cleanup_worker(interval_seconds: int = 60) -> None:
    """Long-running background task that periodically runs the cleanup cycle."""
    logger.info("Background file cleanup worker started (interval: %ds).", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            stats = await run_cleanup_cycle()
            if stats["deleted_count"] > 0:
                logger.info(
                    "Cleanup cycle finished: removed %d expired file(s), freed %s",
                    stats["deleted_count"],
                    utils.format_bytes(stats["freed_bytes"]),
                )
        except asyncio.CancelledError:
            logger.info("Cleanup worker task cancelled.")
            break
        except Exception as exc:
            logger.exception("Unexpected error in cleanup worker: %s", exc)
