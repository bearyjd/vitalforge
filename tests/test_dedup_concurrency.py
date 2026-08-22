"""B4 concurrency tests -- the gap that let F1 through the first time.

Every dedup test in test_dedup.py issues one POST at a time, which is
exactly what let the original (non-atomic) dedup design ship broken: two
concurrent identical POSTs both saw "no duplicate" and both wrote. These
tests race real concurrent requests against the atomic (BEGIN IMMEDIATE)
implementation.

Unlike tests/test_migration.py's concurrency tests, both requests here are
expected to *succeed* (one waits for the other's short transaction, neither
errors), so `asyncio.gather` is used directly -- the interpreter-cleanup
hang documented in test_migration.py was specific to a *failing* aiosqlite
operation, which doesn't happen on this path.
"""

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared import database
from shared.database import get_db


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def seed_row(weight_grams: int, seconds_ago: float = 0) -> int:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    weight_kg = weight_grams / 1000.0
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
            "VALUES (?, ?, ?, ?, 1)",
            (round(weight_kg * 2.20462, 2), round(weight_kg, 2), weight_grams, ts),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def row_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM weight_log")).fetchone())[0]
    finally:
        await db.close()


async def test_two_concurrent_identical_posts_store_one_row(client):
    results = await asyncio.gather(
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"}),
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"}),
    )
    assert all(r.status_code == 200 for r in results)
    assert await row_count() == 1


async def test_two_concurrent_identical_posts_push_to_garmin_once(client, fake_garmin_client):
    await asyncio.gather(
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"}),
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"}),
    )
    assert len(fake_garmin_client.pushed_weights) == 1


async def test_two_concurrent_distinct_posts_both_stored(client):
    results = await asyncio.gather(
        client.post("/api/weight", json={"weight": 150.0, "unit": "lbs"}),
        client.post("/api/weight", json={"weight": 200.0, "unit": "lbs"}),
    )
    assert all(r.status_code == 200 for r in results)
    assert await row_count() == 2


async def test_two_concurrent_enrichment_posts_update_once_and_push_once(client, fake_garmin_client):
    """The gap fix-verification found: two concurrent composition-bearing
    POSTs against one stored row must serialize -- the second observes the
    now-non-NULL columns and falls through to collapse, not a second
    enrichment race."""
    await seed_row(84096, seconds_ago=5)

    results = await asyncio.gather(
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4}),
        client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4}),
    )
    assert all(r.status_code == 200 for r in results)
    assert len(fake_garmin_client.pushed_weights) == 1
    assert await row_count() == 1

    db = await get_db()
    try:
        row = await (await db.execute("SELECT body_fat_pct FROM weight_log")).fetchone()
    finally:
        await db.close()
    assert row["body_fat_pct"] == 18.4


async def test_concurrent_writer_not_blocked_by_garmin_push(client, weight_app_module, monkeypatch):
    """Proves the push is genuinely outside the transaction: a sync.py-style
    writer on a completely separate connection/thread must succeed quickly
    while a (mocked, slow) Garmin push is in flight, not wait for it."""
    push_started = threading.Event()

    def slow_push(weight_grams, timestamp=None, **kwargs):
        push_started.set()
        time.sleep(1.0)

    monkeypatch.setattr(weight_app_module, "push_weight", slow_push)

    db_path = str(database.DB_PATH)
    writer_succeeded = threading.Event()
    write_duration = {}

    def concurrent_writer():
        assert push_started.wait(timeout=5), "push never started"
        start = time.monotonic()
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            conn.execute("INSERT OR REPLACE INTO steps (date, value) VALUES (?, ?)", ("2026-01-01", 9000))
            conn.commit()
        finally:
            conn.close()
        write_duration["seconds"] = time.monotonic() - start
        writer_succeeded.set()

    writer_thread = threading.Thread(target=concurrent_writer)
    writer_thread.start()

    resp = await client.post("/api/weight", json={"weight": 180.0, "unit": "lbs"})
    assert resp.status_code == 200

    writer_thread.join(timeout=5)
    assert writer_succeeded.is_set(), "concurrent writer was blocked by the in-flight Garmin push"
    assert write_duration["seconds"] < 0.5, (
        f"writer took {write_duration['seconds']:.2f}s -- looks like it waited for the push, "
        "meaning the write lock is still held during the Garmin call"
    )
