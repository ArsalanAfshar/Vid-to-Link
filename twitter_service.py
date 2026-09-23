"""
twitter_service.py
--------------------
X / Twitter media downloader supporting images, videos, GIFs and
multi-photo posts (up to 4 images), fixing the previous bug where the
generic yt-dlp path raised::

    ERROR: [twitter] ...: No video could be found in this tweet

That error is expected/by-design in yt-dlp's Twitter extractor: it only
ever looks for a *video* stream and gives up immediately on image-only
tweets, it never even tries to return photos. So downloading images
needs a dedicated code path instead of yt-dlp.

Strategy
--------
1. Primary: call the public, no-auth-required FixTweet API
   (``api.fxtwitter.com``), the same reliable data source powering
   twitter/x.com embed previews. It returns structured JSON with the
   *real* media type (photo/video/gif) and direct, full-resolution CDN
   URLs — no guessing based on file extensions or thumbnails.
2. Fallback for videos/GIFs only (if FixTweet is ever unreachable):
   yt-dlp, which handles Twitter videos/GIFs correctly (only image posts
   were ever the problem).
3. Last resort: scrape ``og:image`` / ``og:video`` from the tweet page
   itself (attribute-order-agnostic, mirrors instagram_service.py /
   pinterest_service.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp

import utils

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)

_META_TAG_RE = re.compile(r"<meta\b([^>]+)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"|(\w[\w:-]*)\s*=\s*\'([^\']*)\'')


class TwitterError(Exception):
    pass


@dataclass
class TwitterItem:
    index: int
    kind: str  # "image" | "video"
    filepath: str


@dataclass
class TwitterResult:
    title: str
    items: list[TwitterItem] = field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return len(self.items) > 1


def _extract_status_id(url: str) -> Optional[str]:
    match = _STATUS_ID_RE.search(url)
    return match.group(1) if match else None


def _find_meta_content(html: str, prop: str) -> Optional[str]:
    for match in _META_TAG_RE.finditer(html):
        attrs: dict[str, str] = {}
        for m in _ATTR_RE.finditer(match.group(1)):
            key = (m.group(1) or m.group(3) or "").lower()
            val = m.group(2) if m.group(1) is not None else m.group(4)
            attrs[key] = val
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key == prop.lower() and "content" in attrs:
            return attrs["content"]
    return None


def _best_quality_photo_url(url: str) -> str:
    """Force the original/full resolution variant of a pbs.twimg.com image."""
    if "pbs.twimg.com" not in url:
        return url
    base = url.split("?")[0]
    return f"{base}?format=jpg&name=orig"


async def _download_bytes(session: aiohttp.ClientSession, url: str, dest: Path) -> None:
    async with session.get(url) as resp:
        if resp.status != 200:
            raise TwitterError(f"Downloading the media file failed (HTTP {resp.status}).")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            async for chunk in resp.content.iter_chunked(1 << 16):
                fh.write(chunk)


async def _via_fxtwitter(session: aiohttp.ClientSession, status_id: str, out_dir: Path) -> Optional[TwitterResult]:
    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    try:
        async with session.get(api_url, headers={"User-Agent": _UA}) as resp:
            if resp.status != 200:
                logger.info("fxtwitter API returned HTTP %s for status %s", resp.status, status_id)
                return None
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.info("fxtwitter API call failed: %s", exc)
        return None

    tweet = payload.get("tweet") or payload.get("status")
    if not isinstance(tweet, dict):
        return None

    title = (tweet.get("text") or "").strip().splitlines()[0][:120] if tweet.get("text") else ""
    author = tweet.get("author") or {}
    if not title:
        title = f"tweet_{author.get('screen_name') or status_id}"

    media = tweet.get("media") or {}
    photos = media.get("photos") or []
    videos = media.get("videos") or []

    items: list[TwitterItem] = []
    index = 1

    for photo in photos:
        photo_url = photo.get("url")
        if not photo_url:
            continue
        photo_url = _best_quality_photo_url(photo_url)
        dest = out_dir / f"{index:02d}_{utils.sanitize_filename(title, 60)}.jpg"
        try:
            await _download_bytes(session, photo_url, dest)
        except TwitterError as exc:
            logger.warning("Skipping tweet photo %s: %s", index, exc)
            continue
        items.append(TwitterItem(index=index, kind="image", filepath=str(dest)))
        index += 1

    for video in videos:
        video_url = video.get("url")
        if not video_url:
            continue
        dest = out_dir / f"{index:02d}_{utils.sanitize_filename(title, 60)}.mp4"
        try:
            await _download_bytes(session, video_url, dest)
        except TwitterError as exc:
            logger.warning("Skipping tweet video %s: %s", index, exc)
            continue
        items.append(TwitterItem(index=index, kind="video", filepath=str(dest)))
        index += 1

    if not items:
        return None
    return TwitterResult(title=title, items=items)


async def _via_ytdlp(url: str, out_dir: Path) -> Optional[TwitterResult]:
    """yt-dlp handles Twitter/X videos & GIFs correctly; only image-only
    tweets fail there (that's exactly what the other code paths cover)."""
    import yt_dlp

    loop = asyncio.get_running_loop()

    def _run() -> Optional[dict]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "format": "(bestvideo+bestaudio/best)[vcodec!=none]",
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "socket_timeout": 20,
            "retries": 3,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as exc:  # noqa: BLE001
            logger.info("yt-dlp could not download this tweet (likely image-only): %s", exc)
            return None

    info = await loop.run_in_executor(None, _run)
    if not info:
        return None

    requested = info.get("requested_downloads") or []
    candidate = None
    if requested:
        candidate = requested[0].get("filepath") or requested[0].get("_filename")
    if not candidate:
        candidate = info.get("filepath") or info.get("_filename")
    if not candidate or not Path(candidate).exists():
        return None

    title = (info.get("title") or "tweet").strip()
    final_name = utils.build_track_filename(title, ext=Path(candidate).suffix.lstrip("."))
    final_path = utils.unique_path(out_dir, final_name)
    Path(candidate).replace(final_path)
    return TwitterResult(title=title, items=[TwitterItem(index=1, kind="video", filepath=str(final_path))])


async def _via_html_fallback(session: aiohttp.ClientSession, url: str, out_dir: Path) -> TwitterResult:
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    async with session.get(url, allow_redirects=True, headers=headers) as resp:
        if resp.status != 200:
            raise TwitterError(f"This tweet's page is not reachable (status {resp.status}).")
        html = await resp.text(errors="ignore")

    title = _find_meta_content(html, "og:title") or "tweet"
    video_url = _find_meta_content(html, "og:video") or _find_meta_content(html, "og:video:secure_url")
    if video_url:
        dest = out_dir / f"{utils.sanitize_filename(title, 60)}.mp4"
        await _download_bytes(session, video_url, dest)
        return TwitterResult(title=title, items=[TwitterItem(index=1, kind="video", filepath=str(dest))])

    image_url = _find_meta_content(html, "og:image")
    if not image_url:
        raise TwitterError("No image or video could be found in this tweet.")
    image_url = _best_quality_photo_url(image_url)
    dest = out_dir / f"{utils.sanitize_filename(title, 60)}.jpg"
    await _download_bytes(session, image_url, dest)
    return TwitterResult(title=title, items=[TwitterItem(index=1, kind="image", filepath=str(dest))])


async def download_tweet(url: str, out_dir: str) -> TwitterResult:
    """Download every media item (image(s), video or GIF) from a tweet,
    preserving original quality and order. Never assumes a tweet must
    contain a video — image-only and mixed-media posts are both handled
    explicitly."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        status_id = _extract_status_id(url)

        if status_id:
            result = await _via_fxtwitter(session, status_id, out_path)
            if result:
                return result

        result = await _via_ytdlp(url, out_path)
        if result:
            return result

        return await _via_html_fallback(session, url, out_path)
