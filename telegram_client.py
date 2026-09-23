"""
telegram_client.py
-------------------
Builds and configures Telethon (MTProto) clients used by both the main
downloader bot and the NexiLink Manager Bot.

Why Telethon instead of the plain HTTP Bot API?
    HTTP Bot API libraries have low, fixed upload/download limits and are
    less reliable for large media files. Telethon talks MTProto directly,
    which gives us real upload/download speed, accurate progress
    percentages and (optionally, via SESSION_STRING) higher upload limits.
"""

from __future__ import annotations

import logging
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

import config

logger = logging.getLogger(__name__)

try:
    import cryptg  # noqa: F401

    logger.info("cryptg is available: MTProto crypto is hardware accelerated.")
except ImportError:  # pragma: no cover
    logger.warning("cryptg is not installed; falling back to pure-Python crypto (slower).")


async def create_client(
    bot_token: Optional[str] = None,
    session_string: Optional[str] = None,
    device_model: Optional[str] = None,
) -> TelegramClient:
    """Build and connect a Telethon client.

    If `session_string` is provided, logs in with that user session
    (allows bigger upload limits on Premium accounts). Otherwise logs in
    as a bot using `bot_token`.
    """
    session_string = session_string if session_string is not None else config.SESSION_STRING
    bot_token = bot_token if bot_token is not None else config.BOT_TOKEN

    session = StringSession(session_string) if session_string else StringSession()

    client = TelegramClient(
        session,
        config.API_ID,
        config.API_HASH,
        device_model=device_model or config.DEVICE_MODEL,
        app_version=config.APP_VERSION,
        connection_retries=10,
        retry_delay=2,
        auto_reconnect=True,
        flood_sleep_threshold=60,
    )

    if session_string:
        await client.start()
        logger.info("Logged in using SESSION_STRING (user account).")
    else:
        await client.start(bot_token=bot_token)
        logger.info("Logged in using BOT_TOKEN (bot account).")

    return client
