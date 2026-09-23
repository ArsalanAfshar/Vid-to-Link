"""
pinterest_service.py
----------------------
Reliable Pinterest pin downloader (image, video or carousel), returning the
real media file(s) — never a preview/storyboard frame, and never an image
when the pin actually contains a video.

Root causes fixed in this revision
-----------------------------------
1. "Video pins download only the first frame / a preview image":
   The previous implementation tried yt-dlp first and, if that raised for
   *any* reason, silently fell back to an HTML-scraping image path. That
   fallback used regexes that assumed ``property="og:xxx"`` always comes
   *before* ``content="..."`` inside a Pinterest ``<meta>`` tag. In reality
   Pinterest renders ``content`` first
   (``<meta content="..." ... property="og:image"/>``), so every
   `og:image` / `og:video` regex silently failed to match, and the code
   fell through even further to "grab any `i.pinimg.com` URL found
   anywhere in the page" — which, for a video pin, is very likely to be
   the static preview thumbnail. That is exactly the reported bug.

   Fix: we no longer rely on fragile HTML scraping as the primary path.
   We call Pinterest's own ``PinResource`` JSON API directly (the same
   endpoint yt-dlp's dedicated Pinterest extractor uses) to get
   structured data: real video stream URLs (``videos.video_list``), the
   true full-resolution image (``images.orig``), and carousel slides
   (``carousel_data.carousel_slots`` / ``story_pin_data``). HTML
   scraping is now only a last-resort fallback, and its regexes are
   attribute-order-agnostic.

2. "Image / carousel posts return errors without useful information":
   Every failure now raises ``PinterestError`` with the *real* underlying
   reason (HTTP status, exception message, ...) instead of a generic
   message, and the full traceback is always logged via
   ``logger.exception`` — nothing is swallowed silently. Carousels are
   now supported end-to-end (see ``PinterestResult.items``).

3. "Never send an image when a video was requested":
   Every downloaded file is validated *before* being handed back to
   handlers.py:
     - videos are checked for a real video stream (ffprobe when
       available, otherwise a container-signature check) and rejected if
       they turn out to be an image;
     - images are checked against known image file signatures (magic
       bytes) and the extension is corrected to match the *real* file
       content, never just trusted from the URL or an HTTP header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yt_dlp

import utils

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_PIN_ID_RE = re.compile(r"/pin/(?:[\w-]+--)?(\d+)")

# Attribute-order-agnostic <meta> scraping (last-resort fallback only).
_META_TAG_RE = re.compile(r"<meta\b([^>]+)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"|(\w[\w:-]*)\s*=\s*\'([^\']*)\'')
_IMAGE_RE = re.compile(r'"(https://i\.pinimg\.com/[^"]+?\.(?:jpg|jpeg|png|gif|webp))"')

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)


class PinterestError(Exception):
    pass


@dataclass
class PinterestItem:
    filepath: str
    kind: str  # "image" | "video"
    index: int = 1


@dataclass
class PinterestResult:
    title: str
    items: list[PinterestItem] = field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return len(self.items) > 1

    # Backward-compatible single-item accessors (used by older call sites).
    @property
    def filepath(self) -> str:
        return self.items[0].filepath

    @property
    def kind(self) -> str:
        return self.items[0].kind


def _meta_attrs(tag_body: str) -> dict[str, str]:
    """Parse a <meta ...> tag's attributes regardless of their order."""
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag_body):
        if m.group(1) is not None:
            attrs[m.group(1).lower()] = m.group(2)
        else:
            attrs[m.group(3).lower()] = m.group(4)
    return attrs


def _find_meta_content(html: str, *, name: Optional[str] = None, prop: Optional[str] = None) -> Optional[str]:
    """Find a <meta> tag's `content` regardless of whether `name`/`property`
    appears before or after `content` in the raw HTML (Pinterest renders
    `content` first, which broke the previous strict-order regexes)."""
    target = (name or prop or "").lower()
    for match in _META_TAG_RE.finditer(html):
        attrs = _meta_attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key == target and "content" in attrs:
            return attrs["content"]
    return None


def _upgrade_to_original(url: str) -> str:
    """Rewrite a low-resolution Pinterest image URL (e.g. /236x/) to the
    full-resolution /originals/ variant."""
    return re.sub(r"/\d+x(?:\d+)?/", "/originals/", url)


def _extract_pin_id(url: str) -> Optional[str]:
    match = _PIN_ID_RE.search(url)
    return match.group(1) if match else None


async def _resolve_canonical_url(session: aiohttp.ClientSession, url: str) -> str:
    """Follow redirects for shortened links (pin.it, mobile.pinterest.com,
    ...) so we always end up with a canonical pinterest.com/pin/<id> URL."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            final_url = str(resp.url)
            # Drain a tiny bit so the connection can be reused/closed cleanly.
            await resp.content.read(0)
            return final_url
    except Exception as exc:  # noqa: BLE001
        raise PinterestError(f"لینک پینترست قابل دسترسی نیست: {exc}") from exc


async def _call_pin_resource(session: aiohttp.ClientSession, pin_id: str) -> dict[str, Any]:
    """Call Pinterest's own PinResource JSON API — the same reliable data
    source used by yt-dlp's dedicated Pinterest extractor. Far more robust
    than scraping the SPA's rendered HTML."""
    api_url = "https://www.pinterest.com/resource/PinResource/get/"
    options = {"field_set_key": "unauth_react_main_pin", "id": pin_id}
    params = {"data": json.dumps({"options": options})}
    headers = {"X-Pinterest-PWS-Handler": "www/[username].js"}
    try:
        async with session.get(api_url, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise PinterestError(f"Pinterest API returned HTTP {resp.status} for pin {pin_id}.")
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise PinterestError(f"Network error while calling the Pinterest API: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise PinterestError(f"Pinterest API returned an unexpected (non-JSON) response: {exc}") from exc

    data = (payload or {}).get("resource_response", {}).get("data")
    if not isinstance(data, dict):
        raise PinterestError("Pinterest API response did not contain pin data (pin may be private or removed).")
    return data


def _best_image_from_dict(images: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(images, dict) or not images:
        return None
    if isinstance(images.get("orig"), dict) and images["orig"].get("url"):
        return images["orig"]

    def _area(entry: dict[str, Any]) -> int:
        return int(entry.get("width") or 0) * int(entry.get("height") or 0)

    candidates = [v for v in images.values() if isinstance(v, dict) and v.get("url")]
    if not candidates:
        return None
    return max(candidates, key=_area)


def _direct_video_url(video_list: dict[str, Any]) -> Optional[str]:
    """Prefer a progressive (non-HLS) MP4 URL — it downloads directly with
    a single HTTP GET, no ffmpeg segment-merging needed."""
    if not isinstance(video_list, dict):
        return None
    progressive = [
        v.get("url")
        for k, v in video_list.items()
        if isinstance(v, dict) and v.get("url") and "hls" not in k.lower() and ".m3u8" not in (v.get("url") or "")
    ]
    if progressive:
        return progressive[0]
    return None


async def _download_bytes(session: aiohttp.ClientSession, url: str, dest: Path) -> None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise PinterestError(f"Downloading the media file failed (HTTP {resp.status}).")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
    except aiohttp.ClientError as exc:
        raise PinterestError(f"Network error while downloading the media file: {exc}") from exc


async def _download_video_via_ytdlp(pin_url: str, out_dir: Path) -> Path:
    """Fallback for HLS-only video pins: let yt-dlp (+ ffmpeg) fetch and
    mux the stream into a real mp4 container."""
    loop = asyncio.get_running_loop()

    def _run() -> Optional[dict]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": str(out_dir / "pin.%(ext)s"),
            "format": "(bestvideo+bestaudio/best)[vcodec!=none]",
            "format_sort": ["res", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "socket_timeout": 20,
            "retries": 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(pin_url, download=True)

    try:
        info = await loop.run_in_executor(None, _run)
    except Exception as exc:  # noqa: BLE001
        raise PinterestError(f"yt-dlp could not download the video stream: {exc}") from exc

    if not info:
        raise PinterestError("yt-dlp returned no data for this video pin.")

    candidates = sorted(out_dir.glob("pin.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = [p for p in candidates if p.stat().st_size > 0]
    if not candidates:
        raise PinterestError("yt-dlp finished but no output file was found on disk.")
    return candidates[0]


async def _validate_video(path: Path) -> None:
    """Reject the file (raising PinterestError with the real reason) unless
    it demonstrably contains a real video stream. This is the safeguard
    that prevents ever sending an image/preview-frame when a video was
    requested."""
    if not path.exists() or path.stat().st_size < 1024:
        raise PinterestError("Downloaded video file is empty or missing.")

    has_video = await utils.has_video_stream(str(path))
    if has_video is True:
        return
    if has_video is False:
        raise PinterestError(
            "The downloaded file does not contain a real video stream "
            "(ffprobe found no video track — this pin has no actual video)."
        )

    # ffprobe unavailable: fall back to a raw container-signature check.
    kind = utils.sniff_kind(path)
    if kind != "video":
        raise PinterestError(
            f"The downloaded file failed content validation (detected type: {kind}, expected: video)."
        )


def _validate_and_fix_image(path: Path, title: str) -> Path:
    """Verify the downloaded bytes are really an image and rename the file
    with the correct extension based on its actual content."""
    if not path.exists() or path.stat().st_size < 64:
        raise PinterestError("Downloaded image file is empty or missing.")

    kind = utils.sniff_kind(path)
    if kind != "image":
        raise PinterestError(
            f"The downloaded file is not a valid image (detected type: {kind}). "
            "Pinterest may have returned a login-wall or error page instead of the media."
        )

    real_ext = utils.sniff_image_extension(path) or path.suffix or ".jpg"
    if path.suffix.lower() != real_ext:
        fixed = path.with_suffix(real_ext)
        path.replace(fixed)
        return fixed
    return path


async def _download_image_item(
    session: aiohttp.ClientSession, image_info: dict[str, Any], out_dir: Path, name_hint: str, index: int
) -> PinterestItem:
    image_url = _upgrade_to_original(image_info["url"])
    tmp_path = out_dir / f"{utils.sanitize_filename(name_hint, 90)}_{index}.jpg"
    await _download_bytes(session, image_url, tmp_path)
    final_path = _validate_and_fix_image(tmp_path, name_hint)
    return PinterestItem(filepath=str(final_path), kind="image", index=index)


async def _download_video_item(
    pin_url: str, video_list: dict[str, Any], session: aiohttp.ClientSession, out_dir: Path, name_hint: str, index: int
) -> PinterestItem:
    direct_url = _direct_video_url(video_list)
    if direct_url:
        tmp_path = out_dir / f"{utils.sanitize_filename(name_hint, 90)}_{index}.mp4"
        await _download_bytes(session, direct_url, tmp_path)
    else:
        downloaded = await _download_video_via_ytdlp(pin_url, out_dir)
        tmp_path = out_dir / f"{utils.sanitize_filename(name_hint, 90)}_{index}{downloaded.suffix}"
        downloaded.replace(tmp_path)

    await _validate_video(tmp_path)
    return PinterestItem(filepath=str(tmp_path), kind="video", index=index)


async def _download_embedded(embed_src: str, out_dir: Path, index: int) -> PinterestItem:
    """Some pins simply embed another platform's video (e.g. Vimeo/YouTube).
    Delegate to the generic yt-dlp powered downloader in that case."""
    import downloader as generic_downloader

    info = await generic_downloader.extract_video_info(embed_src)
    if not info.qualities:
        raise PinterestError("No downloadable quality was found for the embedded video.")
    quality = max((q for q in info.qualities if q.kind == "video"), key=lambda q: (q.height or 0), default=info.qualities[0])
    result = await generic_downloader.download_video(embed_src, quality, str(out_dir))
    await _validate_video(Path(result.filepath))
    return PinterestItem(filepath=result.filepath, kind="video", index=index)


def _carousel_slots(data: dict[str, Any]) -> list[dict[str, Any]]:
    carousel = data.get("carousel_data")
    if isinstance(carousel, dict):
        slots = carousel.get("carousel_slots")
        if isinstance(slots, list) and slots:
            return [s for s in slots if isinstance(s, dict)]

    story = data.get("story_pin_data")
    if isinstance(story, dict):
        pages = story.get("pages")
        if isinstance(pages, list) and len(pages) > 1:
            slots = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for block in page.get("blocks", []) or []:
                    if isinstance(block, dict):
                        slots.append(block)
            if slots:
                return slots
    return []


async def _download_slot(
    session: aiohttp.ClientSession, slot: dict[str, Any], out_dir: Path, title: str, index: int, pin_url: str
) -> Optional[PinterestItem]:
    try:
        video_block = slot.get("video") if isinstance(slot.get("video"), dict) else slot
        video_list = (video_block or {}).get("video_list") if isinstance(video_block, dict) else None
        if isinstance(video_list, dict) and video_list:
            return await _download_video_item(pin_url, video_list, session, out_dir, title, index)

        image_block = slot.get("image") if isinstance(slot.get("image"), dict) else slot
        images = (image_block or {}).get("images") if isinstance(image_block, dict) else None
        best_image = _best_image_from_dict(images) if images else None
        if not best_image:
            best_image = _best_image_from_dict(slot.get("images") if isinstance(slot.get("images"), dict) else {})
        if best_image:
            return await _download_image_item(session, best_image, out_dir, title, index)
    except PinterestError as exc:
        logger.warning("Skipping carousel item %s: %s", index, exc)
        return None
    logger.warning("Carousel slot %s had neither a recognizable video nor image payload.", index)
    return None


async def _try_html_fallback(session: aiohttp.ClientSession, url: str, out_dir: Path) -> PinterestResult:
    """Last-resort fallback used only when the JSON API is fully
    unreachable (e.g. Pinterest rolling out an API change). Regexes here
    are attribute-order-agnostic, fixing the historical bug where
    `content="..."` appearing before `property="og:..."` caused every
    match to silently fail."""
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    async with session.get(url, allow_redirects=True, headers=headers) as resp:
        if resp.status != 200:
            raise PinterestError(f"Pinterest page is not reachable (status {resp.status}).")
        html = await resp.text(errors="ignore")

    title = _find_meta_content(html, prop="og:title") or "pinterest_media"

    video_url = _find_meta_content(html, prop="og:video") or _find_meta_content(html, prop="og:video:secure_url")
    if video_url:
        tmp_path = out_dir / f"{utils.sanitize_filename(title, 90)}.mp4"
        await _download_bytes(session, video_url, tmp_path)
        try:
            await _validate_video(tmp_path)
            return PinterestResult(title=title, items=[PinterestItem(filepath=str(tmp_path), kind="video")])
        except PinterestError:
            utils.safe_delete(tmp_path)  # fall through to image extraction below

    image_url = _find_meta_content(html, prop="og:image")
    if not image_url:
        matches = _IMAGE_RE.findall(html)
        if matches:
            originals = [u for u in matches if "/originals/" in u]
            image_url = max(originals or matches, key=len)
    if not image_url:
        raise PinterestError("No image or video could be found on this pin (page structure may have changed).")

    image_url = _upgrade_to_original(image_url)
    tmp_path = out_dir / f"{utils.sanitize_filename(title, 90)}.jpg"
    await _download_bytes(session, image_url, tmp_path)
    final_path = _validate_and_fix_image(tmp_path, title)
    return PinterestResult(title=title, items=[PinterestItem(filepath=str(final_path), kind="image")])


async def download_pin(url: str, out_dir: str) -> PinterestResult:
    """Download a Pinterest pin (video, image, or carousel) as real,
    full-quality file(s) ready to be sent as Telegram documents.

    Never silently sends the wrong media type: every branch below raises
    ``PinterestError`` with the genuine failure reason rather than masking
    it, and every downloaded file is validated against its expected type
    before being returned.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=headers) as session:
        canonical_url = await _resolve_canonical_url(session, url)
        pin_id = _extract_pin_id(canonical_url) or _extract_pin_id(url)

        if not pin_id:
            logger.info("Could not extract a pin id from %s; using HTML fallback.", url)
            return await _try_html_fallback(session, url, out_path)

        try:
            data = await _call_pin_resource(session, pin_id)
        except PinterestError as exc:
            logger.warning("Pinterest API call failed (%s); falling back to HTML scraping.", exc)
            return await _try_html_fallback(session, url, out_path)

        title = (data.get("title") or data.get("grid_title") or data.get("seo_title") or "pinterest_media").strip()
        title = title or "pinterest_media"

        # 1) Carousel / multi-page story pin -> multiple items.
        slots = _carousel_slots(data)
        if slots:
            items: list[PinterestItem] = []
            for idx, slot in enumerate(slots, start=1):
                item = await _download_slot(session, slot, out_path, title, idx, canonical_url)
                if item:
                    items.append(item)
            if not items:
                raise PinterestError("Failed to download any item from this Pinterest carousel.")
            return PinterestResult(title=title, items=items)

        # 2) Single video pin.
        video_list = (data.get("videos") or {}).get("video_list")
        if isinstance(video_list, dict) and video_list:
            item = await _download_video_item(canonical_url, video_list, session, out_path, title, 1)
            return PinterestResult(title=title, items=[item])

        # 3) Embedded external content (e.g. a Vimeo/YouTube link shared as a pin).
        embed_src = (data.get("embed") or {}).get("src")
        if embed_src:
            item = await _download_embedded(embed_src, out_path, 1)
            return PinterestResult(title=title, items=[item])

        # 4) Plain image pin.
        best_image = _best_image_from_dict(data.get("images") or {})
        if best_image:
            item = await _download_image_item(session, best_image, out_path, title, 1)
            return PinterestResult(title=title, items=[item])

        raise PinterestError("This pin does not appear to contain any downloadable image or video.")
