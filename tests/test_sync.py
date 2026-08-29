"""B5: `sync_weight_history()` reading Garmin's composition fields
(bodyWater/boneMass/muscleMass) off `latestWeight` into `weight_history`.
"""

import asyncio
from contextlib import suppress

from shared.database import get_db
from tests.conftest import import_service_module


async def test_sync_populates_composition_from_weigh_ins_fixture(initialized_db, fake_garmin_client):
    from shared.database import get_primary_person_id

    sync = import_service_module("vitalforge-dashboard.sync")

    person_id = await get_primary_person_id()
    await sync.sync_weight_history("2020-05-01", "2020-06-30", person_id)

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


async def test_scheduled_sync_serializes_against_shared_lock(initialized_db, monkeypatch):
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

    async def fake_run_sync(days, *, person_id):
        seen.append((days, lock.locked()))
        if len(seen) >= 2:
            second_call.set()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    task = asyncio.create_task(sync.scheduled_sync(lock, sync.SyncRegistry()))
    try:
        await asyncio.wait_for(second_call.wait(), timeout=5.0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert seen == [(90, True), (3, True)]
    assert not lock.locked()


async def test_run_sync_preserves_backoff_until(initialized_db, fake_garmin_client):
    """run_sync must not clear sync_status.backoff_until (spec §e, the Garmin
    429 backoff). INSERT OR REPLACE deletes and reinserts the row, so every
    column the statement omits silently reverts to its default -- which would
    drop an active backoff on every sync and turn a rate limit into a ban."""
    from shared.database import get_primary_person_id

    sync = import_service_module("vitalforge-dashboard.sync")
    person_id = await get_primary_person_id()

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sync_status (person_id, backoff_until) VALUES (?, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET backoff_until = excluded.backoff_until",
            (person_id, "2099-01-01T00:00:00+00:00"),
        )
        await db.commit()
    finally:
        await db.close()

    await sync.run_sync(days=1, person_id=person_id)

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT backoff_until, last_sync_result FROM sync_status WHERE person_id = ?",
            (person_id,),
        )
        row = await cur.fetchone()
        assert row["backoff_until"] == "2099-01-01T00:00:00+00:00", (
            "run_sync cleared an active backoff"
        )
        assert row["last_sync_result"] is not None, "run_sync did not record its own result"
    finally:
        await db.close()
