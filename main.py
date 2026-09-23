"""
main.py
-------
Application entry point for Vid-to-Link.

Runs inside a single container on Railway (Free Tier):
 1. Validates environment variables.
 2. Initialises the SQLite database.
 3. Restores admin-customized settings and message overrides.
 4. Performs startup disk cleanup of expired files.
 5. Starts the integrated asynchronous direct HTTP file server (aiohttp.web).
 6. Starts the background automated file cleanup worker.
 7. Connects the main downloader bot (Telethon MTProto).
 8. Optionally connects the Manager Bot in the same process.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import admin_settings
import cleanup_worker
import config
import database
import file_server
from telegram_client import create_client

logger = logging.getLogger(__name__)


async def run() -> None:
    config.setup_logging()

    errors = config.validate_config()
    if errors:
        for err in errors:
            logger.error("Configuration error: %s", err)
        logger.error("Please set the required environment variables (see .env.example).")
        sys.exit(1)

    logger.info("Initializing database...")
    await database.init_db()

    logger.info("Loading dynamic admin overrides...")
    await admin_settings.load_overrides()

    # Startup cleanup: remove expired files left over from prior container runs
    await cleanup_worker.startup_cleanup()

    # Start the HTTP file server for direct downloads
    logger.info("Starting direct HTTP file server...")
    server_runner, site = await file_server.start_file_server()

    # Start background cleanup task
    logger.info("Starting background file expiration cleanup worker...")
    cleanup_task = asyncio.create_task(cleanup_worker.start_cleanup_worker(interval_seconds=60))

    # Import handlers late to prevent circular imports
    from handlers import register_handlers

    logger.info("Connecting main bot to Telegram...")
    main_client = await create_client(device_model=config.DEVICE_MODEL)
    register_handlers(main_client)

    me = await main_client.get_me()
    logger.info("Main bot connected: @%s (ID: %s)", getattr(me, "username", "?"), me.id)
    logger.info("Bot Owner ID: %s", config.OWNER_ID)
    base_url = config.get_base_url()
    logger.info("Base Download URL: %s", base_url)
    if "localhost" in base_url or "127.0.0.1" in base_url:
        logger.warning(
            "⚠️ Notice: Base Download URL is currently '%s'. "
            "To enable external public downloads on Railway, navigate to your Service -> Settings -> "
            "Public Networking -> 'Generate Domain' (or set BASE_URL in Railway Variables).",
            base_url,
        )

    tasks = [
        asyncio.create_task(main_client.run_until_disconnected()),
        cleanup_task,
    ]

    manager_client = None
    if config.MANAGER_ENABLED:
        from manager_bot import register_manager_handlers

        logger.info("Connecting Vid-to-Link Manager Bot...")
        manager_client = await create_client(
            bot_token=config.MANAGER_BOT_TOKEN,
            session_string="",
            device_model="Vid-to-Link Manager Bot",
        )
        register_manager_handlers(manager_client, main_client)
        manager_me = await manager_client.get_me()
        logger.info(
            "Manager bot connected: @%s (ID: %s)",
            getattr(manager_me, "username", "?"),
            manager_me.id,
        )
        tasks.append(asyncio.create_task(manager_client.run_until_disconnected()))
    else:
        logger.info("Manager Bot disabled (set MANAGER_BOT_TOKEN to enable it, or use /admin in main bot).")

    logger.info("Vid-to-Link is fully operational and waiting for requests.")

    try:
        await asyncio.gather(*tasks)
    finally:
        logger.info("Shutting down Vid-to-Link...")
        cleanup_task.cancel()
        await file_server.stop_file_server(server_runner)
        await database.close_db()
        logger.info("Vid-to-Link stopped.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
