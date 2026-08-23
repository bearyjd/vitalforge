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
    silently overwrite it."""
    sync = import_service_module("vitalforge-dashboard.sync")
    lock = asyncio.Lock()
    called = asyncio.Event()
    held_during_call = []

    async def fake_run_sync(days):
        held_during_call.append(lock.locked())
        called.set()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    task = asyncio.create_task(sync.scheduled_sync(lock))
    try:
        await asyncio.wait_for(called.wait(), timeout=1.0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert held_during_call == [True]
    assert not lock.locked()
