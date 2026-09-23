"""
messages.py
-----------
All user-facing text lives here as Python templates.
Keeping strings unified in one module makes localization or styling easy.

Policy:
- User-facing text is in Persian (Farsi) for a native experience.
- Admin-facing menus (manager bot) can be technical / English or Persian.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CUSTOMIZABLE_KEYS: tuple[str, ...] = (
    "pornhub_link_received",
    "pornhub_warning",
    "pornhub_before_download",
    "pornhub_download_started",
    "pornhub_error",
    "pornhub_admin_notify",
    "welcome",
    "help",
    "download_ready",
    "error_generic",
    "error_download_failed",
    "cooldown_active",
    "cooldown_finished",
)

_OVERRIDES: dict[str, str] = {}

MESSAGES: dict[str, str] = {
    # -----------------------------------------------------------------------
    # Basic commands & greetings
    # -----------------------------------------------------------------------
    "welcome": (
        "👋 سلام {name} عزیز!\n\n"
        "به **ربات Vid-to-Link** خوش آمدید 🎬⚡️\n\n"
        "این ربات ویدیوها و رسانه‌ها را از سراسر اینترنت دانلود کرده و یک **لینک دانلود مستقیم و پرسرعت** در اختیارتان می‌گذارد تا بدون محدودیت‌های تلگرام در مرورگر یا با دانلود منیجر (IDM و...) دریافت کنید.\n\n"
        "✨ **ویژگی‌های کلیدی:**\n"
        "• 🚀 تولید لینک مستقیم HTTP (بدون صفحات واسط یا تبلیغات)\n"
        "• 📦 پشتیبانی از فایل‌های حجیم و بالای ۲ گیگابایت\n"
        "• ⚡️ پشتیبانی کامل از توقف و ادامه دانلود (Resume / Range Request)\n"
        "• ⏳ نگهداری و پاکسازی خودکار فایل‌ها پس از انقضا\n\n"
        "برای شروع کافیست **لینک ویدیو** را ارسال کنید یا دستور /help را بزنید."
    ),
    "help": (
        "📖 **راهنمای استفاده از ربات Vid-to-Link**\n\n"
        "1️⃣ لینک ویدیو، موزیک یا پست مورد نظرتان را برای ربات بفرستید.\n"
        "2️⃣ پلتفرم به‌طور خودکار شناسایی و تحلیل می‌شود.\n"
        "3️⃣ برای ویدیوهای یوتیوب و عمومی، کیفیت دلخواه را انتخاب کنید.\n"
        "4️⃣ پس از اتمام دانلود، یک **لینک دانلود مستقیم اختصاصی** همراه با زمان انقضا دریافت می‌کنید.\n"
        "5️⃣ روی لینک کلیک کرده یا آن را در دانلود منیجر وارد کنید تا فایل با نهایت سرعت دانلود شود.\n\n"
        "**دستورات اصلی:**\n"
        "/start — شروع مجدد ربات\n"
        "/help — نمایش همین راهنما\n"
        "/platforms — لیست سایت‌ها و پلتفرم‌های پشتیبانی‌شده\n"
        "/status — وضعیت درخواست فعلی\n"
        "/cancel — لغو عملیات فعلی\n"
        "/settings — مشاهده محدودیت‌ها و تنظیمات فعلی\n"
        "/about — درباره ربات"
    ),
    "about_text": (
        "ℹ️ **درباره Vid-to-Link**\n\n"
        "نسخه: `{version}`\n"
        "این ربات رسانه‌های تصویری و صوتی را از یوتیوب، اینستاگرام، تیک‌تاک، توییتر/ایکس، پینترست، ساندکلاود و صدها سایت دیگر استخراج کرده و لینک مستقیم HTTP با قابلیت Resume ارائه می‌دهد.\n\n"
        "💡 هیچ فایلی از طریق تلگرام آپلود نمی‌شود، بنابراین فایل‌های بالای ۲ گیگابایت بدون کوچک‌ترین مشکلی دانلود می‌شوند."
    ),
    "banned_user": "⛔️ دسترسی شما به این ربات توسط مدیریت مسدود شده است.",
    "url_received": "🔎 لینک دریافت شد، در حال بررسی...",
    "invalid_url": (
        "❌ لینک ارسالی معتبر نیست.\n"
        "لطفاً یک لینک اینترنتی صحیح با http:// یا https:// بفرستید."
    ),
    "unsupported_platform": "⚠️ این پلتفرم در حال حاضر پشتیبانی نمی‌شود.",
    "extracting_info": "⏳ در حال دریافت اطلاعات ویدیو و لیست کیفیت‌ها...",
    "video_detected": "🎬 **{title}**\n\nلطفاً کیفیت مورد نظر خود را انتخاب کنید:",
    "quality_selection": "🎬 **{title}**\n⏱ مدت: {duration}\n\nکیفیت مورد نظر را برای دریافت لینک انتخاب کنید:",
    "no_formats_found": "❌ فرمت یا کیفیت قابل دانلودی برای این لینک پیدا نشد.",
    # -----------------------------------------------------------------------
    # Download progress & delivery
    # -----------------------------------------------------------------------
    "download_started": "⬇️ در حال دانلود ویدیو از منبع...",
    "download_progress": (
        "⬇️ **در حال دانلود از منبع...**\n\n"
        "{bar}  {percent}%\n\n"
        "📦 حجم دریافت شده: {downloaded} از {total}\n"
        "🚀 سرعت دانلود: {speed}\n"
        "⏱ زمان باقی‌مانده: {eta}"
    ),
    "processing": "⚙️ در حال پردازش فایل و ساخت لینک دانلود مستقیم...",
    "download_ready": (
        "🎉 **لینک دانلود مستقیم آماده شد!**\n\n"
        "🎬 **عنوان:** {title}\n"
        "🎚 **کیفیت:** {quality}\n"
        "📦 **حجم فایل:** {size}\n"
        "⏳ **مدت اعتبار لینک:** {expires_in} (تا {expires_at})\n\n"
        "🔗 **لینک دانلود مستقیم:**\n"
        "{download_url}\n\n"
        "💡 _روی لینک بالا کلیک کنید تا در مرورگر دانلود شود، یا آن را در دانلود منیجر (IDM، ADM، curl) کپی کنید. قابلیت Resume فعال است._"
    ),
    "link_details": (
        "📋 **مشخصات فایل دانلودی**\n\n"
        "📄 **نام فایل:** `{filename}`\n"
        "📦 **حجم:** {size}\n"
        "⏳ **زمان باقی‌مانده تا حذف:** {time_left}\n"
        "🕒 **تاریخ انقضا:** {expires_at}\n"
        "📥 **تعداد دفعات دانلود شده:** {download_count} بار"
    ),
    "link_expired_info": "⚠️ این لینک منقضی شده و فایل از حافظه سرور حذف شده است.",
    # -----------------------------------------------------------------------
    # Errors & cancellations
    # -----------------------------------------------------------------------
    "error_generic": "❌ خطایی رخ داد:\n`{error}`",
    "error_extraction_failed": "❌ دریافت اطلاعات ویدیو ناموفق بود:\n`{error}`",
    "error_download_failed": "❌ دانلود ویدیو با خطا مواجه شد:\n`{error}`",
    "error_file_too_large": (
        "⚠️ حجم فایل ({size}) از حداکثر مجاز سرور ({max_size}) بیشتر است."
    ),
    "error_rate_limit": "⏱ شما به سقف مجاز درخواست‌ها رسیده‌اید. لطفاً {seconds} ثانیه دیگر دوباره امتحان کنید.",
    "error_already_processing": "⏳ یک درخواست از طرف شما در حال حاضر در حال پردازش است. لطفاً تا اتمام آن صبور باشید یا از /cancel برای لغو استفاده کنید.",
    "cancel_success": "🛑 عملیات با موفقیت لغو شد.",
    "cancel_nothing": "ℹ️ در حال حاضر عملیات فعالی برای شما وجود ندارد.",
    "status_idle": "🟢 هیچ درخواستی در حال پردازش نیست. می‌توانید لینک جدیدی ارسال کنید.",
    "status_active": "🟡 در حال حاضر در مرحله **{stage}** برای لینک زیر هستید:\n`{url}`",
    # -----------------------------------------------------------------------
    # Menus
    # -----------------------------------------------------------------------
    "menu_download": "📥 دانلود لینک جدید",
    "menu_platforms": "🌐 سایت‌های پشتیبانی‌شده",
    "menu_help": "📖 راهنما",
    "menu_settings": "⚙️ تنظیمات و محدودیت‌ها",
    "menu_about": "ℹ️ درباره ربات",
    "menu_download_prompt": "لطفاً لینک ویدیو یا رسانه مورد نظرتان را در چت ارسال کنید.",
    "settings_text": (
        "⚙️ **تنظیمات و محدودیت‌های ربات**\n\n"
        "📦 حداکثر حجم فایل: {max_size}\n"
        "⏳ مدت اعتبار پیش‌فرض فایل‌ها: {expiration}\n"
        "🔀 حداکثر دانلود همزمان سرور: {max_concurrent}\n"
        "⏱ نرخ درخواست: حداکثر {rate_count} درخواست در {rate_window} ثانیه"
    ),
    "platforms_button": "🌐 لیست پلتفرم‌ها",
    "platforms_title": "📥 **پلتفرم‌های پشتیبانی‌شده**\n\n",
    "platforms_category": "\n{category}:\n",
    "platforms_item": "  {icon} {name}\n",
    "platforms_footer": (
        "\n💡 علاوه‌بر این‌ها، ربات از **بیش از ۱۸۰۰ سایت مختلف** (توییتر، یوتیوب، تیک‌تاک، فیسبوک، ردیت، پینترست و...) با موتور پیشرفته پشتیبانی می‌کند."
    ),
    # -----------------------------------------------------------------------
    # Pinterest
    # -----------------------------------------------------------------------
    "pinterest_downloading": "📌 در حال استخراج پین از پینترست...",
    "pinterest_error": "❌ خطا در دانلود از پینترست:\n`{error}`",
    "pinterest_carousel_found": "📌 این پین شامل **{count} رسانه** (اسلایدشو) است.",
    "pinterest_carousel_ask_format": "📌 این پین حاوی {count} آیتم است. فایل‌ها به‌صورت یک فایل فشرده ZIP آماده می‌شوند.",
    "pinterest_success_single": "🎉 پین با موفقیت دانلود و آماده شد!",
    "pinterest_success_carousel": "🎉 پین اسلایدشویی ({count} آیتم) در قالب فایل ZIP آماده شد!",
    "pinterest_zip_building": "📦 در حال بسته‌بندی {count} آیتم در قالب فایل ZIP...",
    "pinterest_carousel_expired": "⏱ مهلت پاسخگویی به این انتخاب منقضی شد.",
    # -----------------------------------------------------------------------
    # Instagram
    # -----------------------------------------------------------------------
    "instagram_downloading": "📷 در حال استخراج و دانلود از اینستاگرام...",
    "instagram_carousel_found": "📷 پست کاروسل ({count} آیتم) شناسایی شد. در حال تجمیع...",
    "instagram_success_single": "🎉 پست اینستاگرام با موفقیت آماده شد!",
    "instagram_success_carousel": "🎉 پست کاروسل اینستاگرام ({count} آیتم) در قالب ZIP آماده شد!",
    "instagram_zip_building": "📦 در حال ساخت فایل ZIP برای {count} آیتم کاروسل...",
    "instagram_error": "❌ خطا در استخراج اینستاگرام:\n`{error}`",
    # -----------------------------------------------------------------------
    # SoundCloud
    # -----------------------------------------------------------------------
    "soundcloud_downloading": "🎵 در حال استخراج و دانلود موزیک از ساندکلاود...",
    "soundcloud_track_success": "🎉 قطعه «{title}» دانلود شد!",
    "soundcloud_track_error": "❌ خطا در دانلود ساندکلاود:\n`{error}`",
    "soundcloud_playlist_found": "☁️ پلی‌لیست «{name}» شامل {count} قطعه شناسایی شد.",
    "soundcloud_playlist_progress": "🎵 در حال دانلود ({index}/{total}): «{title}»...",
    "soundcloud_playlist_track_failed": "⚠️ دانلود ترک {index}/{total} («{title}») ناموفق بود: {error}",
    "soundcloud_zip_building": "📦 در حال ساخت فایل ZIP از {count} قطعه پلی‌لیست...",
    "soundcloud_playlist_finished": "🎉 پلی‌لیست با موفقیت دانلود و لینک ZIP آماده شد ({success} از {total} قطعه).",
    # -----------------------------------------------------------------------
    # Twitter / X
    # -----------------------------------------------------------------------
    "twitter_downloading": "🐦 در حال استخراج رسانه از توییتر/ایکس...",
    "twitter_carousel_found": "🐦 توییت حاوی {count} تصویر شناسایی شد.",
    "twitter_success_single": "🎉 رسانه توییتر با موفقیت آماده شد!",
    "twitter_success_carousel": "🎉 تصاویر توییت ({count} تصویر) در قالب ZIP آماده شد!",
    "twitter_zip_building": "📦 در حال ساخت فایل ZIP برای {count} تصویر توییت...",
    "twitter_error": "❌ خطا در استخراج رسانه از توییتر:\n`{error}`",
    # -----------------------------------------------------------------------
    # PornHub
    # -----------------------------------------------------------------------
    "pornhub_link_received": "🔞 لینک PornHub دریافت شد. در حال استخراج کیفیت‌ها...",
    "pornhub_warning": "⚠️ محتوای این لینک دارای محدودیت سنی (+18) است.",
    "pornhub_before_download": "🔞 در حال آماده‌سازی دانلود ویدیوی PornHub...",
    "pornhub_download_started": "⬇️ در حال دانلود ویدیوی PornHub در کیفیت انتخابی...",
    "pornhub_error": "❌ خطا در دانلود ویدیوی PornHub:\n`{error}`",
    "pornhub_admin_notify": "🔔 یک ویدیوی PornHub توسط کاربر `{user_id}` دانلود شد: {title}",
    # -----------------------------------------------------------------------
    # Cooldown
    # -----------------------------------------------------------------------
    "cooldown_active": (
        "🧊 **محدودیت موقت (Cooldown)**\n\n"
        "شما به سقف تعداد دانلود متوالی رسیده‌اید.\n"
        "لطفاً {seconds} ثانیه منتظر بمانید تا سهمیه بعدی شما فعال شود."
    ),
    "cooldown_finished": "✅ زمان استراحت به پایان رسید! اکنون می‌توانید دوباره لینک بفرستید.",
}


def get(key: str, **kwargs: Any) -> str:
    """Retrieve and format a message template."""
    template = _OVERRIDES.get(key) or MESSAGES.get(key, f"[{key}]")
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception as exc:
        logger.warning("Failed to format message '%s': %s", key, exc)
        return template
