# ---------------------------------------------------------------------------
# Vid-to-Link Telegram Downloader Bot
# Python 3.11 + FFmpeg/ffprobe + Deno (PO-Token provider) + aiohttp file server
# Optimized for Railway's Free Plan: small image, fast build, low RAM/CPU.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

WORKDIR /app

# Install FFmpeg (with ffprobe), git, curl, and Deno (JavaScript runtime
# required by YouTube Proof-of-Origin token challenge for high-res formats).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

ENV PATH="/usr/local/bin:${PATH}"

# PO Token provider setup (bgutil-ytdlp-pot-provider) running
# in "script" mode via Deno — keeps this a single-container deployment.
ARG BGUTIL_POT_VERSION=1.3.1
RUN git clone --depth 1 --single-branch --branch "${BGUTIL_POT_VERSION}" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider/server \
    && (deno install --allow-scripts=npm:canvas --frozen \
        && echo "bgutil PO Token provider (deno) ready." \
        || echo "Warning: PO Token provider setup failed; continuing with fallback clients.")

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version

# Application code
COPY . .

# Temporary download directory for downloaded files before expiration
RUN mkdir -p /tmp/downloads

ENV DOWNLOAD_PATH=/tmp/downloads
ENV PORT=8080

EXPOSE 8080

# Starts the Telegram bot, HTTP direct download server, and expiration worker
CMD ["python", "main.py"]
