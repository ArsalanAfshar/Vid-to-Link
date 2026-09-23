# Vid-to-Link: Telegram Video Downloader with Direct HTTP Links

A high-performance Telegram media downloader bot optimized for **Railway's Free Tier**. Vid-to-Link downloads videos and media from **over 1,800 platforms** (YouTube, TikTok, Instagram, Twitter/X, Pinterest, SoundCloud, Facebook, Reddit, Vimeo, and more) and delivers **genuine direct HTTP download links** rather than uploading files through Telegram.

This completely bypasses Telegram's 2 GB bot upload limit, supports **files larger than 2 GB without issue**, enables **resumable downloads (HTTP Range requests)** in web browsers and download managers (IDM, ADM, curl, wget), and features an **admin-configurable file expiration and automated background disk cleanup system**.

---

## 🌟 Key Features

1. **Direct HTTP File Delivery (Zero Telegram Upload Limit):**
   - Media is served directly over HTTP via an integrated asynchronous `aiohttp.web` server.
   - Bypasses Telegram's 2GB file upload restriction completely.
   - Files open directly in any web browser or download manager (IDM, wget, curl) — no landing pages, no redirects, no ads, no countdowns.
2. **Large File (>2GB) & Low RAM Architecture:**
   - Designed specifically for Railway's Free Tier resource constraints (ephemeral disk, 512MB RAM).
   - Video download streams directly to disk chunks.
   - Direct HTTP delivery uses OS-level zero-copy `sendfile` and chunked asynchronous streaming (`aiohttp.web.FileResponse`), consuming less than **50–100 MB of RAM** even when serving multi-gigabyte files.
   - Full HTTP Range support (`206 Partial Content`) allowing pause, resume, and multi-connection downloads.
3. **Admin-Configurable File Expiration & Automated Cleanup:**
   - Downloaded files remain accessible for a configurable retention window (default: 2 hours).
   - The bot owner can change the expiration duration at runtime directly from Telegram (`/expiration`) via interactive buttons (15m, 30m, 1h, 2h, 4h, 6h, 12h, 24h, 48h) or custom input (e.g. `90m`, `3h`, `1d`) without code changes or redeploying.
   - Continuous automated background worker sweeps disk every 60 seconds, deleting expired files and reclaiming container disk space.
   - Owner can inspect real-time Railway disk usage and trigger on-demand cleanup sweeps (`/storage`).
4. **Rich Multi-Platform Support:**
   - YouTube (video & audio DASH streams with Proof-of-Origin token provider support).
   - Instagram (single posts, reels, and multi-item carousels bundled into ZIP archives).
   - Pinterest (photo and video pins via direct PinResource API).
   - Twitter / X (videos, GIFs, and multi-photo tweets).
   - SoundCloud (tracks with ID3 tags and sets/playlists bundled into ZIP archives).
   - PornHub (isolated workflow with admin alerts).
   - Generic fallback engine supporting 1,800+ sites via `yt-dlp`.
5. **Concurrency & Anti-Spam Protection:**
   - Concurrency limiter (`MAX_CONCURRENT_DOWNLOADS`) protects Railway container resources.
   - Per-user rate limiting and cooldown system.

---

## 🏛 Architecture & Technical Approach

### 1. Single-Container Unified Async Process
Railway's free plan allows one container service. Rather than complicating the stack with separate Nginx, Celery, or Redis services, **Vid-to-Link runs the Telegram MTProto client (`Telethon`), the HTTP file server (`aiohttp.web`), and the background cleanup worker in the SAME asyncio event loop**:
- **Port Binding:** Binds to `0.0.0.0:$PORT` (Railway automatically sets `$PORT`).
- **Domain Auto-Discovery:** Vid-to-Link reads `RAILWAY_PUBLIC_DOMAIN` or `RAILWAY_STATIC_URL` automatically to construct public HTTPS URLs like `https://<service-name>.up.railway.app/download/<token>/<filename>`.

### 2. File Delivery & Streaming Mechanism
- When a download completes, the bot generates a cryptographically secure token (`secrets.token_urlsafe(16)`), computes `expires_at = now + expiration_seconds`, and registers the link in SQLite.
- The user receives: `https://<DOMAIN>/download/<token>/<filename>`
- When requested via `GET /download/<token>/<filename>`:
  - Database verifies token validity and expiration.
  - If expired, returns `410 Gone`.
  - If active, returns `aiohttp.web.FileResponse(file_path)` with headers:
    - `Content-Disposition: attachment; filename="<filename>"`
    - `Accept-Ranges: bytes`
    - `Cache-Control: no-cache, no-store, must-revalidate`
- **Zero In-Memory Buffering:** `FileResponse` streams using OS sendfile or 256KB chunks. Files of 2GB, 5GB, or 10GB never load into memory.

### 3. Expiration & Storage Lifecycle
- Download files are placed in `DOWNLOAD_PATH / {task_id}`.
- `cleanup_worker.py` runs every 60 seconds:
  - Queries `SELECT * FROM file_links WHERE expires_at <= ? AND is_deleted = 0`.
  - Removes physical files and empty task directories from disk.
  - Marks link as `is_deleted = 1` in SQLite.
  - Secondary sweep purges orphaned `.part` or `.tmp` files older than 2 hours.
- Startup sweep clears any expired files left from prior container restarts.

---

## 🔄 Reference Repository Comparison (NexiLink vs. Vid-to-Link)

Vid-to-Link adapts and refines the core strengths of [NexiLink-Downloader-Telegram-Bot](https://github.com/ArsalanAfshar/NexiLink-Downloader-Telegram-Bot) while replacing the delivery model:

| Component | Status | Reasoning |
|---|---|---|
| **Telegram File Upload (`send_file`, `fast_upload.py`)** | **DROPPED** | Replaced entirely with direct HTTP link generation. Eliminates Telegram's 2GB file size limit and bandwidth bottlenecks. |
| **Media Attributes (`media_attrs.py`)** | **DROPPED** | Telethon MTProto document attributes (`DocumentAttributeVideo`, etc.) are only needed for Telegram file uploads. Video validation stream checking moved to `utils.py`. |
| **Inline Mode (`inline_mode.py`)** | **DROPPED** | Out-of-scope for browser-based direct link downloads; dropped to keep codebase focused and lightweight. |
| **User Session Strings (`generate_session.py`)** | **DROPPED** | User accounts were previously used to get 4GB Telegram Premium upload limits. Direct links make user accounts obsolete; standard Bot Token is all that's required. |
| **HTTP File Server (`file_server.py`)** | **ADDED** | Integrated `aiohttp.web` server delivering streaming downloads with HTTP Range support (RFC 7233) for resuming and download managers. |
| **Cleanup Worker (`cleanup_worker.py`)** | **ADDED** | Automated periodic background worker enforcing admin-configured file retention policies and freeing container storage. |
| **Dynamic Expiration Setting (`admin_settings.py`, `manager_bot.py`)** | **ADDED** | Owner can configure file expiration (15m, 30m, 1h, 2h, 4h, etc. or custom input) directly from Telegram at runtime. |
| **Storage Monitor & Manual Sweep (`/storage`)** | **ADDED** | Allows owner to view real-time Railway disk usage and run manual cleanup sweeps with one click. |
| **`downloader.py` (yt-dlp Engine)** | **REUSED & ADAPTED** | Preserved format ladder, PO-Token provider integration, audio extraction, cookie merging, and thumbnail handling. Removed 2GB upload constraints to support multi-gigabyte files. |
| **Platform Services (Instagram, Pinterest, Twitter, SoundCloud, PornHub)** | **REUSED & ADAPTED** | Retained extractor logic. Multi-item carousels and playlists are bundled into ZIP archives and delivered via direct download links. |
| **Database Layer (`database.py`)** | **REUSED & EXTENDED** | Extended with `file_links` table, expiration tracking, link hit counting, and storage statistics. |
| **Concurrency & Cooldown** | **REUSED** | Essential for Railway Free Plan's 512MB RAM and shared CPU constraints. |

---

## ⚙️ Environment Variables

Copy `.env.example` to `.env` or set these in Railway Variables:

### Required Variables
| Variable | Description | Example |
|---|---|---|
| `API_ID` | Telegram API ID (from https://my.telegram.org) | `12345678` |
| `API_HASH` | Telegram API Hash (from https://my.telegram.org) | `abcdef0123456789...` |
| `BOT_TOKEN` | Bot Token from [@BotFather](https://t.me/BotFather) | `123456789:ABCdef...` |
| `OWNER_ID` | Numeric Telegram ID of the bot owner | `987654321` |

### Railway Networking & Storage Variables
| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Port for the HTTP file server. Automatically set by Railway. |
| `BASE_URL` | Auto-detected | Public domain of the service. Defaults to Railway's public domain (`https://${RAILWAY_PUBLIC_DOMAIN}`). |
| `FILE_EXPIRATION_SECONDS` | `7200` (2 hours) | Default duration downloaded files remain available before automatic deletion. Configurable at runtime. |
| `MAX_FILE_SIZE` | `10737418240` (10 GB) | Max allowed file size in bytes. Set to `0` for unlimited (disk-bound). |
| `DOWNLOAD_PATH` | `/tmp/downloads` | Temporary download storage directory. |
| `DB_PATH` | `bot_data.db` | SQLite database file location. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous downloads limit to protect Railway CPU/RAM. |

### Optional Variables
| Variable | Default | Description |
|---|---|---|
| `MANAGER_BOT_TOKEN` | `""` | Optional second bot token for dedicated admin management. (Owner can also use `/admin` directly in the main bot). |
| `RATE_LIMIT_COUNT` | `5` | Request limit per window. |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds. |
| `COOLDOWN_ENABLED` | `false` | Enable cooldown periods after consecutive downloads. |
| `PORNHUB_ENABLED` | `true` | Toggle PornHub support. |
| `PORNHUB_NOTIFY_ADMIN`| `true` | Notify owner when adult content is downloaded. |
| `YOUTUBE_COOKIES` | `""` | Netscape cookies string for YouTube bypass. |
| `INSTAGRAM_COOKIES` | `""` | Netscape cookies string for Instagram. |

---

## 🚀 Deployment Instructions for Railway (Free Plan)

### Step 1: Fork or Push Repository to GitHub
Push your Vid-to-Link repository to your GitHub account:
```bash
git push origin arena/01a0ce0c-vid-to-link
```

### Step 2: Create a New Project on Railway
1. Go to [Railway.app](https://railway.app/) and log in (GitHub login is free).
2. Click **New Project** → **Deploy from GitHub repo**.
3. Select your `Vid-to-Link` repository.

### Step 3: Configure Environment Variables
In your Railway dashboard, click on your service and navigate to the **Variables** tab. Add:
- `API_ID`: Your Telegram API ID.
- `API_HASH`: Your Telegram API Hash.
- `BOT_TOKEN`: The token given by [@BotFather](https://t.me/BotFather).
- `OWNER_ID`: Your numerical Telegram user ID (obtain from [@userinfobot](https://t.me/userinfobot)).
- `MAX_FILE_SIZE`: `10737418240` (10 GB) or `0` for unlimited.
- `FILE_EXPIRATION_SECONDS`: `7200` (2 hours default).

### Step 4: Generate a Public Domain (Critical)
To allow browsers to download files from your Railway container:
1. In Railway, click on your service → navigate to **Settings**.
2. Scroll to **Networking** → **Public Networking**.
3. Click **Generate Domain**.
4. Railway will assign an HTTPS domain (e.g. `vid-to-link-production.up.railway.app`).
5. Vid-to-Link automatically detects this domain and generates public download links!

### Step 5: (Optional) Persistent Volume
On Railway's Free Plan, the ephemeral container disk provides several gigabytes of temporary space which is automatically kept clean by Vid-to-Link's expiration worker. If you wish to preserve the SQLite database (`bot_data.db`) across new Git commits/deployments:
1. In Railway, click **+ New** → **Volume**.
2. Mount the volume at `/app/data`.
3. Set the environment variable `DB_PATH=/app/data/bot_data.db`.

---

## 🛠 Admin Controls

The bot owner (`OWNER_ID`) has full management capabilities either through the optional Manager Bot or **directly in the main bot**:

- `/expiration` — Interactive inline menu to select file validity presets (15 min, 30 min, 1h, 2h, 4h, 6h, 12h, 24h, 48h) or send custom duration (e.g. `90m`, `3h`, `1d`). Takes effect immediately without redeploy.
- `/storage` — View container disk stats (Total, Used, Free), count of active stored files, and trigger an immediate on-demand cleanup sweep (`[ 🧹 Run Cleanup Now ]`).
- `/limits` — Adjust maximum file size (500MB up to 20GB or Unlimited), max concurrent downloads, and cooldown.
- `/stats` — Total users, download requests, success/fail ratios, and active download link hits.
- `/broadcast <message>` — Send announcements to all registered bot users.

---

## 🧪 Local Testing

To run the automated test suite locally:
```bash
python3 test_bot.py
```

To run the application locally:
```bash
cp .env.example .env
# Edit .env with your credentials
python3 main.py
```
Open `http://localhost:8080` in your web browser to verify the HTTP server is running.
