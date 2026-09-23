"""
manager_bot.py
---------------
Dedicated admin management bot for Vid-to-Link.
Runs inside the same process / container on Railway:
- Configures file expiration duration at runtime without code changes or redeploy.
- Monitors Railway disk space and active stored files with one-click cleanup.
- Configures download limits (large file support, concurrency, cooldown).
- Live event logging and periodic aggregated reports.
- Message customization and user broadcasts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Optional

from telethon import Button, TelegramClient, errors, events

import admin_settings
import cleanup_worker
import config
import database
import messages
import utils
from event_logger import Event, EventType, shared as event_logger

logger = logging.getLogger(__name__)

_INTERVAL_OPTIONS = [
    (0, "Immediately"),
    (5, "5 minutes"),
    (10, "10 minutes"),
    (30, "30 minutes"),
    (60, "1 hour"),
]

_START_TIME = time.time()

# owner_id -> {"action": "broadcast_confirm", "text": str}
_pending_broadcast: dict[int, dict] = {}

# admin_id -> {"action": str, "key": Optional[str]}
_pending_input: dict[int, dict] = {}

_MAX_FILE_SIZE_PRESETS_MB = [500, 1000, 2000, 5000, 10000, 20000, 0]  # 0 = Unlimited
_MAX_CONCURRENT_PRESETS = [1, 2, 3, 4, 5]
_COOLDOWN_COUNT_PRESETS = [3, 5, 10, 15, 20]
_COOLDOWN_SECONDS_PRESETS = [30, 60, 120, 300, 600]


def _is_admin(user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    return user_id in config.MANAGER_ADMIN_IDS


def _format_event(event: Event) -> str:
    icons = {
        EventType.NEW_USER: "🆕",
        EventType.DOWNLOAD_REQUEST: "📥",
        EventType.DOWNLOAD_SUCCESS: "✅",
        EventType.DOWNLOAD_FAILED: "❌",
        EventType.ERROR: "⚠️",
    }
    icon = icons.get(event.type, "ℹ️")
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(event.timestamp))
    lines = [f"{icon} **{event.type.value}**"]
    if event.user_id:
        who = f"`{event.user_id}`"
        if event.username:
            who += f" (@{event.username})"
        lines.append(f"👤 User: {who}")
    if event.platform:
        lines.append(f"🌐 Platform: {event.platform}")
    if event.url:
        lines.append(f"🔗 URL: {event.url}")
    if event.quality:
        lines.append(f"🎚 Quality: {event.quality}")
    if event.file_size:
        lines.append(f"📦 Size: {utils.format_bytes(event.file_size)}")
    if event.error:
        lines.append(f"🧯 Error: {event.error}")
    lines.append(f"🕒 {ts}")
    return "\n".join(lines)


async def _consume_events(manager_client: TelegramClient) -> None:
    """Consume background events and forward immediately or aggregate."""
    counters: Counter[str] = Counter()
    window_start = time.time()

    async def flush_report() -> None:
        nonlocal counters, window_start
        if not config.MANAGER_CHAT_ID:
            counters.clear()
            window_start = time.time()
            return
        total = counters.get(EventType.DOWNLOAD_REQUEST.value, 0)
        success = counters.get(EventType.DOWNLOAD_SUCCESS.value, 0)
        failed = counters.get(EventType.DOWNLOAD_FAILED.value, 0)
        new_users = counters.get(EventType.NEW_USER.value, 0)
        if total or success or failed or new_users:
            interval = await database.get_report_interval_minutes()
            period = next(
                (label for m, label in _INTERVAL_OPTIONS if m == interval),
                f"{interval} minutes",
            )
            text = (
                "📊 **Vid-to-Link Activity Report**\n\n"
                f"⏱ Interval: {period}\n"
                f"👤 New users: {new_users}\n"
                f"📥 Total requests: {total}\n"
                f"✅ Successful downloads: {success}\n"
                f"❌ Failed: {failed}"
            )
            try:
                await manager_client.send_message(config.MANAGER_CHAT_ID, text)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to send periodic report")
        counters.clear()
        window_start = time.time()

    async def periodic_flusher() -> None:
        while True:
            interval = await database.get_report_interval_minutes()
            if interval <= 0:
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(min(interval * 60, 3600))
            if time.time() - window_start >= interval * 60 - 1:
                await flush_report()

    flusher_task = asyncio.create_task(periodic_flusher())
    try:
        async for event in event_logger.events():
            counters[event.type.value] += 1
            interval = await database.get_report_interval_minutes()
            if interval <= 0 and config.MANAGER_CHAT_ID:
                try:
                    await manager_client.send_message(
                        config.MANAGER_CHAT_ID, _format_event(event)
                    )
                except errors.FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to forward live event")
                counters.clear()
    finally:
        flusher_task.cancel()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def _cmd_start(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await event.respond(
        "🛠 **Vid-to-Link Manager Bot**\n\n"
        "Commands:\n"
        "/expiration — ⏳ Configure file retention & expiration duration\n"
        "/storage — 💾 View disk space & active files + run cleanup\n"
        "/limits — ⚙️ Download limits (max size, concurrency, cooldown)\n"
        "/stats — 📊 Bot statistics & total downloads\n"
        "/logs — 🧾 Most recent download events\n"
        "/interval — ⏱ Report interval frequency\n"
        "/messages — 💬 Customize user-facing messages\n"
        "/broadcast <text> — 📢 Send broadcast to all users\n"
        "/ping — 🏓 Uptime check"
    )


async def _cmd_ping(event) -> None:
    if not _is_admin(event.sender_id):
        return
    uptime = int(time.time() - _START_TIME)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    await event.respond(f"🏓 Pong!\n⏱ Uptime: {hours}h {minutes}m {seconds}s")


async def _cmd_stats(event) -> None:
    if not _is_admin(event.sender_id):
        return
    stats = await database.get_stats()
    storage = await database.get_storage_stats()
    await event.respond(
        "📊 **Bot Statistics**\n\n"
        f"👥 Total users: {stats['total_users']}\n"
        f"⬇️ Total requests: {stats['total_downloads']}\n"
        f"✅ Successful: {stats['successful_downloads']}\n"
        f"❌ Failed: {stats['failed_downloads']}\n\n"
        "💾 **Direct Link Stats:**\n"
        f"📂 Active links: {storage['active_count']} files ({utils.format_bytes(storage['active_size_bytes'])})\n"
        f"🗑 Expired / deleted: {storage['expired_count']} files\n"
        f"🌐 Total direct link hits: {storage['total_hits']}"
    )


async def _cmd_logs(event) -> None:
    if not _is_admin(event.sender_id):
        return
    rows = await database.get_recent_downloads(15)
    if not rows:
        await event.respond("No download events yet.")
        return
    lines = ["🧾 **Recent Downloads**\n"]
    for row in rows:
        ts = time.strftime("%m-%d %H:%M", time.localtime(row["created_at"]))
        icon = {"success": "✅", "failed": "❌", "requested": "📥", "cancelled": "🛑"}.get(
            row["status"], "ℹ️"
        )
        lines.append(
            f"{icon} [{ts}] user `{row['user_id']}` — {row['platform'] or 'unknown'} ({row['quality'] or 'direct'})"
        )
    await event.respond("\n".join(lines))


# ---------------------------------------------------------------------------
# /expiration — File retention & expiration configuration
# ---------------------------------------------------------------------------
async def _cmd_expiration(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_expiration_menu(event)


async def _send_expiration_menu(event_or_msg) -> None:
    current_sec = await admin_settings.get_file_expiration_seconds()
    human_en = utils.format_time_remaining_en(current_sec)
    human_fa = utils.format_time_remaining(current_sec)

    text = (
        "⏳ **File Expiration & Retention Settings**\n\n"
        f"Current validity duration: **{human_en}** ({human_fa} / {current_sec:,} seconds)\n\n"
        "When a user downloads a video, the direct link will remain valid for this amount of time. "
        "Once expired, the file is automatically purged from Railway disk storage.\n\n"
        "Select a quick preset or choose Custom Duration:"
    )

    buttons = [
        [
            Button.inline(f"{'✅ ' if current_sec == 900 else ''}15 min", "exp|set|900"),
            Button.inline(f"{'✅ ' if current_sec == 1800 else ''}30 min", "exp|set|1800"),
            Button.inline(f"{'✅ ' if current_sec == 3600 else ''}1 hour", "exp|set|3600"),
        ],
        [
            Button.inline(f"{'✅ ' if current_sec == 7200 else ''}2 hours", "exp|set|7200"),
            Button.inline(f"{'✅ ' if current_sec == 14400 else ''}4 hours", "exp|set|14400"),
            Button.inline(f"{'✅ ' if current_sec == 21600 else ''}6 hours", "exp|set|21600"),
        ],
        [
            Button.inline(f"{'✅ ' if current_sec == 43200 else ''}12 hours", "exp|set|43200"),
            Button.inline(f"{'✅ ' if current_sec == 86400 else ''}24 hours", "exp|set|86400"),
            Button.inline(f"{'✅ ' if current_sec == 172800 else ''}48 hours", "exp|set|172800"),
        ],
        [Button.inline("✏️ Custom Duration", "exp|custom")],
    ]

    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _handle_expiration_callback(event: events.CallbackQuery.Event, action: str, val: str) -> None:
    if action == "set":
        try:
            sec = int(val)
            await admin_settings.set_file_expiration_seconds(sec)
            human = utils.format_time_remaining_en(sec)
            await event.answer(f"File expiration set to {human}!")
            await _send_expiration_menu(event)
        except ValueError:
            await event.answer("Invalid value.")
    elif action == "custom":
        _pending_input[event.sender_id] = {"action": "set_expiration"}
        await event.answer()
        await event.respond(
            "✏️ **Custom Expiration Duration**\n\n"
            "Please send the desired duration:\n"
            "Examples: `45m` (45 min), `3h` (3 hours), `1d` (1 day), or seconds like `5400`."
        )


# ---------------------------------------------------------------------------
# /storage — Railway disk monitor & manual cleanup
# ---------------------------------------------------------------------------
async def _cmd_storage(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_storage_menu(event)


async def _send_storage_menu(event_or_msg) -> None:
    disk = cleanup_worker.get_disk_usage_info()
    storage = await database.get_storage_stats()

    text = (
        "💾 **Storage & Disk Status**\n\n"
        f"📁 Download Path: `{config.DOWNLOAD_PATH}`\n\n"
        "💽 **Railway Container Disk:**\n"
        f"• Total: {disk['total_formatted']}\n"
        f"• Used: {disk['used_formatted']} ({disk['percent']:.1f}%)\n"
        f"• Free: {disk['free_formatted']}\n\n"
        "📦 **Stored Files:**\n"
        f"• Active files: {storage['active_count']} ({utils.format_bytes(storage['active_size_bytes'])})\n"
        f"• Expired/deleted records: {storage['expired_count']}\n"
        f"• Total direct link downloads: {storage['total_hits']} hits"
    )

    buttons = [
        [Button.inline("🧹 Run Cleanup Now", "stor|cleanup")],
        [Button.inline("🔄 Refresh Status", "stor|refresh")],
    ]

    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _handle_storage_callback(event: events.CallbackQuery.Event, action: str) -> None:
    if action == "cleanup":
        await event.answer("Running cleanup sweep...")
        stats = await cleanup_worker.run_cleanup_cycle()
        freed_str = utils.format_bytes(stats["freed_bytes"])
        await event.answer(
            f"Cleanup complete! Removed {stats['deleted_count']} file(s), freed {freed_str}.",
            alert=True,
        )
        await _send_storage_menu(event)
    elif action == "refresh":
        await event.answer("Refreshed!")
        await _send_storage_menu(event)


# ---------------------------------------------------------------------------
# /limits — Download limits (size, concurrency, cooldown)
# ---------------------------------------------------------------------------
async def _cmd_limits(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_limits_menu(event)


async def _send_limits_menu(event_or_msg) -> None:
    max_size = await admin_settings.get_max_file_size()
    max_concurrent = await admin_settings.get_max_concurrent_downloads()
    cooldown_cfg = await admin_settings.get_cooldown_config()

    size_display = "Unlimited (0)" if max_size == 0 else utils.format_bytes(max_size)

    text = (
        "⚙️ **Download Limits Configuration**\n\n"
        f"📦 Max file size: {size_display}\n"
        f"🔀 Max simultaneous downloads: {max_concurrent}\n"
        f"🧊 Cooldown enabled: {'yes' if cooldown_cfg['enabled'] else 'no'}\n"
        f"🔢 Downloads before cooldown: {cooldown_cfg['count']}\n"
        f"⏳ Cooldown duration: {cooldown_cfg['seconds']} sec\n\n"
        "Choose an option to modify:"
    )

    buttons = [
        [Button.inline("📦 Max file size", "lim|size")],
        [Button.inline("🔀 Max concurrent downloads", "lim|conc")],
        [
            Button.inline(
                f"🧊 Cooldown: {'disable' if cooldown_cfg['enabled'] else 'enable'}",
                "lim|toggle_cd",
            )
        ],
        [Button.inline("🔢 Downloads before cooldown", "lim|cdcount")],
        [Button.inline("⏳ Cooldown duration", "lim|cdsec")],
    ]

    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _limits_callback(event: events.CallbackQuery.Event, action: str) -> None:
    if action == "size":
        buttons = []
        for mb in _MAX_FILE_SIZE_PRESETS_MB:
            label = "Unlimited" if mb == 0 else f"{mb:,} MB"
            buttons.append([Button.inline(label, f"limset|size|{mb}")])
        await event.edit(
            "📦 Choose the maximum allowed file size:\n(Files > 2GB are fully supported)",
            buttons=buttons,
        )
    elif action == "conc":
        buttons = [[Button.inline(str(n), f"limset|conc|{n}")] for n in _MAX_CONCURRENT_PRESETS]
        await event.edit(
            "🔀 Choose maximum simultaneous downloads:\n(Railway free tier recommended: 1 - 2)",
            buttons=buttons,
        )
    elif action == "toggle_cd":
        cfg = await admin_settings.get_cooldown_config()
        new_cfg = await admin_settings.set_cooldown_config(enabled=not cfg["enabled"])
        await event.answer(f"Cooldown {'enabled' if new_cfg['enabled'] else 'disabled'}.")
        await _send_limits_menu(event)
    elif action == "cdcount":
        buttons = [[Button.inline(str(n), f"limset|cdcount|{n}")] for n in _COOLDOWN_COUNT_PRESETS]
        await event.edit("🔢 Downloads allowed before cooldown activates:", buttons=buttons)
    elif action == "cdsec":
        buttons = [[Button.inline(f"{n}s", f"limset|cdsec|{n}")] for n in _COOLDOWN_SECONDS_PRESETS]
        await event.edit("⏳ Cooldown wait duration:", buttons=buttons)
    else:
        await event.answer()


async def _limits_set_callback(event: events.CallbackQuery.Event, kind: str, value: str) -> None:
    if kind == "size":
        val_mb = int(value)
        bytes_val = 0 if val_mb == 0 else val_mb * 1024 * 1024
        await admin_settings.set_max_file_size(bytes_val)
        await event.answer(f"Max size set to {'Unlimited' if val_mb == 0 else f'{val_mb} MB'}.")
    elif kind == "conc":
        await admin_settings.set_max_concurrent_downloads(int(value))
        await event.answer(f"Max concurrent set to {value}.")
    elif kind == "cdcount":
        await admin_settings.set_cooldown_config(count=int(value))
        await event.answer(f"Cooldown activates after {value} downloads.")
    elif kind == "cdsec":
        await admin_settings.set_cooldown_config(seconds=int(value))
        await event.answer(f"Cooldown duration set to {value} seconds.")
    else:
        await event.answer()
        return
    await _send_limits_menu(event)


# ---------------------------------------------------------------------------
# /messages — Customizable messages
# ---------------------------------------------------------------------------
_KEY_LABELS = {
    "welcome": "Welcome message",
    "help": "Help message",
    "download_ready": "Download ready message",
    "pornhub_link_received": "PornHub: link received",
    "pornhub_warning": "PornHub: warning",
    "pornhub_before_download": "PornHub: before download",
    "pornhub_download_started": "PornHub: download started",
    "pornhub_error": "PornHub: error message",
    "pornhub_admin_notify": "PornHub: admin notification",
    "error_generic": "Generic error message",
    "error_download_failed": "Download failed message",
    "cooldown_active": "Cooldown active message",
    "cooldown_finished": "Cooldown finished message",
}


async def _cmd_messages(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_messages_menu(event)


async def _send_messages_menu(event_or_msg) -> None:
    keys = admin_settings.customizable_keys()
    buttons = []
    for key in keys:
        label = _KEY_LABELS.get(key, key)
        buttons.append([Button.inline(label, f"msg|{key}")])

    text = "💬 **Customizable Messages**\nSelect a message template to view or customize:"
    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _cmd_interval(event) -> None:
    if not _is_admin(event.sender_id):
        return
    current = await database.get_report_interval_minutes()
    buttons = [
        [Button.inline(f"{'✅ ' if m == current else ''}{label}", f"ivl|{m}")]
        for m, label in _INTERVAL_OPTIONS
    ]
    await event.respond("⏱ Choose reporting interval:", buttons=buttons)


async def _cmd_broadcast(event, main_client: TelegramClient) -> None:
    if not config.OWNER_ID or event.sender_id != config.OWNER_ID:
        await event.respond("Only the owner can broadcast.")
        return
    text = (event.raw_text or "").partition(" ")[2].strip()
    if not text:
        await event.respond("Usage: `/broadcast <message text>`")
        return
    total_users = await database.get_user_count()
    _pending_broadcast[event.sender_id] = {"action": "broadcast_confirm", "text": text}
    buttons = [
        [
            Button.inline("✅ Yes, Send", "bc|confirm"),
            Button.inline("❌ Cancel", "bc|cancel"),
        ]
    ]
    await event.respond(
        f"📢 **Broadcast Confirmation**\n\n"
        f"Target: {total_users} registered users\n\n"
        f"**Preview:**\n{text}",
        buttons=buttons,
    )


# ---------------------------------------------------------------------------
# Dispatcher & Callback Registrations
# ---------------------------------------------------------------------------
def register_manager_handlers(manager_client: TelegramClient, main_client: TelegramClient) -> None:
    """Register all Manager Bot event handlers."""

    @manager_client.on(events.NewMessage())
    async def _on_message(event: events.NewMessage.Event) -> None:
        if not _is_admin(event.sender_id):
            return

        # Check pending state input
        pending = _pending_input.get(event.sender_id)
        if pending:
            action = pending.get("action")
            if action == "set_expiration":
                _pending_input.pop(event.sender_id, None)
                parsed_sec = utils.parse_time_duration(event.raw_text or "")
                if parsed_sec is None or parsed_sec < 60:
                    await event.respond(
                        "❌ Invalid duration. Please provide a valid value (e.g. `90m`, `3h`, `1d`, or seconds like `5400`)."
                    )
                    return
                await admin_settings.set_file_expiration_seconds(parsed_sec)
                human = utils.format_time_remaining_en(parsed_sec)
                await event.respond(
                    f"✅ File expiration updated to **{human}** ({parsed_sec:,} seconds)!\n"
                    "All future downloads will use this retention period."
                )
                return
            elif action == "set_message":
                key = pending.get("key")
                _pending_input.pop(event.sender_id, None)
                if key:
                    await admin_settings.set_message_override(key, event.raw_text or "")
                    await event.respond(f"✅ Message `{key}` has been updated!")
                    return

        text = (event.raw_text or "").strip()
        cmd = text.split()[0].lower() if text else ""

        if cmd == "/start":
            await _cmd_start(event)
        elif cmd == "/ping":
            await _cmd_ping(event)
        elif cmd == "/stats":
            await _cmd_stats(event)
        elif cmd == "/logs":
            await _cmd_logs(event)
        elif cmd == "/expiration":
            await _cmd_expiration(event)
        elif cmd == "/storage":
            await _cmd_storage(event)
        elif cmd == "/limits":
            await _cmd_limits(event)
        elif cmd == "/messages":
            await _cmd_messages(event)
        elif cmd == "/interval":
            await _cmd_interval(event)
        elif cmd.startswith("/broadcast"):
            await _cmd_broadcast(event, main_client)

    @manager_client.on(events.CallbackQuery())
    async def _on_callback(event: events.CallbackQuery.Event) -> None:
        if not _is_admin(event.sender_id):
            await event.answer("Unauthorized.", alert=True)
            return

        data = (event.data or b"").decode("utf-8", errors="ignore")
        parts = data.split("|")
        prefix = parts[0]

        if prefix == "exp":
            if len(parts) >= 3 and parts[1] == "set":
                await _handle_expiration_callback(event, "set", parts[2])
            elif len(parts) >= 2 and parts[1] == "custom":
                await _handle_expiration_callback(event, "custom", "")
        elif prefix == "stor":
            action = parts[1] if len(parts) > 1 else "refresh"
            await _handle_storage_callback(event, action)
        elif prefix == "lim":
            action = parts[1] if len(parts) > 1 else ""
            await _limits_callback(event, action)
        elif prefix == "limset":
            if len(parts) >= 3:
                await _limits_set_callback(event, parts[1], parts[2])
        elif prefix == "ivl":
            if len(parts) >= 2:
                try:
                    val = int(parts[1])
                    await database.set_report_interval_minutes(val)
                    await event.answer(f"Report interval set to {val} min.")
                    await _cmd_interval(event)
                except ValueError:
                    await event.answer()
        elif prefix == "msg":
            if len(parts) >= 2:
                key = parts[1]
                cur = admin_settings.get_current_text(key)
                _pending_input[event.sender_id] = {"action": "set_message", "key": key}
                buttons = [[Button.inline("🔄 Reset to Default", f"msgreset|{key}")]]
                await event.edit(
                    f"💬 **Customize:** `{key}`\n\n"
                    f"**Current Text:**\n{cur}\n\n"
                    "Reply with your new text to update, or click Reset.",
                    buttons=buttons,
                )
        elif prefix == "msgreset":
            if len(parts) >= 2:
                key = parts[1]
                await admin_settings.reset_message_override(key)
                await event.answer("Reset to default!")
                await _send_messages_menu(event)
        elif prefix == "bc":
            action = parts[1] if len(parts) > 1 else ""
            if action == "confirm":
                info = _pending_broadcast.pop(event.sender_id, None)
                if not info:
                    await event.answer("Expired.")
                    return
                await event.answer("Starting broadcast...")
                # Run broadcast in background
                asyncio.create_task(_run_broadcast(main_client, event.chat_id, info["text"]))
            elif action == "cancel":
                _pending_broadcast.pop(event.sender_id, None)
                await event.edit("Broadcast cancelled.")

    asyncio.create_task(_consume_events(manager_client))


async def _run_broadcast(client: TelegramClient, admin_chat_id: int, text: str) -> None:
    """Execute broadcast to all registered users."""
    user_ids = await database.get_all_user_ids()
    sent = 0
    failed = 0
    broadcast_id = await database.start_broadcast()

    for uid in user_ids:
        try:
            await client.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except errors.FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except Exception:
            failed += 1

    await database.finish_broadcast(broadcast_id, sent, failed)
    try:
        await client.send_message(
            admin_chat_id,
            f"📢 **Broadcast Finished**\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
        )
    except Exception:
        pass
