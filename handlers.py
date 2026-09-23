"""
handlers.py
-----------
All conversational logic for the Vid-to-Link bot:
- URL intake and platform routing (YouTube, Instagram, TikTok, Twitter/X, Pinterest, SoundCloud, PornHub, and 1800+ sites).
- Quality selection for videos.
- Download pipeline with live progress reporting.
- Direct HTTP download link delivery (never sending media via Telegram).
- Admin management commands (/admin, /expiration, /storage, /limits, /stats) for the owner.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from telethon import Button, TelegramClient, errors, events
from telethon.tl.custom import Message

import admin_settings
import cleanup_worker
import concurrency
import config
import cooldown
import database
import downloader
import instagram_service
import messages
import pinterest_service
import platforms_registry
import pornhub_service
import soundcloud_service
import twitter_service
import utils
from event_logger import Event, EventType, shared as event_logger

logger = logging.getLogger(__name__)

rate_limiter = utils.RateLimiter()
download_semaphore = concurrency.download_semaphore

# task_id -> {"user_id", "info", "url", "message"}
_pending_selection: dict[str, dict[str, Any]] = {}

# user_id -> state dict for admin custom inputs
_admin_pending_input: dict[int, dict[str, Any]] = {}


@dataclass
class ActiveTask:
    task_id: str
    url: str
    stage: str
    status_message: Message
    temp_dir: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)


_active_tasks: dict[int, ActiveTask] = {}


def is_owner(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.MANAGER_ADMIN_IDS


def _is_busy(user_id: int) -> bool:
    task = _active_tasks.get(user_id)
    if task is not None:
        return True
    return any(v.get("user_id") == user_id for v in _pending_selection.values())


def _cleanup_task(user_id: int, keep_temp: bool = False) -> None:
    """Clear active task from memory. If keep_temp=False, also wipe disk directory."""
    task = _active_tasks.pop(user_id, None)
    if task is not None and not keep_temp:
        utils.safe_delete(task.temp_dir)


def _log(event_type: EventType, user_id: int, username: Optional[str], **kwargs: Any) -> None:
    event_logger.log(
        Event(
            type=event_type,
            user_id=user_id,
            username=username,
            timestamp=time.time(),
            **kwargs,
        )
    )


async def send(event: Message, key: str, buttons: Any = None, **kwargs: Any) -> Message:
    text = messages.get(key, **kwargs)
    return await event.respond(text, buttons=buttons, link_preview=False)


async def safe_edit(msg: Message, key: str, buttons: Any = None, **kwargs: Any) -> None:
    text = messages.get(key, **kwargs)
    try:
        await msg.edit(text, buttons=buttons, link_preview=False)
    except errors.MessageNotModifiedError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to edit message: %s", exc)


async def _note_download_success(
    user_id: int,
    event_type: EventType,
    username: Optional[str] = None,
    platform: Optional[str] = None,
    url: Optional[str] = None,
    quality: Optional[str] = None,
    file_size: Optional[int] = None,
) -> None:
    _log(event_type, user_id, username, platform=platform, url=url, quality=quality, file_size=file_size)

    # Check cooldown
    cfg = await database.get_cooldown_settings()
    if cfg["enabled"]:
        count, active, remaining = cooldown.note_download(user_id, cfg["count"], cfg["seconds"])
        if active:
            task = _active_tasks.get(user_id)
            if task and task.status_message:
                asyncio.create_task(_run_cooldown_countdown(task.status_message, remaining))


async def _run_cooldown_countdown(msg: Message, seconds: int) -> None:
    for remaining in range(seconds, 0, -5):
        try:
            await msg.respond(messages.get("cooldown_active", seconds=remaining))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(min(5, remaining))
    try:
        await msg.respond(messages.get("cooldown_finished"))
    except Exception:  # noqa: BLE001
        pass


def main_menu_keyboard() -> list[list[Button]]:
    return [
        [Button.text(messages.get("menu_download"), resize=True), Button.text(messages.get("menu_platforms"), resize=True)],
        [Button.text(messages.get("menu_help"), resize=True), Button.text(messages.get("menu_settings"), resize=True)],
        [Button.text(messages.get("menu_about"), resize=True)],
    ]


# ---------------------------------------------------------------------------
# Direct Link Delivery Engine
# ---------------------------------------------------------------------------
async def _deliver_direct_link(
    chat_id: int,
    status_msg: Message,
    filepath: str,
    title: str,
    platform: str,
    url: str,
    quality_label: str,
    user_id: int,
    username: Optional[str],
    on_success: Optional[Callable] = None,
) -> None:
    """Register file in SQLite and present the direct HTTP download link."""
    file_p = Path(filepath)
    if not file_p.exists() or not file_p.is_file():
        await safe_edit(status_msg, "error_generic", error="فایل دانلود شده روی سرور یافت نشد.")
        return

    file_size = file_p.stat().st_size
    max_size = await admin_settings.get_max_file_size()
    if max_size > 0 and file_size > max_size:
        await safe_edit(
            status_msg,
            "error_file_too_large",
            size=utils.format_bytes(file_size),
            max_size=utils.format_bytes(max_size),
        )
        _log(
            EventType.DOWNLOAD_FAILED, user_id, username, platform=platform, url=url,
            quality=quality_label, file_size=file_size, error="file too large",
        )
        return

    # Generate secure download token
    token = secrets.token_urlsafe(16)
    exp_seconds = await admin_settings.get_file_expiration_seconds()
    now = int(time.time())
    expires_at = now + exp_seconds

    filename = file_p.name
    await database.create_file_link(
        token=token,
        user_id=user_id,
        file_path=str(file_p.resolve()),
        file_name=filename,
        file_size=file_size,
        expires_at=expires_at,
        mime_type=utils.guess_mime(filepath),
    )

    base_url = config.get_base_url()
    download_url = f"{base_url}/download/{token}/{quote(filename)}"

    expires_in = utils.format_time_remaining(exp_seconds)
    expires_at_str = utils.format_timestamp_utc(expires_at)

    buttons = [
        [Button.url("🌐 دانلود مستقیم در مرورگر", download_url)],
        [Button.inline("📋 مشخصات فایل دانلودی", f"linkinfo|{token}")],
    ]

    extra_notice = ""
    if is_owner(user_id) and ("localhost" in base_url or "127.0.0.1" in base_url):
        extra_notice = (
            "\n\n⚠️ **نکته مدیر:** دامنه عمومی ریل‌وی هنوز تنظیم نشده است. در پنل Railway از مسیر "
            "Settings -> Networking روی **Generate Domain** بزنید تا لینک اینترنتی عمومی شود."
        )

    await safe_edit(
        status_msg,
        "download_ready",
        title=title,
        quality=quality_label,
        size=utils.format_bytes(file_size),
        expires_in=expires_in,
        expires_at=expires_at_str,
        download_url=download_url + extra_notice,
        buttons=buttons,
    )

    await _note_download_success(
        user_id, EventType.DOWNLOAD_SUCCESS, username=username, platform=platform,
        url=url, quality=quality_label, file_size=file_size,
    )

    if on_success:
        try:
            await on_success(filepath)
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_success callback error: %s", exc)


# ---------------------------------------------------------------------------
# General Bot Commands
# ---------------------------------------------------------------------------
async def cmd_start(event: Message) -> None:
    sender = await event.get_sender()
    name = getattr(sender, "first_name", None) or "کاربر گرامی"
    await send(event, "welcome", name=name, buttons=main_menu_keyboard())


async def cmd_help(event: Message) -> None:
    await send(event, "help", buttons=main_menu_keyboard())


async def cmd_platforms(event: Message) -> None:
    text = platforms_registry.build_platforms_text()
    await event.client.send_message(event.chat_id, text, link_preview=False, buttons=main_menu_keyboard())


async def cmd_settings(event: Message) -> None:
    max_size = await database.get_max_file_size()
    max_size_str = "نامحدود" if max_size == 0 else utils.format_bytes(max_size)
    exp_sec = await database.get_file_expiration_seconds()
    exp_str = utils.format_time_remaining(exp_sec)
    rate_count, rate_window = await database.get_rate_limit()

    await send(
        event,
        "settings_text",
        max_size=max_size_str,
        expiration=exp_str,
        rate_count=rate_count,
        rate_window=rate_window,
        max_concurrent=await admin_settings.get_max_concurrent_downloads(),
    )


async def cmd_about(event: Message) -> None:
    await send(event, "about_text", version=config.APP_VERSION)


async def cmd_menu_download_prompt(event: Message) -> None:
    await send(event, "menu_download_prompt")


async def cmd_cancel(event: Message, user_id: int) -> None:
    task = _active_tasks.get(user_id)
    pending_keys = [tid for tid, v in _pending_selection.items() if v["user_id"] == user_id]
    for tid in pending_keys:
        entry = _pending_selection.pop(tid, None)
        if entry:
            await safe_edit(entry.get("message"), "cancel_success")

    if task is None and not pending_keys:
        await send(event, "cancel_nothing")
        return

    if task is not None:
        task.cancel_event.set()
        _cleanup_task(user_id, keep_temp=False)

    await send(event, "cancel_success")


async def cmd_status(event: Message, user_id: int) -> None:
    task = _active_tasks.get(user_id)
    if task is None:
        await send(event, "status_idle")
        return
    await send(event, "status_active", url=task.url, stage=task.stage)


# ---------------------------------------------------------------------------
# Link Intake & Routing
# ---------------------------------------------------------------------------
async def handle_url_message(event: Message, user_id: int, username: Optional[str], text: str) -> None:
    if await database.is_user_banned(user_id):
        await send(event, "banned_user")
        return

    url = utils.extract_first_url(text)
    if not url:
        await send(event, "invalid_url")
        return

    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    max_count, window = await database.get_rate_limit()
    allowed, retry_after = rate_limiter.check(user_id, max_count, window)
    if not allowed:
        await send(event, "error_rate_limit", seconds=int(retry_after) + 1)
        return

    handler_key = platforms_registry.detect_platform(url)
    platform_name = platforms_registry.find_platform_name(url) or handler_key

    _log(EventType.DOWNLOAD_REQUEST, user_id, username, platform=platform_name, url=url)

    if handler_key == "pornhub":
        await _handle_pornhub_url(event, user_id, username, url)
    elif handler_key == "pinterest":
        await _handle_pinterest_url(event, user_id, username, url)
    elif handler_key == "instagram":
        await _handle_instagram_url(event, user_id, username, url)
    elif handler_key == "soundcloud":
        await _handle_soundcloud_url(event, user_id, username, url)
    elif handler_key == "twitter":
        await _handle_twitter_url(event, user_id, username, url)
    else:
        await _handle_generic_url(event, user_id, username, url, platform_name)


# ---------------------------------------------------------------------------
# Generic Path (yt-dlp): YouTube, TikTok, Reddit, Vimeo, Facebook, etc.
# ---------------------------------------------------------------------------
async def _handle_generic_url(event: Message, user_id: int, username: Optional[str], url: str, platform_hint: str) -> None:
    await send(event, "url_received")
    status_msg = await send(event, "extracting_info")

    try:
        info = await downloader.extract_video_info(url)
    except downloader.ExtractionError as exc:
        await safe_edit(status_msg, "error_extraction_failed", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=platform_hint, url=url, error=str(exc))
        return

    if not info.qualities:
        await safe_edit(status_msg, "no_formats_found")
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=platform_hint, url=url, error="no formats")
        return

    task_id = uuid.uuid4().hex[:10]
    _pending_selection[task_id] = {
        "user_id": user_id,
        "username": username,
        "info": info,
        "url": url,
        "message": status_msg,
    }

    buttons = []
    row = []
    for q in info.qualities:
        size_label = f" ({utils.format_bytes(q.filesize)})" if q.filesize else ""
        text = f"{q.label}{size_label}"
        row.append(Button.inline(text, f"q|{task_id}|{q.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("❌ لغو", f"cancelq|{task_id}")])

    await safe_edit(
        status_msg,
        "quality_selection",
        buttons=buttons,
        title=info.title,
        duration=utils.format_duration(info.duration),
    )


async def handle_quality_selected(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    if len(parts) < 3:
        await event.answer()
        return

    _, task_id, quality_id = parts
    entry = _pending_selection.pop(task_id, None)
    if not entry or entry["user_id"] != event.sender_id:
        await event.answer("درخواست منقضی شده یا نامعتبر است.", alert=True)
        return

    await event.answer()
    info: downloader.VideoInfo = entry["info"]
    quality = next((q for q in info.qualities if q.id == quality_id), None)
    if not quality:
        await safe_edit(entry["message"], "error_generic", error="کیفیت انتخاب‌شده یافت نشد.")
        return

    await _run_download_and_deliver(
        event.chat_id,
        entry["user_id"],
        entry["username"],
        info,
        entry["url"],
        quality,
        entry["message"],
        task_id,
    )


async def handle_cancel_quality(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    if len(parts) < 2:
        await event.answer()
        return
    task_id = parts[1]
    entry = _pending_selection.pop(task_id, None)
    if entry and entry["user_id"] == event.sender_id:
        await safe_edit(entry["message"], "cancel_success")
    await event.answer()


async def _run_download_and_deliver(
    chat_id: int,
    user_id: int,
    username: Optional[str],
    info: downloader.VideoInfo,
    url: str,
    quality: downloader.QualityOption,
    msg: Message,
    task_id: str,
    on_success: Optional[Callable] = None,
    on_failure: Optional[Callable] = None,
) -> None:
    async with download_semaphore:
        temp_dir = Path(config.DOWNLOAD_PATH) / task_id
        task = ActiveTask(task_id=task_id, url=url, stage="downloading", status_message=msg, temp_dir=temp_dir)
        _active_tasks[user_id] = task

        loop = asyncio.get_running_loop()
        last_edit = {"t": 0.0}

        def on_progress(d: dict[str, Any]) -> None:
            status = d.get("status")
            now = time.monotonic()
            if status == "downloading":
                if now - last_edit["t"] < 4:
                    return
                last_edit["t"] = now
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                percent = (downloaded / total * 100) if total else 0.0
                kwargs = {
                    "bar": utils.progress_bar(percent),
                    "percent": f"{percent:.0f}",
                    "downloaded": utils.format_bytes(downloaded),
                    "total": utils.format_bytes(total) if total else "unknown",
                    "speed": utils.format_speed(d.get("speed")),
                    "eta": utils.format_eta(d.get("eta")),
                }
                asyncio.run_coroutine_threadsafe(safe_edit(msg, "download_progress", **kwargs), loop)
            elif status == "finished":
                asyncio.run_coroutine_threadsafe(safe_edit(msg, "processing"), loop)

        try:
            result = await downloader.download_video(
                url, quality, str(temp_dir), progress_callback=on_progress, cancel_event=task.cancel_event
            )
        except downloader.DownloadCancelledError:
            await safe_edit(msg, "cancel_success")
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error="cancelled")
            _cleanup_task(user_id, keep_temp=False)
            return
        except downloader.DownloadFailedError as exc:
            await safe_edit(msg, "error_download_failed", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error=str(exc))
            if on_failure:
                await on_failure(str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during download")
            await safe_edit(msg, "error_generic", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error=str(exc))
            if on_failure:
                await on_failure(str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return

        # Deliver via direct HTTP download link
        task.stage = "delivering"
        await _deliver_direct_link(
            chat_id=chat_id,
            status_msg=msg,
            filepath=result.filepath,
            title=result.title or info.title,
            platform=info.platform,
            url=url,
            quality_label=quality.label,
            user_id=user_id,
            username=username,
            on_success=on_success,
        )
        # Keep directory on disk for HTTP file serving until expired!
        _cleanup_task(user_id, keep_temp=True)


# ---------------------------------------------------------------------------
# Pinterest
# ---------------------------------------------------------------------------
async def _handle_pinterest_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    status_msg = await send(event, "pinterest_downloading")
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (Pinterest)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            result = await pinterest_service.download_pin(url, str(temp_dir))
        except pinterest_service.PinterestError as exc:
            await safe_edit(status_msg, "pinterest_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Pinterest error")
            await safe_edit(status_msg, "pinterest_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return

        if not result.is_carousel():
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=result.filepath(),
                title=result.title,
                platform="Pinterest",
                url=url,
                quality_label="اصل پین",
                user_id=user_id,
                username=username,
            )
        else:
            await safe_edit(status_msg, "pinterest_zip_building", count=len(result.items))
            zip_name = f"{utils.sanitize_filename(result.title, 60)}.zip"
            zip_path = utils.create_zip([i.filepath for i in result.items], temp_dir / zip_name)
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=str(zip_path),
                title=f"{result.title} ({len(result.items)} آیتم)",
                platform="Pinterest",
                url=url,
                quality_label=f"ZIP ({len(result.items)} آیتم)",
                user_id=user_id,
                username=username,
            )
        _cleanup_task(user_id, keep_temp=True)


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
async def _handle_instagram_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    status_msg = await send(event, "instagram_downloading")
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (Instagram)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            result = await instagram_service.download_post(url, str(temp_dir))
        except instagram_service.InstagramError as exc:
            await safe_edit(status_msg, "instagram_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Instagram", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Instagram error")
            await safe_edit(status_msg, "instagram_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Instagram", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return

        if not result.is_carousel():
            item = result.items[0]
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=item.filepath,
                title=result.title,
                platform="Instagram",
                url=url,
                quality_label="اصلی",
                user_id=user_id,
                username=username,
            )
        else:
            await safe_edit(status_msg, "instagram_zip_building", count=len(result.items))
            zip_name = f"{utils.sanitize_filename(result.title, 60)}.zip"
            zip_path = utils.create_zip([i.filepath for i in result.items], temp_dir / zip_name)
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=str(zip_path),
                title=f"{result.title} ({len(result.items)} آیتم)",
                platform="Instagram",
                url=url,
                quality_label=f"ZIP ({len(result.items)} آیتم)",
                user_id=user_id,
                username=username,
            )
        _cleanup_task(user_id, keep_temp=True)


# ---------------------------------------------------------------------------
# Twitter / X
# ---------------------------------------------------------------------------
async def _handle_twitter_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    status_msg = await send(event, "twitter_downloading")
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (Twitter/X)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            result = await twitter_service.download_tweet(url, str(temp_dir))
        except twitter_service.TwitterError as exc:
            await safe_edit(status_msg, "twitter_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Twitter", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Twitter error")
            await safe_edit(status_msg, "twitter_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Twitter", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return

        if not result.is_carousel():
            item = result.items[0]
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=item.filepath,
                title=result.title,
                platform="Twitter",
                url=url,
                quality_label="اصلی",
                user_id=user_id,
                username=username,
            )
        else:
            await safe_edit(status_msg, "twitter_zip_building", count=len(result.items))
            zip_name = f"{utils.sanitize_filename(result.title, 60)}.zip"
            zip_path = utils.create_zip([i.filepath for i in result.items], temp_dir / zip_name)
            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=str(zip_path),
                title=f"{result.title} ({len(result.items)} تصویر)",
                platform="Twitter",
                url=url,
                quality_label=f"ZIP ({len(result.items)} تصویر)",
                user_id=user_id,
                username=username,
            )
        _cleanup_task(user_id, keep_temp=True)


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------
async def _handle_soundcloud_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    is_playlist = "/sets/" in url.lower()
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id

    if not is_playlist:
        status_msg = await send(event, "soundcloud_downloading")
        task = ActiveTask(task_id=task_id, url=url, stage="downloading (SoundCloud)", status_message=status_msg, temp_dir=temp_dir)
        _active_tasks[user_id] = task

        async with download_semaphore:
            try:
                result = await soundcloud_service.download_track(url, str(temp_dir))
            except soundcloud_service.SoundCloudError as exc:
                await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
                _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
                _cleanup_task(user_id, keep_temp=False)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected SoundCloud error")
                await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
                _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
                _cleanup_task(user_id, keep_temp=False)
                return

            await _deliver_direct_link(
                chat_id=event.chat_id,
                status_msg=status_msg,
                filepath=result.filepath,
                title=result.title,
                platform="SoundCloud",
                url=url,
                quality_label="MP3 320kbps",
                user_id=user_id,
                username=username,
            )
            _cleanup_task(user_id, keep_temp=True)
        return

    # SoundCloud Playlist
    status_msg = await send(event, "extracting_info")
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (SoundCloud playlist)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            playlist = await soundcloud_service.extract_playlist(url, config.MAX_PLAYLIST_TRACKS)
        except soundcloud_service.SoundCloudError as exc:
            await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
            _cleanup_task(user_id, keep_temp=False)
            return

        await safe_edit(status_msg, "soundcloud_playlist_found", name=playlist.title, count=len(playlist.tracks))

        downloaded_paths: list[str] = []
        for idx, track in enumerate(playlist.tracks, start=1):
            await safe_edit(status_msg, "soundcloud_playlist_progress", index=idx, total=len(playlist.tracks), title=track.title)
            try:
                res = await soundcloud_service.download_track(track.url, str(temp_dir / str(idx)))
                downloaded_paths.append(res.filepath)
            except Exception as exc:  # noqa: BLE001
                await safe_edit(status_msg, "soundcloud_playlist_track_failed", index=idx, total=len(playlist.tracks), title=track.title, error=str(exc))
                await asyncio.sleep(1)

        if not downloaded_paths:
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error="no tracks")
            _cleanup_task(user_id, keep_temp=False)
            return

        await safe_edit(status_msg, "soundcloud_zip_building", count=len(downloaded_paths))
        zip_name = f"{utils.sanitize_filename(playlist.title, 60)}.zip"
        zip_path = utils.create_zip(downloaded_paths, temp_dir / zip_name)

        await _deliver_direct_link(
            chat_id=event.chat_id,
            status_msg=status_msg,
            filepath=str(zip_path),
            title=f"{playlist.title} ({len(downloaded_paths)} قطعه)",
            platform="SoundCloud",
            url=url,
            quality_label=f"ZIP ({len(downloaded_paths)} قطعه)",
            user_id=user_id,
            username=username,
        )
        _cleanup_task(user_id, keep_temp=True)


# ---------------------------------------------------------------------------
# PornHub (Optional)
# ---------------------------------------------------------------------------
async def _handle_pornhub_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    await send(event, "pornhub_link_received")
    await send(event, "pornhub_warning")
    status_msg = await send(event, "extracting_info")
    client: TelegramClient = event.client

    try:
        info = await downloader.extract_video_info(url)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(status_msg, "pornhub_error", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="PornHub", url=url, error=str(exc))
        await pornhub_service.notify_admin(
            client, pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title="unknown", status="failed", detail=str(exc))
        )
        return

    if not info.qualities:
        await safe_edit(status_msg, "pornhub_error", error="No quality found.")
        return

    best_quality = max(
        (q for q in info.qualities if q.kind == "video"),
        key=lambda q: (q.height or 0),
        default=info.qualities[0],
    )

    await safe_edit(status_msg, "pornhub_before_download", title=info.title, duration=utils.format_duration(info.duration))

    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    task_id = uuid.uuid4().hex[:10]
    await safe_edit(status_msg, "pornhub_download_started")

    async def on_success(filepath: str) -> None:
        await pornhub_service.notify_admin(
            client,
            pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title=info.title, status="success"),
        )

    async def on_failure(error: str) -> None:
        await pornhub_service.notify_admin(
            client,
            pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title=info.title, status="failed", detail=error),
        )

    asyncio.create_task(
        _run_download_and_deliver(
            event.chat_id, user_id, username, info, url, best_quality, status_msg, task_id,
            on_success=on_success, on_failure=on_failure,
        )
    )


# ---------------------------------------------------------------------------
# Admin Panel from Main Bot (Available directly to OWNER_ID)
# ---------------------------------------------------------------------------
async def cmd_admin(event: Message) -> None:
    if not is_owner(event.sender_id):
        return
    text = (
        "🛠 **پنل مدیریت ربات Vid-to-Link**\n\n"
        "دستورات مدیریت:\n"
        "/expiration — ⏳ تنظیم مدت زمان اعتبار لینک‌ها و فایل‌ها\n"
        "/storage — 💾 وضعیت دیسک ریل‌وی و اجرای پاکسازی فوری\n"
        "/limits — ⚙️ تنظیم سقف حجم و دانلودهای همزمان\n"
        "/stats — 📊 آمار کاربران و تعداد دانلودها\n"
        "/broadcast <text> — 📢 ارسال پیام همگانی"
    )
    buttons = [
        [Button.inline("⏳ مدت اعتبار فایل‌ها", "exp|menu"), Button.inline("💾 حافظه و دیسک", "stor|refresh")],
        [Button.inline("⚙️ محدودیت‌های دانلود", "lim|menu"), Button.inline("📊 آمار کلی", "adm|stats")],
    ]
    await event.respond(text, buttons=buttons)


async def cmd_expiration(event: Message) -> None:
    if not is_owner(event.sender_id):
        return
    current_sec = await admin_settings.get_file_expiration_seconds()
    human_fa = utils.format_time_remaining(current_sec)
    human_en = utils.format_time_remaining_en(current_sec)

    text = (
        "⏳ **تنظیم مدت زمان اعتبار فایل‌ها (File Expiration)**\n\n"
        f"مدت اعتبار فعلی: **{human_fa}** ({human_en} / {current_sec:,} ثانیه)\n\n"
        "پس از اتمام این زمان، فایل به‌صورت خودکار از دیسک ریل‌وی پاک شده و لینک منقضی می‌گردد.\n"
        "یک گزینه را انتخاب کنید یا زمان سفارشی بفرستید:"
    )
    buttons = [
        [
            Button.inline(f"{'✅ ' if current_sec == 900 else ''}۱۵ دقیقه", "expset|900"),
            Button.inline(f"{'✅ ' if current_sec == 1800 else ''}۳۰ دقیقه", "expset|1800"),
            Button.inline(f"{'✅ ' if current_sec == 3600 else ''}۱ ساعت", "expset|3600"),
        ],
        [
            Button.inline(f"{'✅ ' if current_sec == 7200 else ''}۲ ساعت", "expset|7200"),
            Button.inline(f"{'✅ ' if current_sec == 14400 else ''}۴ ساعت", "expset|14400"),
            Button.inline(f"{'✅ ' if current_sec == 21600 else ''}۶ ساعت", "expset|21600"),
        ],
        [
            Button.inline(f"{'✅ ' if current_sec == 43200 else ''}۱۲ ساعت", "expset|43200"),
            Button.inline(f"{'✅ ' if current_sec == 86400 else ''}۲۴ ساعت", "expset|86400"),
            Button.inline(f"{'✅ ' if current_sec == 172800 else ''}۴۸ ساعت", "expset|172800"),
        ],
        [Button.inline("✏️ زمان دلخواه (سفارشی)", "exp|custom")],
    ]
    await event.respond(text, buttons=buttons)


async def cmd_storage(event: Message) -> None:
    if not is_owner(event.sender_id):
        return
    disk = cleanup_worker.get_disk_usage_info()
    storage = await database.get_storage_stats()

    text = (
        "💾 **وضعیت دیسک و فایل‌های سرور (Railway Free Tier)**\n\n"
        f"📁 مسیر فایل‌ها: `{config.DOWNLOAD_PATH}`\n\n"
        "💽 **فضای دیسک:**\n"
        f"• کل فضا: {disk['total_formatted']}\n"
        f"• استفاده‌شده: {disk['used_formatted']} ({disk['percent']:.1f}%)\n"
        f"• فضای خالی: {disk['free_formatted']}\n\n"
        "📦 **فایل‌های دانلود شده:**\n"
        f"• فایل‌های دارای لینک معتبر: {storage['active_count']} عدد ({utils.format_bytes(storage['active_size_bytes'])})\n"
        f"• فایل‌های پاک‌شده یا منقضی: {storage['expired_count']} عدد\n"
        f"• کل دانلودها از لینک مستقیم: {storage['total_hits']} بار"
    )
    buttons = [
        [Button.inline("🧹 پاکسازی فوری فایل‌های منقضی", "stor|cleanup")],
        [Button.inline("🔄 به‌روزرسانی", "stor|refresh")],
    ]
    await event.respond(text, buttons=buttons)


# ---------------------------------------------------------------------------
# Event Dispatchers
# ---------------------------------------------------------------------------
def register_handlers(client: TelegramClient) -> None:
    async def dispatcher(event: Message) -> None:
        if not event.is_private:
            return
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        username = getattr(sender, "username", None)
        is_new = await database.upsert_user(sender.id, username, getattr(sender, "first_name", None))
        if is_new:
            _log(EventType.NEW_USER, sender.id, username)

        text = (event.raw_text or "").strip()

        # Check admin pending custom duration input
        if is_owner(sender.id) and sender.id in _admin_pending_input:
            action = _admin_pending_input.pop(sender.id, {}).get("action")
            if action == "set_expiration":
                sec = utils.parse_time_duration(text)
                if sec is None or sec < 60:
                    await event.respond("❌ زمان نامعتبر است. لطفاً مثلاً `90m`، `3h`، `1d` یا ثانیه وارد کنید.")
                    return
                await admin_settings.set_file_expiration_seconds(sec)
                await event.respond(
                    f"✅ مدت اعتبار فایل‌ها روی **{utils.format_time_remaining(sec)}** ({sec:,} ثانیه) تنظیم شد."
                )
                return

        try:
            if text.startswith("/start"):
                await cmd_start(event)
            elif text.startswith("/help"):
                await cmd_help(event)
            elif text.startswith("/platforms"):
                await cmd_platforms(event)
            elif text.startswith("/cancel"):
                await cmd_cancel(event, sender.id)
            elif text.startswith("/status"):
                await cmd_status(event, sender.id)
            elif text.startswith("/settings"):
                await cmd_settings(event)
            elif text.startswith("/about"):
                await cmd_about(event)
            elif text.startswith("/admin") and is_owner(sender.id):
                await cmd_admin(event)
            elif text.startswith("/expiration") and is_owner(sender.id):
                await cmd_expiration(event)
            elif text.startswith("/storage") and is_owner(sender.id):
                await cmd_storage(event)
            elif text in (messages.get("platforms_button"), messages.get("menu_platforms")):
                await cmd_platforms(event)
            elif text == messages.get("menu_help"):
                await cmd_help(event)
            elif text == messages.get("menu_settings"):
                await cmd_settings(event)
            elif text == messages.get("menu_about"):
                await cmd_about(event)
            elif text == messages.get("menu_download"):
                await cmd_menu_download_prompt(event)
            elif text.startswith("/"):
                await cmd_help(event)
            else:
                await handle_url_message(event, sender.id, username, text)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error processing message")
            _log(EventType.ERROR, sender.id, username, error="Unhandled error")
            try:
                await send(event, "error_generic", error="خطای داخلی ربات")
            except Exception:  # noqa: BLE001
                pass

    async def callback_dispatcher(event: events.CallbackQuery.Event) -> None:
        try:
            data = event.data.decode("utf-8", errors="ignore")
            parts = data.split("|")
            prefix = parts[0]

            if prefix == "q":
                await handle_quality_selected(event, parts)
            elif prefix == "cancelq":
                await handle_cancel_quality(event, parts)
            elif prefix == "linkinfo":
                token = parts[1] if len(parts) > 1 else ""
                link = await database.get_file_link(token)
                if not link or link["is_deleted"] or link["expires_at"] <= int(time.time()):
                    await event.answer("⚠️ این لینک منقضی شده و فایل از سرور حذف شده است.", alert=True)
                    return
                rem = max(0, link["expires_at"] - int(time.time()))
                info_text = (
                    f"📄 نام: {link['file_name']}\n"
                    f"📦 حجم: {utils.format_bytes(link['file_size'])}\n"
                    f"⏳ اعتبار باقی‌مانده: {utils.format_time_remaining(rem)}\n"
                    f"🕒 انقضا: {utils.format_timestamp_utc(link['expires_at'])}\n"
                    f"📥 دفعات دانلود: {link['download_count']} بار"
                )
                await event.answer(info_text, alert=True)

            # Owner admin callbacks from main bot
            elif is_owner(event.sender_id):
                if prefix == "expset" and len(parts) > 1:
                    sec = int(parts[1])
                    await admin_settings.set_file_expiration_seconds(sec)
                    await event.answer(f"✅ مدت اعتبار روی {utils.format_time_remaining(sec)} تنظیم شد.", alert=True)
                elif prefix == "exp" and len(parts) > 1 and parts[1] == "custom":
                    _admin_pending_input[event.sender_id] = {"action": "set_expiration"}
                    await event.answer()
                    await event.respond("✏️ لطفاً مدت زمان دلخواه را ارسال کنید (مثال: `90m`، `3h`، `1d` یا `7200`):")
                elif prefix == "stor":
                    action = parts[1] if len(parts) > 1 else "refresh"
                    if action == "cleanup":
                        stats = await cleanup_worker.run_cleanup_cycle()
                        await event.answer(
                            f"🧹 پاکسازی شد: {stats['deleted_count']} فایل حذف و {utils.format_bytes(stats['freed_bytes'])} آزاد شد.",
                            alert=True,
                        )
                    else:
                        await event.answer("به‌روزرسانی شد.")
                elif prefix == "adm" and len(parts) > 1 and parts[1] == "stats":
                    stats = await database.get_stats()
                    storage = await database.get_storage_stats()
                    await event.answer(
                        f"👥 کاربران: {stats['total_users']} | 📥 کل: {stats['total_downloads']}\n"
                        f"✅ موفق: {stats['successful_downloads']} | ❌ ناموفق: {stats['failed_downloads']}\n"
                        f"📂 فایل‌های فعال: {storage['active_count']}",
                        alert=True,
                    )
            else:
                await event.answer()
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error processing callback")
            try:
                await event.answer("خطایی رخ داد", alert=True)
            except Exception:  # noqa: BLE001
                pass

    client.add_event_handler(dispatcher, events.NewMessage(incoming=True))
    client.add_event_handler(callback_dispatcher, events.CallbackQuery())
    logger.info("Handlers registered successfully.")
