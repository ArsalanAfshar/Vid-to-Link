"""
downloader.py
-------------
Integration layer with yt-dlp for extracting info and downloading video from hundreds of platforms.

Because yt-dlp is synchronous, every call to it runs inside a
thread executor so the main asyncio event loop never blocks.

The architecture is designed so that every yt-dlp update / newly added
extractor automatically adds support for more platforms;
no platform-specific logic is hardcoded in this file (except a few
genuinely necessary YouTube helpers, like choosing the player_client).

Reliability notes and the reasoning behind each design decision:

1) «Sign in to confirm you're not a bot» / «The page needs to be reloaded» /
   «Requested format is not available»:
   These errors almost always happen for one of these reasons:
      a) The chosen YouTube player_client is temporarily broken;
           YouTube constantly blocks/unblocks different player clients.
      b) The high quality format needs a PO Token (Proof-of-Origin), which cannot
         be generated without a real provider.
   Solution: instead of hardcoding one fixed combination of clients (which breaks
   every few weeks), a fallback ladder is used: first
   yt-dlp's own defaults are tried (kept up to date by its maintainers),
   then a few other known-good combinations. In addition, installing
   `bgutil-ytdlp-pot-provider` + Deno (see Dockerfile)
   generates a real PO Token, enabling reliable high quality (720p/1080p/4K) DASH downloads
   — this has been directly tested and confirmed.

2) Fake qualities (27p/45p/108p) -> these are 'storyboard' formats (the scrub-bar
   preview tiles), not real video. They are now explicitly filtered
   out (see `_is_real_video_format`).

3) File size used to only show correctly for 360p -> because on YouTube, formats above
   360p are usually DASH (separate video/audio files), so the video-only file
   alone did not include audio size. Size is now video size + best available
   audio format size (see `_build_quality_options`).

4) 'The downloaded file is empty' at 720p/1080p -> this usually means
   the selected format's URL required a PO Token and without one returns an
   empty/truncated response. Installing a PO Token provider fixes this; additionally
   a fallback chain was added to the download itself: if the final file is 0 bytes,
   it automatically retries with a different client/format combination, so
   the user never ends up with an empty file.

5) Selected quality not matching the actually downloaded quality -> the format is now
   locked to the exact `format_id` identified when qualities were listed (not
   just a generic height cap), so the format whose size was shown to the user
   is the one actually downloaded (with a safe fallback if it's gone by download time).

6) Multi-platform support (e.g. PornHub used to return HTTP 403) -> these sites
   usually have a TLS-fingerprint-based anti-bot layer and need `impersonate`
   (real Chrome/Firefox/Safari browser impersonation via curl_cffi)
   which is why installing `yt-dlp[default,curl-cffi]` (see requirements.txt)
   lets extractors like PornHub use this automatically;
   this was directly tested: without curl_cffi you get a 403, with it the request
   succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

import utils

logger = logging.getLogger(__name__)

# Maximum number of qualities shown to the user
MAX_QUALITY_OPTIONS = 8

# Lowest height still considered a real, usable quality
MIN_USABLE_HEIGHT = 144

_COOKIE_FILE_PATH = "/tmp/downloader_cookies.txt"


def _env_list(name: str, default: list[str]) -> list[str]:
    """Read a comma separated environment variable into a list."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


def _get_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_TRANSIENT_ERROR_HINTS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporary failure",
    "network is unreachable",
    "remote end closed connection",
    "server disconnected",
)


def _looks_transient(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_ERROR_HINTS)


def _get_cookie_file() -> Optional[str]:
    """
    Build a single cookies.txt file (Netscape format) from environment variables.

    - YOUTUBE_COOKIES: YouTube cookies (bypass bot checks and
      age/member-restricted videos).
    - EXTRA_COOKIES: optional cookies for other platforms (e.g. PornHub,
      Instagram, Twitter/X, ...) in the same Netscape format.

    Both values (if present) are merged into a single file; since yt-dlp
    filters cookies by domain, merging them into one file is completely
    safe and every site only receives its own cookies.
    """

    raw_blocks = [
        os.environ.get("YOUTUBE_COOKIES", "").strip(),
        os.environ.get("EXTRA_COOKIES", "").strip(),
    ]
    blocks = [b for b in raw_blocks if b]
    if not blocks:
        return None

    lines: list[str] = ["# Netscape HTTP Cookie File"]
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("# Netscape"):
                continue
            lines.append(line)

    try:
        with open(_COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return _COOKIE_FILE_PATH
    except Exception as exc:  # noqa: BLE001
        logger.error("Cookie file creation failed: %s", exc)
        return None


class ExtractionError(Exception):
    """Error while extracting video information."""


class DownloadFailedError(Exception):
    """Error during the download process."""


class DownloadCancelledError(Exception):
    """Download cancelled by the user."""


@dataclass
class QualityOption:
    id: str
    label: str
    kind: str  # "video" | "audio"
    height: Optional[int] = None
    filesize: Optional[int] = None
    format_id: Optional[str] = None

    def format_selector(self) -> str:
        """
        Build a real, safe format selector string for yt-dlp.

        The selection chain works like this:
        1) The exact format_id identified/measured when qualities were listed
           (guarantees the quality the user picked matches what is downloaded).
        2) If that exact format is gone (e.g. the format list changed),
           the best video at the same height + best audio.
        3) Finally, the best quality available regardless of height, so the user never hits
           'Requested format is not available'.
        """

        if self.kind == "audio":
            return "bestaudio/best"

        chain: list[str] = []

        if self.format_id:
            chain.append(
                f"{self.format_id}+bestaudio[ext=m4a]/"
                f"{self.format_id}+bestaudio/"
                f"{self.format_id}"
            )

        if self.height:
            h = int(self.height)
            chain.extend(
                [
                    f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]",
                    f"bestvideo[height<={h}]+bestaudio",
                    f"best[height<={h}]",
                ]
            )

        chain.append("bestvideo+bestaudio/best")
        return "/".join(chain)


@dataclass
class VideoInfo:
    id: str
    title: str
    platform: str
    duration: Optional[float]
    thumbnail: Optional[str]
    webpage_url: str
    qualities: list[QualityOption] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadResult:
    filepath: str
    title: str
    duration: Optional[float]
    thumbnail: Optional[str]
    filesize: int


def _is_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com" in lowered or "youtu.be" in lowered


def _client_attempts() -> list[Optional[list[str]]]:
    """
    List of different YouTube player_client combinations tried in order.

    None means 'use yt-dlp's own defaults', which are
    kept continuously up to date by its maintainers based on the latest
    YouTube changes; in direct testing (with a PO Token provider) this mode
    gave the best result (up to 4K with accurate size). Other combinations are just
    fallbacks so that if YouTube temporarily blocks a specific client
    (which happens a lot), the bot keeps working.

    YOUTUBE_PLAYER_CLIENTS lets you override the first combination without
    touching the code (e.g. when YouTube changes behaviour again).
    """

    attempts: list[Optional[list[str]]] = []

    env_override = _env_list("YOUTUBE_PLAYER_CLIENTS", [])
    if env_override:
        attempts.append(env_override)

    attempts.append(None)

    for combo in (
        ["tv", "web_safari"],
        ["tv", "web_safari", "android", "ios"],
        ["web", "mweb"],
        ["ios"],
    ):
        if combo not in attempts:
            attempts.append(combo)

    return attempts


def _base_ydl_opts(player_clients: Optional[list[str]] = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": _get_int_env("DOWNLOADER_SOCKET_TIMEOUT", 30),
        "retries": _get_int_env("DOWNLOADER_RETRIES", 5),
        "fragment_retries": _get_int_env("DOWNLOADER_FRAGMENT_RETRIES", 10),
        "extractor_retries": _get_int_env("DOWNLOADER_EXTRACTOR_RETRIES", 3),
        "geo_bypass": True,
        # On Railway's Free Plan (limited CPU/RAM) a lower number is more stable;
        # tunable via CONCURRENT_FRAGMENT_DOWNLOADS if needed.
        "concurrent_fragment_downloads": _get_int_env("CONCURRENT_FRAGMENT_DOWNLOADS", 4),
        # Forces ranged (Range) requests; a common cause of the
        # 'The downloaded file is empty' is trying to fetch a format with one
        # error is one big continuous request getting cut off by YouTube throttling.
        "http_chunk_size": _get_int_env("DOWNLOADER_HTTP_CHUNK_MB", 10) * 1024 * 1024,
        # Prefer the best resolution/codec/container when formats tie
        "format_sort": ["res", "fps", "codec:h264", "ext:mp4:m4a", "+size", "+br"],
    }

    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    cookie_file = _get_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


async def extract_video_info(url: str) -> VideoInfo:
    """
    Extract video information (without downloading) using yt-dlp, with an
    Outer retry layer for transient network errors (e.g. a temporary connection
    drop on Railway), fully independent from the client fallback chain
    YouTube client fallback chain.
    """
    outer_retries = _get_int_env("DOWNLOADER_OUTER_RETRIES", 2)
    last_exc: Optional[BaseException] = None
    for outer_attempt in range(outer_retries):
        try:
            return await _extract_video_info_once(url)
        except ExtractionError as exc:
            last_exc = exc
            if outer_attempt + 1 >= outer_retries or not _looks_transient(exc):
                raise
            logger.warning("Transient extraction error, retrying (%d/%d): %s", outer_attempt + 1, outer_retries, exc)
            await asyncio.sleep(2 * (outer_attempt + 1))
    raise last_exc if last_exc else ExtractionError("Failed to extract video information.")


async def _extract_video_info_once(url: str) -> VideoInfo:
    """
    Extract format info for a URL.

    Root cause of "only 360p is available" on YouTube (investigated and
    reproduced):
    For a YouTube URL this used to try player_client combinations one at a
    time and *return immediately after the very first attempt that produced
    ANY real quality option at all* — even if that attempt only exposed
    360p because the specific client used for that attempt is one of the
    clients YouTube requires a PO Token for (see the PO Token guide) and
    the higher-resolution DASH formats were silently absent from that
    client's format list (not blocked — just never listed). The later
    attempts in `_client_attempts()` (e.g. ``tv``+``web_safari``, which does
    NOT require a PO Token) that could have exposed 720p/1080p/4K were then
    never even tried, because the loop already returned.

    Fix: for YouTube, every client combination is now tried and the
    resulting formats are *merged* (deduplicated by ``format_id``) into a
    single pool before building the quality ladder, so the final list of
    qualities shown to the user is the union of everything reachable across
    every client — not just whatever the first (possibly PO-Token-limited)
    client happened to expose. To keep this cheap on Railway's limited
    CPU/RAM, the loop still stops early once a genuinely high quality
    (>=1080p by default) has already been found.
    """
    loop = asyncio.get_running_loop()
    is_youtube = _is_youtube_url(url)
    attempts = _client_attempts() if is_youtube else [None]
    last_error: Optional[BaseException] = None

    good_enough_height = _get_int_env("DOWNLOADER_GOOD_ENOUGH_HEIGHT", 1080)
    max_attempts = _get_int_env("DOWNLOADER_MAX_CLIENT_ATTEMPTS", len(attempts))

    base_info: Optional[dict[str, Any]] = None
    merged_formats: dict[Any, dict[str, Any]] = {}
    successes = 0

    for attempt_index, clients in enumerate(attempts):
        if attempt_index >= max_attempts and merged_formats:
            break

        def _extract(clients=clients) -> dict[str, Any]:
            opts = _base_ydl_opts(clients)
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await loop.run_in_executor(None, _extract)
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            logger.warning(
                "Extraction attempt %d/%d failed (clients=%s): %s",
                attempt_index + 1, len(attempts), clients, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "Unexpected extraction error (clients=%s): %s", clients, exc
            )
            continue

        if not info:
            last_error = ExtractionError("No information could be extracted from this link.")
            continue

        if info.get("_type") == "playlist":
            entries = list(info.get("entries") or [])
            if not entries:
                last_error = ExtractionError("No video was found at this link.")
                continue
            info = entries[0]

        successes += 1
        if base_info is None:
            base_info = info

        new_formats = 0
        for fmt in info.get("formats") or []:
            fmt_id = fmt.get("format_id")
            key = fmt_id if fmt_id is not None else id(fmt)
            if key not in merged_formats:
                merged_formats[key] = fmt
                new_formats += 1

        logger.info(
            "Extraction attempt %d/%d (clients=%s) added %d new format(s), %d total so far.",
            attempt_index + 1, len(attempts), clients, new_formats, len(merged_formats),
        )

        if not is_youtube:
            # Non-YouTube extractors only ever run a single attempt (see
            # `attempts` above); nothing more to merge.
            break

        current_qualities = _build_quality_options({**base_info, "formats": list(merged_formats.values())})
        best_height = max((q.height or 0) for q in current_qualities) if current_qualities else 0
        if best_height >= good_enough_height:
            logger.info("Reached a good-enough quality (%sp); stopping further client attempts.", best_height)
            break

    if base_info is None or not merged_formats:
        message = str(last_error) if last_error else "Failed to extract video information."
        if last_error is not None:
            raise ExtractionError(message) from last_error
        raise ExtractionError(message)

    merged_info = dict(base_info)
    merged_info["formats"] = list(merged_formats.values())

    qualities = _build_quality_options(merged_info)
    if not qualities:
        raise ExtractionError("No downloadable format was found.")

    platform = (base_info.get("extractor_key") or base_info.get("extractor") or "unknown").strip()
    title = (base_info.get("title") or "Untitled").strip()

    return VideoInfo(
        id=str(base_info.get("id") or "video"),
        title=title,
        platform=platform,
        duration=base_info.get("duration"),
        thumbnail=base_info.get("thumbnail"),
        webpage_url=base_info.get("webpage_url") or url,
        qualities=qualities,
        raw=merged_info,
    )


def _is_real_video_format(fmt: dict[str, Any]) -> bool:
    """
    Detect whether an entry in info["formats"] is truly a downloadable video format
    is actually downloadable.

    'storyboard' formats (YouTube's tiny scrub-bar preview tiles) show up in
    this same list and have a height value (e.g. 27, 45, 90, 108) but
    are not real video; this is what used to cause bogus qualities to show up.
    """

    if fmt.get("vcodec") in (None, "none"):
        return False

    if fmt.get("has_drm"):
        return False

    protocol = (fmt.get("protocol") or "").lower()
    if "mhtml" in protocol:
        return False

    if (fmt.get("format_note") or "").strip().lower() == "storyboard":
        return False

    if (fmt.get("ext") or "").lower() == "mhtml":
        return False

    # Formats with no valid URL because of a missing PO Token
    if not fmt.get("url") and not fmt.get("fragments") and not fmt.get("manifest_url"):
        return False

    return True


def _estimate_size_from_bitrate(fmt: dict[str, Any], duration: Optional[float]) -> int:
    """
    Estimate size from bitrate x duration, for formats (usually HLS/m3u8)
    that yt-dlp reports no filesize/filesize_approx for. Without this
    estimate, HLS-only qualities used to show size as 'unknown'.
    """

    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if not tbr or not duration:
        return 0
    try:
        return int(float(tbr) * 1000 / 8 * float(duration))
    except (TypeError, ValueError):
        return 0


def _format_size(fmt: dict[str, Any], duration: Optional[float] = None) -> int:
    size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
    if size:
        return size
    return _estimate_size_from_bitrate(fmt, duration)


def _pick_best_audio(formats: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    audio_formats = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    if not audio_formats:
        return None

    def score(f: dict[str, Any]) -> float:
        return float(f.get("abr") or f.get("tbr") or 0)

    return max(audio_formats, key=score)


def _build_quality_options(info: dict[str, Any]) -> list[QualityOption]:
    all_formats = info.get("formats") or []
    real_formats = [f for f in all_formats if _is_real_video_format(f)]
    duration = info.get("duration")

    logger.info(
        "Available real video heights: %s",
        sorted({f.get("height") for f in real_formats if f.get("height")}),
    )

    video_formats = [f for f in real_formats if f.get("vcodec") not in (None, "none")]

    best_audio = _pick_best_audio(all_formats)
    best_audio_size = _format_size(best_audio, duration) if best_audio else 0

    # Best candidate for each real height bucket
    buckets: dict[int, dict[str, Any]] = {}

    for fmt in video_formats:
        height = fmt.get("height")
        if not height or height < MIN_USABLE_HEIGHT:
            continue

        has_own_audio = fmt.get("acodec") not in (None, "none")
        own_size = _format_size(fmt, duration)

        if has_own_audio:
            # Progressive format: video and audio are already combined
            total_size = own_size
        elif own_size:
            # DASH format: best audio size must be added, otherwise the real size
            # otherwise the real size would not be shown (the originally reported bug)
            total_size = own_size + best_audio_size
        else:
            total_size = 0

        protocol = (fmt.get("protocol") or "").lower()
        has_exact_size = bool(fmt.get("filesize") or fmt.get("filesize_approx"))

        candidate = {
            "format_id": fmt.get("format_id"),
            "filesize": total_size or None,
            "tbr": float(fmt.get("tbr") or fmt.get("vbr") or 0),
            # Tie-break priority when multiple formats share a height: exact size known >
            # direct protocol (https) > compatible codec (avc1/h264) > higher bitrate
            "rank": (
                1 if has_exact_size else 0,
                1 if protocol in ("https", "http") else 0,
                1 if str(fmt.get("vcodec") or "").startswith("avc1") else 0,
            ),
        }

        current = buckets.get(height)
        if current is None:
            buckets[height] = candidate
            continue

        if candidate["rank"] > current["rank"] or (
            candidate["rank"] == current["rank"] and candidate["tbr"] > current["tbr"]
        ):
            buckets[height] = candidate

    sorted_heights = sorted(buckets.keys(), reverse=True)[:MAX_QUALITY_OPTIONS]

    options = [
        QualityOption(
            id=f"h{h}",
            label=f"🎬 {h}p",
            kind="video",
            height=h,
            filesize=buckets[h]["filesize"],
            format_id=buckets[h]["format_id"],
        )
        for h in sorted_heights
    ]

    has_audio_track = best_audio is not None
    if has_audio_track or not options:
        options.append(
            QualityOption(
                id="audio",
                label="🎵 فقط صدا (MP3)",
                kind="audio",
                filesize=best_audio_size or None,
            )
        )

    return options


ProgressCallback = Callable[[dict[str, Any]], None]


def _find_downloaded_file(info: dict[str, Any], out_dir: str, quality: QualityOption) -> str:
    """
    Robustly locate the final downloaded/converted file path across
    different yt-dlp versions (the filepath key differs between them).
    """

    requested = info.get("requested_downloads") or []
    if requested:
        candidate = requested[0].get("filepath") or requested[0].get("_filename")
        if candidate and Path(candidate).exists():
            return candidate

    candidate = info.get("filepath") or info.get("_filename")
    if candidate and Path(candidate).exists():
        return candidate

    video_id = info.get("id") or "video"
    matches = sorted(Path(out_dir).glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if matches:
        return str(matches[0])

    raise DownloadFailedError("The downloaded file could not be found on disk.")


def _clear_output_dir(out_dir: str) -> None:
    """Clean up incomplete/empty files between download attempts."""
    for p in Path(out_dir).glob("*"):
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _download_attempts(
    quality: QualityOption, is_youtube: bool
) -> list[tuple[str, Optional[list[str]]]]:
    """
    Download attempt chain: (format_selector, player_clients).

    If the first attempt returns an empty/failed file (usually because it needs
    a PO Token or a temporarily blocked YouTube client), the next attempts
    try a different format/client so the user always ends up with a valid file
    ends up with a valid file.
    """

    if quality.kind == "audio":
        attempts = [(quality.format_selector(), None)]
        if is_youtube:
            attempts.append((quality.format_selector(), ["tv", "web_safari"]))
        return attempts

    attempts = [(quality.format_selector(), None)]

    if is_youtube:
        attempts.append((quality.format_selector(), ["tv", "web_safari"]))
        if quality.height:
            attempts.append(
                (f"best[height<={quality.height}]/best", ["tv", "web_safari", "android", "ios"])
            )
        attempts.append(("best", None))

    return attempts


async def download_video(
    url: str,
    quality: QualityOption,
    out_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadResult:
    """
    Download the video at the chosen quality, muxing audio/video with FFmpeg if needed,
    plus an outer retry layer for transient network errors.
    """
    outer_retries = _get_int_env("DOWNLOADER_OUTER_RETRIES", 2)
    last_exc: Optional[BaseException] = None
    for outer_attempt in range(outer_retries):
        try:
            return await _download_video_once(url, quality, out_dir, progress_callback, cancel_event)
        except DownloadCancelledError:
            raise
        except DownloadFailedError as exc:
            last_exc = exc
            if outer_attempt + 1 >= outer_retries or not _looks_transient(exc):
                raise
            logger.warning("Transient download error, retrying (%d/%d): %s", outer_attempt + 1, outer_retries, exc)
            await asyncio.sleep(2 * (outer_attempt + 1))
    raise last_exc if last_exc else DownloadFailedError("Download failed.")


async def _download_video_once(
    url: str,
    quality: QualityOption,
    out_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadResult:
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    def _hook(d: dict[str, Any]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by the user.")
        if progress_callback is not None:
            try:
                progress_callback(d)
            except DownloadCancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    is_youtube = _is_youtube_url(url)
    attempts = _download_attempts(quality, is_youtube)
    loop = asyncio.get_running_loop()
    last_exc: Optional[BaseException] = None

    for attempt_index, (selector, clients) in enumerate(attempts):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by the user.")

        opts = _base_ydl_opts(clients)
        opts.update(
            {
                "format": selector,
                "outtmpl": str(Path(out_dir) / "%(id)s.%(ext)s"),
                "progress_hooks": [_hook],
                "restrictfilenames": True,
            }
        )

        if quality.kind == "audio":
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            # only needed when two separate streams (video + audio) might be downloaded
            opts["merge_output_format"] = "mp4"

        def _download() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        start = time.monotonic()
        try:
            info = await loop.run_in_executor(None, _download)
        except DownloadCancelledError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            logger.warning(
                "Download attempt %d/%d failed (format=%s clients=%s): %s",
                attempt_index + 1, len(attempts), selector, clients, exc,
            )
            _clear_output_dir(out_dir)
            continue
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "Unexpected download error (format=%s clients=%s): %s", selector, clients, exc
            )
            _clear_output_dir(out_dir)
            continue

        if not info:
            last_exc = DownloadFailedError("Download failed.")
            _clear_output_dir(out_dir)
            continue

        if info.get("_type") == "playlist":
            entries = list(info.get("entries") or [])
            if not entries:
                last_exc = DownloadFailedError("Download failed.")
                _clear_output_dir(out_dir)
                continue
            info = entries[0]

        try:
            filepath = _find_downloaded_file(info, out_dir, quality)
            filesize = Path(filepath).stat().st_size
        except (DownloadFailedError, OSError) as exc:
            last_exc = exc
            _clear_output_dir(out_dir)
            continue

        if filesize <= 0:
            logger.warning(
                "Attempt %d/%d produced an empty file (format=%s clients=%s); retrying with fallback",
                attempt_index + 1, len(attempts), selector, clients,
            )
            last_exc = DownloadFailedError("The downloaded file was empty.")
            _clear_output_dir(out_dir)
            continue

        elapsed = time.monotonic() - start
        logger.info(
            "Download finished in %.1fs -> %s (%s bytes) [attempt %d/%d]",
            elapsed, filepath, filesize, attempt_index + 1, len(attempts),
        )

        title = (info.get("title") or "video").strip()

        # Rename the file on disk from yt-dlp's id/hash based name
        # (%(id)s.%(ext)s, used above so re-attempts never collide) to a
        # filesystem-safe version of the *real title* before handing it back.
        # This guarantees every downloaded file — audio in particular — is
        # delivered to Telegram with a clean "Title.ext" filename instead of
        # a random video id/hash, regardless of platform (YouTube, TikTok,
        # Instagram, SoundCloud, ...).
        try:
            src = Path(filepath)
            desired_name = utils.build_track_filename(title, ext=src.suffix.lstrip("."))
            if src.name != desired_name:
                dest = utils.unique_path(src.parent, desired_name)
                src.replace(dest)
                filepath = str(dest)
        except OSError as exc:  # noqa: BLE001
            logger.debug("Could not rename downloaded file to title-based name: %s", exc)

        return DownloadResult(
            filepath=filepath,
            title=title,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            filesize=filesize,
        )

    if isinstance(last_exc, DownloadFailedError):
        raise last_exc
    raise DownloadFailedError(str(last_exc) if last_exc else "Download failed.")
