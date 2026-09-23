"""
event_logger.py
-----------------
Lightweight, low-overhead event bus used to feed the NexiLink Manager Bot
with everything happening in the main bot: new users, download requests,
successes, failures and generic errors.

Design goals (Railway Free Plan friendly):
 - A single `asyncio.Queue` in the same process — no extra service, no
   extra network hop, negligible memory footprint.
 - Persistence (SQLite) happens immediately regardless of the configured
   report interval, so `/stats` in the Manager Bot is always accurate.
 - The queue is only used for *notifications* (immediate forwarding or
   periodic aggregation); if nothing is consuming it yet (Manager Bot
   disabled/not started), events are simply persisted and the queue is
   capped so it can never grow unbounded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional

import database

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    NEW_USER = "new_user"
    DOWNLOAD_REQUEST = "download_request"
    DOWNLOAD_SUCCESS = "download_success"
    DOWNLOAD_FAILED = "download_failed"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    user_id: int = 0
    username: Optional[str] = None
    platform: Optional[str] = None
    url: Optional[str] = None
    quality: Optional[str] = None
    file_size: Optional[int] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


_MAX_QUEUE_SIZE = 2000


class EventLogger:
    """Process-wide event bus. Create one instance and share it between
    `handlers.py` (producer) and `manager_bot.py` (consumer)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)

    def log(self, event: Event) -> None:
        """Fire-and-forget: persist to SQLite and publish to the queue.
        Never raises and never blocks the caller."""
        asyncio.create_task(self._persist(event))
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue is full; dropping oldest event to stay memory-safe.")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    async def _persist(self, event: Event) -> None:
        try:
            if event.type in (
                EventType.DOWNLOAD_REQUEST,
                EventType.DOWNLOAD_SUCCESS,
                EventType.DOWNLOAD_FAILED,
            ):
                status = {
                    EventType.DOWNLOAD_REQUEST: "requested",
                    EventType.DOWNLOAD_SUCCESS: "success",
                    EventType.DOWNLOAD_FAILED: "failed",
                }[event.type]
                await database.add_download(
                    user_id=event.user_id,
                    url=event.url or "",
                    platform=event.platform,
                    quality=event.quality,
                    file_size=event.file_size,
                    status=status,
                    error=event.error,
                    username=event.username,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist event %s", event.type)

    async def events(self) -> AsyncIterator[Event]:
        """Async generator consumed by the Manager Bot's background task."""
        while True:
            event = await self._queue.get()
            yield event


# A single shared instance used across the whole process.
shared = EventLogger()
