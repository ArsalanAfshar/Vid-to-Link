"""
database.py
-----------
SQLite persistence layer (via aiosqlite) for Vid-to-Link.
Stores:
 - Registered users (for broadcast + usage statistics).
 - Download event history.
 - Generated direct download links and their expiration timestamps (`file_links`).
 - Dynamic admin runtime settings (expiration duration, download limits, cooldown).
 - Broadcast tasks and history.

Tables are initialized automatically on startup.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiosqlite

import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    download_count INTEGER NOT NULL DEFAULT 0,
    is_banned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    url TEXT NOT NULL,
    platform TEXT,
    quality TEXT,
    file_size INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS file_links (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mime_type TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    download_count INTEGER NOT NULL DEFAULT 0,
    is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_file_links_expires ON file_links(expires_at, is_deleted);
CREATE INDEX IF NOT EXISTS idx_file_links_user ON file_links(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER
);
"""

_connection: Optional[aiosqlite.Connection] = None


async def init_db() -> None:
    """Create tables if needed and open the shared connection."""
    global _connection
    _connection = await aiosqlite.connect(config.DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(_SCHEMA)
    await _connection.commit()
    logger.info("Database ready: %s", config.DB_PATH)


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _conn() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database has not been initialised. Call init_db() first.")
    return _connection


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
async def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
    """Insert or update a user. Returns True if this is a brand-new user."""
    conn = _conn()
    now = int(time.time())
    async with conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
        existing = await cursor.fetchone()
    await conn.execute(
        """
        INSERT INTO users (user_id, username, first_name, first_seen, last_seen, download_count)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
        """,
        (user_id, username, first_name, now, now),
    )
    await conn.commit()
    return existing is None


async def increment_user_downloads(user_id: int) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE users SET download_count = download_count + 1 WHERE user_id = ?", (user_id,)
    )
    await conn.commit()


async def is_user_banned(user_id: int) -> bool:
    conn = _conn()
    async with conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    return bool(row and row["is_banned"])


async def set_user_banned(user_id: int, banned: bool) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id)
    )
    await conn.commit()


async def get_user_count() -> int:
    conn = _conn()
    async with conn.execute("SELECT COUNT(*) AS c FROM users") as cursor:
        row = await cursor.fetchone()
    return int(row["c"]) if row else 0


async def get_all_user_ids() -> list[int]:
    conn = _conn()
    async with conn.execute("SELECT user_id FROM users WHERE is_banned = 0") as cursor:
        rows = await cursor.fetchall()
    return [int(r["user_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Download history / events
# ---------------------------------------------------------------------------
async def add_download(
    user_id: int,
    url: str,
    platform: Optional[str],
    quality: Optional[str],
    file_size: Optional[int],
    status: str,
    error: Optional[str] = None,
    username: Optional[str] = None,
) -> None:
    conn = _conn()
    await conn.execute(
        """
        INSERT INTO downloads (user_id, username, url, platform, quality, file_size, status, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, url, platform, quality, file_size, status, error, int(time.time())),
    )
    await conn.commit()
    if status == "success":
        await increment_user_downloads(user_id)


async def get_download_count(status: Optional[str] = None, since_ts: Optional[int] = None) -> int:
    conn = _conn()
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if since_ts:
        clauses.append("created_at >= ?")
        params.append(since_ts)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with conn.execute(f"SELECT COUNT(*) AS c FROM downloads{where}", params) as cursor:
        row = await cursor.fetchone()
    return int(row["c"]) if row else 0


async def get_stats() -> dict[str, int]:
    return {
        "total_users": await get_user_count(),
        "total_downloads": await get_download_count(),
        "successful_downloads": await get_download_count("success"),
        "failed_downloads": await get_download_count("failed"),
    }


async def get_recent_downloads(limit: int = 20) -> list[aiosqlite.Row]:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
    ) as cursor:
        return list(await cursor.fetchall())


# ---------------------------------------------------------------------------
# Direct Download File Links & Expiration
# ---------------------------------------------------------------------------
async def create_file_link(
    token: str,
    user_id: int,
    file_path: str,
    file_name: str,
    file_size: int,
    expires_at: int,
    mime_type: Optional[str] = None,
) -> None:
    conn = _conn()
    now = int(time.time())
    await conn.execute(
        """
        INSERT INTO file_links (token, user_id, file_path, file_name, file_size, mime_type, created_at, expires_at, download_count, is_deleted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """,
        (token, user_id, file_path, file_name, file_size, mime_type, now, expires_at),
    )
    await conn.commit()


async def get_file_link(token: str) -> Optional[aiosqlite.Row]:
    conn = _conn()
    async with conn.execute("SELECT * FROM file_links WHERE token = ?", (token,)) as cursor:
        return await cursor.fetchone()


async def increment_link_download_count(token: str) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE file_links SET download_count = download_count + 1 WHERE token = ?", (token,)
    )
    await conn.commit()


async def mark_file_link_deleted(token: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE file_links SET is_deleted = 1 WHERE token = ?", (token,))
    await conn.commit()


async def get_expired_file_links(now: Optional[int] = None) -> list[aiosqlite.Row]:
    if now is None:
        now = int(time.time())
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM file_links WHERE expires_at <= ? AND is_deleted = 0", (now,)
    ) as cursor:
        return list(await cursor.fetchall())


async def get_active_file_links(limit: int = 50) -> list[aiosqlite.Row]:
    now = int(time.time())
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM file_links WHERE expires_at > ? AND is_deleted = 0 ORDER BY created_at DESC LIMIT ?",
        (now, limit),
    ) as cursor:
        return list(await cursor.fetchall())


async def get_storage_stats() -> dict[str, Any]:
    conn = _conn()
    now = int(time.time())
    async with conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(file_size), 0) AS total_size FROM file_links WHERE is_deleted = 0 AND expires_at > ?",
        (now,),
    ) as cursor:
        active_row = await cursor.fetchone()

    async with conn.execute(
        "SELECT COUNT(*) AS c FROM file_links WHERE is_deleted = 1 OR expires_at <= ?",
        (now,),
    ) as cursor:
        expired_row = await cursor.fetchone()

    async with conn.execute(
        "SELECT COALESCE(SUM(download_count), 0) AS total_hits FROM file_links"
    ) as cursor:
        hits_row = await cursor.fetchone()

    active_count = int(active_row["c"]) if active_row else 0
    active_size = int(active_row["total_size"]) if active_row else 0
    expired_count = int(expired_row["c"]) if expired_row else 0
    total_hits = int(hits_row["total_hits"]) if hits_row else 0

    return {
        "active_count": active_count,
        "active_size_bytes": active_size,
        "expired_count": expired_count,
        "total_hits": total_hits,
    }


# ---------------------------------------------------------------------------
# Broadcasts
# ---------------------------------------------------------------------------
async def start_broadcast() -> int:
    conn = _conn()
    cursor = await conn.execute(
        "INSERT INTO broadcasts (sent_count, failed_count, started_at) VALUES (0, 0, ?)",
        (int(time.time()),),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def finish_broadcast(broadcast_id: int, sent: int, failed: int) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE broadcasts SET sent_count = ?, failed_count = ?, finished_at = ? WHERE id = ?",
        (sent, failed, int(time.time()), broadcast_id),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Key/Value Settings
# ---------------------------------------------------------------------------
async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _conn()
    async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    conn = _conn()
    await conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# File Expiration Setting (Runtime configurable)
# ---------------------------------------------------------------------------
async def get_file_expiration_seconds() -> int:
    value = await get_setting("file_expiration_seconds")
    if value is None:
        return config.DEFAULT_FILE_EXPIRATION_SECONDS
    try:
        val = int(value)
        return val if val >= 60 else config.DEFAULT_FILE_EXPIRATION_SECONDS
    except ValueError:
        return config.DEFAULT_FILE_EXPIRATION_SECONDS


async def set_file_expiration_seconds(seconds: int) -> None:
    seconds = max(60, int(seconds))
    await set_setting("file_expiration_seconds", str(seconds))


# ---------------------------------------------------------------------------
# Download Limits & Cooldown
# ---------------------------------------------------------------------------
async def get_max_file_size() -> int:
    value = await get_setting("max_file_size")
    if value is None:
        return config.MAX_FILE_SIZE
    try:
        return int(value)
    except ValueError:
        return config.MAX_FILE_SIZE


async def set_max_file_size(num_bytes: int) -> None:
    await set_setting("max_file_size", str(max(0, num_bytes)))


async def get_max_concurrent_downloads() -> int:
    value = await get_setting("max_concurrent_downloads")
    if value is None:
        return config.MAX_CONCURRENT_DOWNLOADS
    try:
        return max(1, int(value))
    except ValueError:
        return config.MAX_CONCURRENT_DOWNLOADS


async def set_max_concurrent_downloads(limit: int) -> None:
    await set_setting("max_concurrent_downloads", str(max(1, limit)))


async def get_rate_limit() -> tuple[int, int]:
    count = await get_setting("rate_limit_count")
    window = await get_setting("rate_limit_window")
    try:
        c = int(count) if count is not None else config.RATE_LIMIT_COUNT
    except ValueError:
        c = config.RATE_LIMIT_COUNT
    try:
        w = int(window) if window is not None else config.RATE_LIMIT_WINDOW
    except ValueError:
        w = config.RATE_LIMIT_WINDOW
    return c, w


async def set_rate_limit(count: int, window: int) -> None:
    await set_setting("rate_limit_count", str(max(1, count)))
    await set_setting("rate_limit_window", str(max(1, window)))


async def get_cooldown_settings() -> dict[str, int]:
    enabled = await get_setting("cooldown_enabled")
    count = await get_setting("cooldown_count")
    seconds = await get_setting("cooldown_seconds")

    def _int(v: Optional[str], default: int) -> int:
        try:
            return int(v) if v is not None else default
        except ValueError:
            return default

    is_enabled = (
        1
        if (enabled if enabled is not None else str(int(config.COOLDOWN_ENABLED)))
        in ("1", "true", "True")
        else 0
    )
    return {
        "enabled": is_enabled,
        "count": max(1, _int(count, config.COOLDOWN_COUNT)),
        "seconds": max(1, _int(seconds, config.COOLDOWN_SECONDS)),
    }


async def set_cooldown_settings(
    *,
    enabled: Optional[bool] = None,
    count: Optional[int] = None,
    seconds: Optional[int] = None,
) -> None:
    if enabled is not None:
        await set_setting("cooldown_enabled", "1" if enabled else "0")
    if count is not None:
        await set_setting("cooldown_count", str(max(1, count)))
    if seconds is not None:
        await set_setting("cooldown_seconds", str(max(1, seconds)))


async def get_report_interval_minutes() -> int:
    value = await get_setting("report_interval_minutes")
    if value is None:
        return config.DEFAULT_REPORT_INTERVAL_MINUTES
    try:
        return int(value)
    except ValueError:
        return config.DEFAULT_REPORT_INTERVAL_MINUTES


async def set_report_interval_minutes(minutes: int) -> None:
    await set_setting("report_interval_minutes", str(minutes))


# ---------------------------------------------------------------------------
# Message Overrides
# ---------------------------------------------------------------------------
_MESSAGE_OVERRIDE_PREFIX = "msg_override:"


async def get_message_override(key: str) -> Optional[str]:
    return await get_setting(_MESSAGE_OVERRIDE_PREFIX + key)


async def set_message_override(key: str, text: str) -> None:
    await set_setting(_MESSAGE_OVERRIDE_PREFIX + key, text)


async def reset_message_override(key: str) -> None:
    conn = _conn()
    await conn.execute(
        "DELETE FROM settings WHERE key = ?", (_MESSAGE_OVERRIDE_PREFIX + key,)
    )
    await conn.commit()


async def get_all_message_overrides() -> dict[str, str]:
    conn = _conn()
    async with conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?", (_MESSAGE_OVERRIDE_PREFIX + "%",)
    ) as cursor:
        rows = await cursor.fetchall()
    return {r["key"][len(_MESSAGE_OVERRIDE_PREFIX):]: r["value"] for r in rows}
