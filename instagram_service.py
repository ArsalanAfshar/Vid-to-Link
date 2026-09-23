"""
instagram_service.py
----------------------
Instagram media downloader supporting:
 - Single image posts (downloaded at original resolution, sent as a file).
 - Single video posts / Reels.
 - Carousel (slideshow) posts with mixed image/video items, downloaded in
   their original order.

Strategy
--------
1. Use yt-dlp first (it understands Instagram's post/reel JSON API and
   correctly returns every item of a carousel as a playlist entry,
   including plain image slides on recent versions).
2. If yt-dlp returns nothing usable (common for pure-image posts on older
   yt-dlp releases, or when Instagram requires a session), fall back to
   scraping the public page for the `display_url` (image) / `video_url`
   fields embedded in the post's JSON, which still works for public posts
   without login.

Optional `INSTAGRAM_COOKIES` (Netscape format) can be provided via the
environment to improve reliability for rate-limited/borderline posts.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yt_dlp

import config
import utils

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_COOKIE_FILE_PATH = "/tmp/instagram_cookies.txt"

_DISPLAY_URL_RE = re.compile(r'"display_url"\s*:\s*"([^"]+)"')
_VIDEO_URL_RE = re.compile(r'"video_url"\s*:\s*"([^"]+)"')
_IS_VIDEO_RE = re.compile(r'"is_video"\s*:\s*(true|false)')
# Used by the HTML fallback path to read the post title / preview image from
# the page's Open Graph <meta> tags (attribute-order-agnostic, see
# `_find_meta_content` below). Keeping these as plain string constants (not
# regexes) avoids the previous ``NameError: name '_TITLE_RE' is not defined``
# bug that broke every Instagram image-post download through this fallback.
_OG_TITLE_PROP = "og:title"
_OG_IMAGE_PROP = "og:image"

# NOTE: attribute-order-agnostic. Instagram (like Pinterest) sometimes
# renders `content="..."` *before* `property="og:..."` inside a <meta>
# tag; a strict `property=...content=...` regex silently fails to match
# in that case, which used to break this fallback path entirely.
_META_TAG_RE = re.compile(r"<meta\b([^>]+)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"|(\w[\w:-]*)\s*=\s*\'([^\']*)\'')


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


class InstagramError(Exception):
    pass


@dataclass
class InstagramItem:
    index: int
    kind: str  # "image" | "video"
    filepath: str


@dataclass
class InstagramResult:
    title: str
    items: list[InstagramItem] = field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return len(self.items) > 1


def _cookie_file() -> Optional[str]:
    if not config.INSTAGRAM_COOKIES:
        return None
    try:
        with open(_COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(config.INSTAGRAM_COOKIES)
        return _COOKIE_FILE_PATH
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write Instagram cookie file: %s", exc)
        return None


def _base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 25,
        "retries": 3,
        "extractor_retries": 2,
        "geo_bypass": True,
        "user_agent": _UA,
    }
    cookie_file = _cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def _best_video_format(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Pick the highest-resolution real video format from an entry's
    ``formats`` list (populated by yt-dlp's Instagram extractor from
    ``video_versions`` — see the root-cause note on `_download_via_ytdlp`)."""
    formats = [
        f for f in (entry.get("formats") or [])
        if f.get("url") and f.get("vcodec") not in (None, "none")
    ]
    if not formats:
        return None

    def _area(f: dict[str, Any]) -> int:
        return int(f.get("width") or 0) * int(f.get("height") or 0)

    return max(formats, key=_area)


def _best_photo_url(entry: dict[str, Any]) -> Optional[str]:
    """Recover the real, original-resolution photo URL for a photo-only
    entry. See the root-cause note on `_download_via_ytdlp` for why this
    has to be read from ``thumbnails`` instead of ``formats``/``url``."""
    thumbnails = [t for t in (entry.get("thumbnails") or []) if t.get("url")]
    if not thumbnails:
        return entry.get("thumbnail")

    def _area(t: dict[str, Any]) -> int:
        return int(t.get("width") or 0) * int(t.get("height") or 0)

    return max(thumbnails, key=_area).get("url")


async def _download_entry_media(
    session: aiohttp.ClientSession, entry: dict[str, Any], out_dir: Path, title: str, idx: int
) -> Optional[InstagramItem]:
    headers = dict(entry.get("http_headers") or {}) or {"Referer": "https://www.instagram.com/"}
    headers.setdefault("User-Agent", _UA)

    video_fmt = _best_video_format(entry)
    if video_fmt:
        media_url = video_fmt["url"]
        kind = "video"
        ext = ".mp4"
    else:
        media_url = _best_photo_url(entry)
        kind = "image"
        ext = ".jpg"

    if not media_url:
        logger.warning("Instagram item %d had neither a video format nor a photo URL.", idx)
        return None

    dest = out_dir / f"{idx:02d}_{utils.sanitize_filename(title, max_length=60)}{ext}"
    async with session.get(media_url, headers=headers) as resp:
        if resp.status != 200:
            raise InstagramError(f"Downloading Instagram media failed (HTTP {resp.status}).")
        data = await resp.read()

    if not data:
        raise InstagramError("Downloaded Instagram media file is empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    if kind == "image":
        # Validate + correct the extension against the real file content,
        # never trust the URL/content-type blindly (same safeguard used in
        # pinterest_service.py).
        real_kind = utils.sniff_kind(dest)
        if real_kind != "image":
            raise InstagramError(
                f"Downloaded file is not a valid image (detected type: {real_kind}). "
                "Instagram may have returned a login-wall/placeholder instead of the real photo."
            )
        real_ext = utils.sniff_image_extension(dest) or ext
        if dest.suffix.lower() != real_ext:
            fixed = dest.with_suffix(real_ext)
            dest.replace(fixed)
            dest = fixed

    return InstagramItem(index=idx, kind=kind, filepath=str(dest))


async def _download_via_ytdlp(url: str, out_dir: Path) -> Optional[InstagramResult]:
    """
    Root cause of "Instagram photo posts fail to download" (found by
    reading yt-dlp's own Instagram extractor source,
    ``yt_dlp/extractor/instagram.py::_extract_product_media``):

    yt-dlp's Instagram extractor builds an entry's ``formats`` list *only*
    from ``video_versions``. A pure photo entry (single photo post, or a
    photo slide inside a carousel) has no ``video_versions`` at all, so
    ``formats`` ends up completely EMPTY for it — even though the real,
    original-resolution photo URL (``image_versions2.candidates``) is
    present in Instagram's raw response, yt-dlp only exposes it as a
    "thumbnail", never as a downloadable format. The previous implementation
    called ``extract_info(url, download=True)`` with an explicit
    video-oriented ``format`` selector; for any entry with an empty
    ``formats`` list this always failed (or was silently skipped), which is
    exactly the reported bug: video/Reel posts (which *do* populate
    ``formats``) worked, plain photo posts and photo carousel items did not.

    Fix: use yt-dlp *only* for metadata extraction (``download=False`` —
    it still correctly handles Instagram's internal API + session cookies
    + carousel structure), then download every item's real bytes ourselves:
    videos from the best `formats` entry, photos from the largest
    `thumbnails` entry (the real trick yt-dlp itself doesn't do for
    photos). This fixes single photos, photo carousels, and keeps
    video/Reel downloads working exactly as before.
    """
    loop = asyncio.get_running_loop()

    def _run() -> Optional[dict]:
        opts = _base_opts()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            logger.info("Instagram yt-dlp metadata extraction failed, will try HTML fallback: %s", exc)
            return None

    info = await loop.run_in_executor(None, _run)
    if not info:
        return None

    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    entries = [e for e in entries if e]
    if not entries:
        return None

    title = (info.get("title") or entries[0].get("title") or "instagram_post").strip()
    items: list[InstagramItem] = []

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for idx, entry in enumerate(entries, start=1):
            try:
                item = await _download_entry_media(session, entry, out_dir, title, idx)
            except InstagramError as exc:
                logger.warning("Failed to download Instagram item %d/%d: %s", idx, len(entries), exc)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error downloading Instagram item %d/%d", idx, len(entries))
                continue
            if item:
                items.append(item)

    if not items:
        return None
    return InstagramResult(title=title, items=items)


def _clean_url(raw: str) -> str:
    return raw.replace("\\/", "/").replace("\\u0026", "&")


def _extract_ordered_media(html: str) -> list[tuple[str, str]]:
    """Return an ordered list of ``(kind, url)`` tuples for every media node
    found in the page JSON, correctly pairing each ``display_url`` with its
    own ``is_video``/``video_url`` fields (instead of naively assuming *all*
    items are videos whenever a single video is present, which used to break
    mixed image+video carousels)."""
    items: list[tuple[str, str]] = []
    matches = list(_DISPLAY_URL_RE.finditer(html))
    for i, match in enumerate(matches):
        display_url = _clean_url(match.group(1))
        window_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), match.end() + 2000)
        window = html[match.end():window_end]
        is_video_match = _IS_VIDEO_RE.search(window)
        is_video = bool(is_video_match and is_video_match.group(1) == "true")
        if is_video:
            video_match = _VIDEO_URL_RE.search(window)
            if video_match:
                items.append(("video", _clean_url(video_match.group(1))))
                continue
        items.append(("image", display_url))
    return items


async def _download_via_html(url: str, out_dir: Path) -> InstagramResult:
    """Fallback for public posts: scrape display_url/video_url pairs from
    the page HTML, preserving carousel order."""
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise InstagramError(f"Instagram page is not reachable (status {resp.status}).")
            html = await resp.text(errors="ignore")

        title = (_find_meta_content(html, _OG_TITLE_PROP) or "instagram_post").strip()

        media_urls = _extract_ordered_media(html)
        if not media_urls:
            # Single video post without a sidecar (e.g. a Reel) only exposes
            # a top-level video_url, or as a last resort the og:image.
            video_urls = [_clean_url(u) for u in _VIDEO_URL_RE.findall(html)]
            if video_urls:
                media_urls = [("video", video_urls[0])]
            else:
                og_image = _find_meta_content(html, _OG_IMAGE_PROP)
                if og_image:
                    media_urls = [("image", og_image)]

        if not media_urls:
            raise InstagramError("No downloadable media was found on this Instagram page.")

        items: list[InstagramItem] = []
        for idx, (kind, media_url) in enumerate(media_urls, start=1):
            async with session.get(media_url) as mresp:
                if mresp.status != 200:
                    continue
                data = await mresp.read()
                content_type = mresp.headers.get("Content-Type", "")
            ext = ".mp4" if kind == "video" else (".png" if "png" in content_type else ".jpg")
            filename = f"{idx:02d}_{utils.sanitize_filename(title, max_length=60)}{ext}"
            path = out_dir / filename
            path.write_bytes(data)
            items.append(InstagramItem(index=idx, kind=kind, filepath=str(path)))

    if not items:
        raise InstagramError("Failed to download any media from this Instagram post.")
    return InstagramResult(title=title, items=items)


async def download_post(url: str, out_dir: str) -> InstagramResult:
    """Download an Instagram post (image, video or carousel) preserving
    original quality and item order."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    result = await _download_via_ytdlp(url, out_path)
    if result:
        return result

    return await _download_via_html(url, out_path)
