"""
cooldown.py
-----------
Per-user download cooldown: after N successful downloads, the user must
wait S seconds before starting another one (both N and S are configurable
at runtime from the Manager Bot, see database.get_cooldown_settings()).

Counters are kept in memory only (not persisted): they are meant to
throttle short bursts of usage, not to survive a redeploy, so this stays
extremely lightweight (a couple of small dicts) which fits Railway's Free
Plan RAM budget perfectly.
"""

from __future__ import annotations

import time
from typing import Optional

import database

# user_id -> number of successful downloads since the last cooldown reset
_counts: dict[int, int] = {}
# user_id -> unix timestamp when the current cooldown ends
_cooldown_until: dict[int, float] = {}


async def get_remaining_seconds(user_id: int) -> int:
    """Returns >0 while the user is still inside an active cooldown window."""
    settings = await database.get_cooldown_settings()
    if not settings["enabled"]:
        return 0
    until = _cooldown_until.get(user_id, 0.0)
    remaining = int(until - time.time())
    if remaining <= 0:
        _cooldown_until.pop(user_id, None)
        return 0
    return remaining


async def register_success(user_id: int) -> Optional[int]:
    """Call once per successful download. Returns the cooldown duration
    (seconds) if this download just triggered a new cooldown window,
    otherwise ``None``."""
    settings = await database.get_cooldown_settings()
    if not settings["enabled"]:
        return None

    _counts[user_id] = _counts.get(user_id, 0) + 1
    if _counts[user_id] >= settings["count"]:
        _counts[user_id] = 0
        _cooldown_until[user_id] = time.time() + settings["seconds"]
        return settings["seconds"]
    return None


def reset(user_id: int) -> None:
    _counts.pop(user_id, None)
    _cooldown_until.pop(user_id, None)
