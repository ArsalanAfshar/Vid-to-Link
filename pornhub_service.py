"""
pornhub_service.py
--------------------
PornHub-specific logic, fully isolated from the generic download path.

All user-facing text is defined in messages.py, keyed with a
'pornhub_' prefix so the same pattern can be reused for future sites.





Admin notification: after every PornHub download (success or failure),
OWNER_ID receives a message with the user id/username, video title,
status and a precise timestamp.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient

import config

logger = logging.getLogger(__name__)


@dataclass
class PornhubNotifyPayload:
    user_id: int
    username: Optional[str]
    title: str
    status: str  # "success" | "failed"
    detail: str = ""


def _format_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def notify_admin(client: TelegramClient, payload: PornhubNotifyPayload) -> None:
    """Send an admin notification, only if PORNHUB_NOTIFY_ADMIN is enabled."""
    if not config.PORNHUB_NOTIFY_ADMIN or not config.OWNER_ID:
        return

    username_display = f"@{payload.username}" if payload.username else "none"
    status_display = "✅ success" if payload.status == "success" else "❌ failed"

    text = (
        "🔔 PornHub download notification\n\n"
        f"👤 User ID: `{payload.user_id}`\n"
        f"🔗 Username: {username_display}\n"
        f"🎬 Video title: {payload.title}\n"
        f"📊 Status: {status_display}\n"
        f"🕒 Time: {_format_timestamp()}\n"
    )
    if payload.detail:
        text += f"\nℹ️ Details: {payload.detail}"

    try:
        await client.send_message(config.OWNER_ID, text, parse_mode="markdown", link_preview=False)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send the PornHub admin notification.")
