"""B5: `sync_weight_history()` reading Garmin's composition fields
(bodyWater/boneMass/muscleMass) off `latestWeight` into `weight_history`.
"""

import asyncio
from contextlib import suppress

from shared.database import get_db
from tests.conftest import import_service_module


async def test_sync_populates_composition_from_weigh_ins_fixture(initialized_db, fake_garmin_client):
    sync = import_service_module("vitalforge-dashboard.sync")

    await sync.sync_weight_history("2020-05-01", "2020-06-30")

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT weight_grams, bmi, body_fat, body_water, bone_mass_g, muscle_mass_g "
                "FROM weight_history WHERE date = ?",
                ("2020-06-01",),
            )
        ).fetchone()
    finally:
        await db.close()

    assert row is not None
    assert row["weight_grams"] == 81200
    assert row["bmi"] == 24.1
    assert row["body_fat"] == 18.4
    assert row["body_water"] == 55.2
    assert row["bone_mass_g"] == 3200
    assert row["muscle_mass_g"] == 34000


async def test_scheduled_sync_serializes_against_shared_lock(monkeypatch):
    """Phase 4 adversarial review finding: the background scheduler used to
    call run_sync() without acquiring the same lock /api/sync's manual
    trigger holds (see vitalforge-dashboard/app.py's _sync_lock), so a
    manual sync and the initial 90-day backfill could interleave -- and
    since every write goes through upsert()'s last-writer-wins
    INSERT OR REPLACE, an older pull finishing after a newer one could
    silently overwrite it.

    Covers BOTH `async with lock:` sites in scheduled_sync -- the initial
    backfill and the periodic loop iteration -- not just the first. An
    earlier version of this test only ever observed the backfill call (the
    loop's `asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)` never completed
    before the test cancelled the task), so deleting the loop's `async
    with lock:` would have left this test green (Phase 4 fix-review
    finding). Setting SYNC_INTERVAL_HOURS to 0 collapses that sleep to
    effectively-zero, letting a second call happen inside the test's
    timeout."""
    sync = import_service_module("vitalforge-dashboard.sync")
    monkeypatch.setattr(sync, "SYNC_INTERVAL_HOURS", 0)
    lock = asyncio.Lock()
    seen = []
    second_call = asyncio.Event()

    async def fake_run_sync(days):
        seen.append((days, lock.locked()))
        if len(seen) >= 2:
            second_call.set()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    task = asyncio.create_task(sync.scheduled_sync(lock))
    try:
        await asyncio.wait_for(second_call.wait(), timeout=5.0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert seen == [(90, True), (3, True)]
    assert not lock.locked()
