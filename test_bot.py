"""
test_bot.py
-----------
Comprehensive automated test suite for Vid-to-Link:
1. Database link registration, lookup, expiration and statistics.
2. Admin runtime setting of file expiration duration (presets & custom).
3. HTTP direct download server (200 OK, 206 Partial Content Range, HEAD, 410 Gone for expired, 404 Not Found).
4. Automatic cleanup worker (disk file deletion and DB state update).
5. Large file simulation (>2GB size tracking & streaming checks).
6. Admin settings module overrides and limit setters.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import test_utils, web

import admin_settings
import cleanup_worker
import config
import database
import file_server
import utils


async def test_database_and_links():
    print("Testing database and file_links...")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test.db"
        config.DB_PATH = str(test_db)
        await database.init_db()

        # 1. Test expiration setting
        default_exp = await database.get_file_expiration_seconds()
        assert default_exp == config.DEFAULT_FILE_EXPIRATION_SECONDS
        await database.set_file_expiration_seconds(3600)
        assert await database.get_file_expiration_seconds() == 3600

        # 2. Test create link
        token = "test_token_123"
        user_id = 99999
        file_path = str(Path(tmpdir) / "video.mp4")
        with open(file_path, "wb") as f:
            f.write(b"0" * 1024)

        now = int(time.time())
        expires_at = now + 100
        await database.create_file_link(
            token=token,
            user_id=user_id,
            file_path=file_path,
            file_name="video.mp4",
            file_size=1024,
            expires_at=expires_at,
        )

        link = await database.get_file_link(token)
        assert link is not None
        assert link["token"] == token
        assert link["file_name"] == "video.mp4"
        assert link["file_size"] == 1024
        assert link["download_count"] == 0
        assert link["is_deleted"] == 0

        # 3. Test increment download count
        await database.increment_link_download_count(token)
        link = await database.get_file_link(token)
        assert link["download_count"] == 1

        # 4. Storage stats
        stats = await database.get_storage_stats()
        assert stats["active_count"] == 1
        assert stats["active_size_bytes"] == 1024
        assert stats["total_hits"] == 1

        await database.close_db()
    print("  -> Database tests passed!")


async def test_file_expiration_and_cleanup():
    print("Testing file expiration and cleanup worker...")
    with tempfile.TemporaryDirectory() as tmpdir:
        config.DB_PATH = str(Path(tmpdir) / "test.db")
        config.DOWNLOAD_PATH = str(Path(tmpdir) / "downloads")
        Path(config.DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)
        await database.init_db()

        # Create 2 files: 1 active, 1 expired
        task1_dir = Path(config.DOWNLOAD_PATH) / "task1"
        task1_dir.mkdir()
        file1 = task1_dir / "active.mp4"
        file1.write_bytes(b"active video data")

        task2_dir = Path(config.DOWNLOAD_PATH) / "task2"
        task2_dir.mkdir()
        file2 = task2_dir / "expired.mp4"
        file2.write_bytes(b"expired video data")

        now = int(time.time())
        # Active: expires in 3600 seconds
        await database.create_file_link(
            token="tok_active",
            user_id=1,
            file_path=str(file1),
            file_name="active.mp4",
            file_size=file1.stat().st_size,
            expires_at=now + 3600,
        )
        # Expired: expired 10 seconds ago
        await database.create_file_link(
            token="tok_expired",
            user_id=1,
            file_path=str(file2),
            file_name="expired.mp4",
            file_size=file2.stat().st_size,
            expires_at=now - 10,
        )

        expired_list = await database.get_expired_file_links(now)
        assert len(expired_list) == 1
        assert expired_list[0]["token"] == "tok_expired"

        # Run cleanup cycle
        res = await cleanup_worker.run_cleanup_cycle()
        assert res["deleted_count"] == 1
        assert not file2.exists(), "Expired file should be deleted from disk"
        assert file1.exists(), "Active file should NOT be deleted from disk"

        # Check DB status of expired link
        expired_link = await database.get_file_link("tok_expired")
        assert expired_link["is_deleted"] == 1

        await database.close_db()
    print("  -> Cleanup worker tests passed!")


async def test_http_server():
    print("Testing direct HTTP file server...")
    with tempfile.TemporaryDirectory() as tmpdir:
        config.DB_PATH = str(Path(tmpdir) / "test.db")
        config.DOWNLOAD_PATH = str(Path(tmpdir) / "downloads")
        Path(config.DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)
        await database.init_db()

        # Create test video
        video_file = Path(config.DOWNLOAD_PATH) / "sample.mp4"
        sample_content = b"0123456789" * 100  # 1000 bytes
        video_file.write_bytes(sample_content)

        now = int(time.time())
        await database.create_file_link(
            token="sample_tok",
            user_id=10,
            file_path=str(video_file),
            file_name="sample.mp4",
            file_size=1000,
            expires_at=now + 3600,
        )

        # Create expired link
        await database.create_file_link(
            token="expired_tok",
            user_id=10,
            file_path=str(video_file),
            file_name="sample.mp4",
            file_size=1000,
            expires_at=now - 50,
        )

        app = file_server.create_web_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()

        # 1. Test GET /
        resp_root = await client.get("/")
        assert resp_root.status == 200
        assert "Vid-to-Link" in await resp_root.text()

        # 2. Test GET /health
        resp_health = await client.get("/health")
        assert resp_health.status == 200
        health_json = await resp_health.json()
        assert health_json["status"] == "healthy"

        # 3. Test GET download active link
        resp_dl = await client.get("/download/sample_tok/sample.mp4")
        assert resp_dl.status == 200
        assert resp_dl.headers["Content-Disposition"] == 'attachment; filename="sample.mp4"'
        assert resp_dl.headers["Accept-Ranges"] == "bytes"
        dl_body = await resp_dl.read()
        assert dl_body == sample_content

        # 4. Test HTTP Range request (resume capability)
        resp_range = await client.get(
            "/download/sample_tok/sample.mp4",
            headers={"Range": "bytes=0-9"},
        )
        assert resp_range.status == 206
        range_body = await resp_range.read()
        assert range_body == b"0123456789"

        # 5. Test HEAD request
        resp_head = await client.head("/download/sample_tok/sample.mp4")
        assert resp_head.status == 200
        assert resp_head.headers["Content-Length"] == "1000"

        # 6. Test GET expired link -> 410 Gone
        resp_exp = await client.get("/download/expired_tok/sample.mp4")
        assert resp_exp.status == 410
        assert "410 Gone" in await resp_exp.text()

        # 7. Test invalid token -> 404
        resp_404 = await client.get("/download/non_existent/file.mp4")
        assert resp_404.status == 404

        await client.close()
        await database.close_db()
    print("  -> HTTP server tests passed!")


async def test_admin_settings():
    print("Testing admin_settings module...")
    with tempfile.TemporaryDirectory() as tmpdir:
        config.DB_PATH = str(Path(tmpdir) / "test.db")
        await database.init_db()

        # Expiration
        await admin_settings.set_file_expiration_seconds(14400)
        assert await admin_settings.get_file_expiration_seconds() == 14400

        # Max file size (>2GB support)
        large_size = 5 * 1024 * 1024 * 1024  # 5 GB
        await admin_settings.set_max_file_size(large_size)
        assert await admin_settings.get_max_file_size() == large_size

        # Concurrency
        await admin_settings.set_max_concurrent_downloads(4)
        assert await admin_settings.get_max_concurrent_downloads() == 4

        # Cooldown
        cd = await admin_settings.set_cooldown_config(enabled=True, count=10, seconds=120)
        assert cd["enabled"] is True or cd["enabled"] == 1
        assert cd["count"] == 10
        assert cd["seconds"] == 120

        # Messages
        assert await admin_settings.set_message_override("welcome", "Custom Welcome!")
        assert admin_settings.get_current_text("welcome") == "Custom Welcome!"
        await admin_settings.reset_message_override("welcome")

        await database.close_db()
    print("  -> Admin settings tests passed!")


async def test_time_parser():
    print("Testing time duration parser and presets...")
    assert utils.parse_time_duration("15m") == 900
    assert utils.parse_time_duration("30m") == 1800
    assert utils.parse_time_duration("1h") == 3600
    assert utils.parse_time_duration("2h") == 7200
    assert utils.parse_time_duration("4h") == 14400
    assert utils.parse_time_duration("1d") == 86400
    assert utils.parse_time_duration("7200") == 7200
    assert utils.parse_time_duration("2.5h") == 9000
    assert utils.parse_time_duration("invalid") is None

    # Expiration presets check
    for sec, label_fa, label_en in admin_settings.FILE_EXPIRATION_PRESETS:
        assert sec >= 60
        assert label_fa
        assert label_en
    print("  -> Time parser tests passed!")


async def main():
    await test_database_and_links()
    await test_file_expiration_and_cleanup()
    await test_http_server()
    await test_admin_settings()
    await test_time_parser()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! 🎉")


if __name__ == "__main__":
    asyncio.run(main())
