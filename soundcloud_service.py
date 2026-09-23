"""
soundcloud_service.py
-----------------------
Direct SoundCloud track/playlist (Sets) downloader using yt-dlp.

SoundCloud has no DRM, so yt-dlp can download the real audio
directly; this module makes sure that:
 - The final file is saved using the track title as its name.
 - The track cover art is embedded/sent alongside the file.
 - Metadata (title/artist) is present both in ID3 tags and captions.
 - Playlists (sets) are supported.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yt_dlp

import utils

logger = logging.getLogger(__name__)


class SoundCloudError(Exception):
    pass


@dataclass
class SoundCloudTrackInfo:
    id: str
    title: str
    uploader: str
    url: str
    thumbnail: Optional[str]
    duration: Optional[float]


@dataclass
class SoundCloudPlaylistInfo:
    title: str
    tracks: list[SoundCloudTrackInfo] = field(default_factory=list)


@dataclass
class SoundCloudDownloadResult:
    filepath: str
    cover_path: Optional[str]
    title: str
    artist: str
    duration: Optional[float]
    filesize: int


def _base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
        "geo_bypass": True,
    }


async def extract_playlist(url: str, max_tracks: int) -> SoundCloudPlaylistInfo:
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        opts = _base_opts()
        opts.update({"extract_flat": "in_playlist", "playlistend": max_tracks})
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, _extract)
    if not info:
        raise SoundCloudError("No information could be found for this playlist.")

    entries = list(info.get("entries") or [])
    tracks = []
    for entry in entries[:max_tracks]:
        if not entry:
            continue
        tracks.append(
            SoundCloudTrackInfo(
                id=str(entry.get("id") or ""),
                title=(entry.get("title") or "Untitled").strip(),
                uploader=(entry.get("uploader") or entry.get("channel") or "Unknown").strip(),
                url=entry.get("url") or entry.get("webpage_url") or url,
                thumbnail=entry.get("thumbnail"),
                duration=entry.get("duration"),
            )
        )
    return SoundCloudPlaylistInfo(title=(info.get("title") or "SoundCloud Playlist").strip(), tracks=tracks)


async def _download_cover(url: Optional[str], out_dir: Path) -> Optional[str]:
    if not url:
        return None
    try:
        dest = out_dir / "cover.jpg"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        dest.write_bytes(data)
        return str(dest)
    except Exception:  # noqa: BLE001
        return None


def _embed_tags(mp3_path: str, title: str, artist: str, cover_path: Optional[str]) -> None:
    try:
        from mutagen.id3 import APIC, ID3, TIT2, TPE1
        from mutagen.mp3 import MP3

        audio = MP3(mp3_path)
        try:
            audio.add_tags()
        except Exception:
            pass
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        if cover_path and Path(cover_path).exists():
            with open(cover_path, "rb") as img:
                audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img.read()))
        audio.save(v2_version=3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding ID3 tags failed for %s: %s", mp3_path, exc)


async def download_track(url: str, out_dir: str) -> SoundCloudDownloadResult:
    """دانلود یک آهنگ ساندکلاود به‌صورت mp3 با متادیتا و کاور کامل."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    tmp_template = str(out_path / "src.%(ext)s")

    def _download() -> dict:
        opts = _base_opts()
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": tmp_template,
                "restrictfilenames": True,
                "noplaylist": True,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                ],
            }
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = await loop.run_in_executor(None, _download)
    except Exception as exc:  # noqa: BLE001
        raise SoundCloudError(f"Failed to download the SoundCloud track: {exc}") from exc

    if not info:
        raise SoundCloudError("Failed to download the SoundCloud track.")

    src_files = [f for f in out_path.glob("src.*") if f.suffix.lower() == ".mp3"]
    if not src_files:
        raise SoundCloudError("The downloaded audio file could not be found on disk.")

    title = (info.get("title") or "Untitled").strip()
    artist = (info.get("uploader") or info.get("channel") or "Unknown").strip()
    cover_path = await _download_cover(info.get("thumbnail"), out_path)

    final_name = utils.build_track_filename(title, ext="mp3")
    final_path = out_path / final_name
    src_files[0].replace(final_path)

    _embed_tags(str(final_path), title, artist, cover_path)

    return SoundCloudDownloadResult(
        filepath=str(final_path),
        cover_path=cover_path,
        title=title,
        artist=artist,
        duration=info.get("duration"),
        filesize=final_path.stat().st_size,
    )
