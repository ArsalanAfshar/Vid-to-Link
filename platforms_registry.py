"""
platforms_registry.py
----------------------
Single source of truth for every platform NexiLink knows about.

This registry is used for two things:
 1. Dynamically building the "📥 Supported Platforms" message.
 2. Routing a URL to the right specialised service (Pinterest, Instagram,
    SoundCloud, PornHub) before falling back to the generic yt-dlp engine
    that transparently supports hundreds of additional sites.

Adding support for a brand-new site that needs *no* special handling is a
one-line change here (new `PlatformInfo` entry with `handler="generic"`);
no other file needs to be touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import config


@dataclass(frozen=True)
class PlatformInfo:
    key: str
    name: str
    category: str
    domains: tuple[str, ...]
    icon: str = "🔗"
    handler: str = "generic"  # generic | pinterest | instagram | soundcloud | pornhub
    enabled: bool = True


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _matches(url: str, domains: tuple[str, ...]) -> bool:
    host = _hostname(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _pornhub_enabled() -> bool:
    return config.PORNHUB_ENABLED


def _pinterest_enabled() -> bool:
    return config.PINTEREST_ENABLED


def _instagram_enabled() -> bool:
    return config.INSTAGRAM_ENABLED


def _twitter_enabled() -> bool:
    return config.TWITTER_ENABLED


REGISTRY: list[PlatformInfo] = [
    PlatformInfo(
        key="youtube",
        name="YouTube (video & shorts)",
        category="🎬 ویدیو",
        domains=("youtube.com", "youtu.be", "music.youtube.com"),
        icon="▶️",
    ),
    PlatformInfo(
        key="tiktok",
        name="TikTok",
        category="🎬 ویدیو",
        domains=("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
        icon="🎵",
    ),
    PlatformInfo(
        key="facebook",
        name="Facebook",
        category="🎬 ویدیو",
        domains=("facebook.com", "fb.watch", "m.facebook.com"),
        icon="📘",
    ),
    PlatformInfo(
        key="twitter",
        name="Twitter / X (عکس، ویدیو و گیف)",
        category="📱 شبکه اجتماعی",
        domains=("twitter.com", "x.com", "t.co"),
        icon="🐦",
        handler="twitter",
        enabled=_twitter_enabled(),
    ),
    PlatformInfo(
        key="vimeo",
        name="Vimeo",
        category="🎬 ویدیو",
        domains=("vimeo.com",),
        icon="🎞️",
    ),
    PlatformInfo(
        key="reddit",
        name="Reddit",
        category="🎬 ویدیو",
        domains=("reddit.com", "v.redd.it"),
        icon="👽",
    ),
    PlatformInfo(
        key="threads",
        name="Threads",
        category="📱 شبکه اجتماعی",
        domains=("threads.net",),
        icon="🧵",
    ),
    PlatformInfo(
        key="instagram",
        name="Instagram (photo / video / carousel)",
        category="📱 شبکه اجتماعی",
        domains=("instagram.com",),
        icon="📷",
        handler="instagram",
        enabled=_instagram_enabled(),
    ),
    PlatformInfo(
        key="pinterest",
        name="Pinterest (photo & video pins)",
        category="📱 شبکه اجتماعی",
        domains=("pinterest.com", "pin.it", "pinterest.co.uk", "pinterest.ca"),
        icon="📌",
        handler="pinterest",
        enabled=_pinterest_enabled(),
    ),
    PlatformInfo(
        key="soundcloud",
        name="SoundCloud (tracks & playlists)",
        category="🎵 موسیقی",
        domains=("soundcloud.com", "snd.sc", "on.soundcloud.com"),
        icon="☁️",
        handler="soundcloud",
    ),
    PlatformInfo(
        key="pornhub",
        name="PornHub",
        category="🔞 بزرگسال (اختیاری)",
        domains=("pornhub.com", "pornhub.net", "pornhubpremium.com"),
        icon="🔞",
        handler="pornhub",
        enabled=_pornhub_enabled(),
    ),
]


def all_platforms(enabled_only: bool = True) -> list[PlatformInfo]:
    if not enabled_only:
        return list(REGISTRY)
    return [p for p in REGISTRY if p.enabled]


def detect_platform(url: str) -> str:
    """Return the routing handler key ('pinterest', 'instagram',
    'soundcloud', 'pornhub') or 'generic' as a fallback for the
    yt-dlp based engine (which supports 1800+ sites on its own)."""
    for platform in REGISTRY:
        if not platform.enabled:
            continue
        if _matches(url, platform.domains):
            return platform.handler
    return "generic"


def find_platform_name(url: str) -> str | None:
    for platform in REGISTRY:
        if _matches(url, platform.domains):
            return platform.name
    return None


def build_platforms_text() -> str:
    """Dynamically render the supported-platforms list grouped by category."""
    import messages

    grouped: dict[str, list[PlatformInfo]] = {}
    for platform in all_platforms(enabled_only=True):
        grouped.setdefault(platform.category, []).append(platform)

    text = messages.get("platforms_title")
    for category, items in grouped.items():
        text += messages.get("platforms_category", category=category)
        for item in items:
            text += messages.get("platforms_item", icon=item.icon, name=item.name)
    text += messages.get("platforms_footer")
    return text
