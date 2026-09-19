"""B5: `sync_weight_history()` reading Garmin's composition fields
(bodyWater/boneMass/muscleMass) off `latestWeight` into `weight_history`.
"""

import asyncio
from contextlib import suppress

import pytest

from shared import garmin_registry
from shared.database import get_db, get_primary_person_id
from vitalforge_dashboard import sync


async def test_sync_populates_composition_from_weigh_ins_fixture(initialized_db, fake_garmin_client):
    from shared.database import get_primary_person_id

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
    trigger holds (see vitalforge_dashboard/app.py's _sync_lock), so a
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
    monkeypatch.setattr(sync, "SYNC_INTERVAL_HOURS", 0)
    lock = asyncio.Lock()
    seen = []
    second_call = asyncio.Event()

    async def fake_run_sync(days, *, person_id):
        seen.append((days, lock.locked()))
        if len(seen) >= 2:
            second_call.set()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    async def usable_garmin_link(_person_id: int) -> bool:
        return True

    monkeypatch.setattr(sync, "has_usable_garmin_link", usable_garmin_link)

    task = asyncio.create_task(sync.scheduled_sync(lock, sync.SyncRegistry()))
    try:
        await asyncio.wait_for(second_call.wait(), timeout=5.0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert seen == [(90, True), (3, True)]
    assert not lock.locked()


@pytest.mark.parametrize("state, usable", (("linked", True), ("legacy_bound", False)))
async def test_usable_garmin_link_accepts_only_linked(initialized_db, state, usable):
    """'legacy_bound' is a retired state an older release may have left
    behind; it is tolerated by the schema but never synced from."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at)
            VALUES (?, ?, 'owner@example.test', 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')
            """,
            (person_id, state),
        )
        await db.commit()
    finally:
        await db.close()

    assert await sync.has_usable_garmin_link(person_id) is usable


async def _sync_status(person_id: int):
    db = await get_db()
    try:
        return await (
            await db.execute("SELECT last_sync_result FROM sync_status WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()


async def _row_count(table: str, person_id: int) -> int:
    db = await get_db()
    try:
        row = await (
            await db.execute(f"SELECT COUNT(*) AS n FROM [{table}] WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    return row["n"]


@pytest.mark.parametrize("failure", ("registry_operation_error", "provider_exception"))
async def test_run_sync_skips_a_failed_metric_and_keeps_the_rest_of_the_date(
    initialized_db, fake_garmin_client, monkeypatch, caplog, failure
):
    """One failing metric read must not abort the whole date, and the sync
    result must still say something went wrong."""
    person_id = await get_primary_person_id()
    provider_text = "provider-response-text-that-must-not-be-logged"
    real_call_paced = fake_garmin_client.registry_call
    pending_failures = [
        garmin_registry.GarminOperationError("network")
        if failure == "registry_operation_error"
        else RuntimeError(provider_text)
    ]

    async def flaky_call_paced(person_id, operation):
        # The first read of the run is sleep; fail exactly that one.
        if pending_failures:
            raise pending_failures.pop()
        return await real_call_paced(person_id, operation)

    monkeypatch.setattr(sync.garmin_registry, "call_paced", flaky_call_paced)
    with caplog.at_level("WARNING", logger="vitalforge_dashboard.sync"):
        result = await sync.run_sync(days=1, person_id=person_id)

    assert result == "completed with 1 errors"
    assert await _row_count("sleep", person_id) == 0
    assert await _row_count("resting_hr", person_id) == 1
    assert await _row_count("weight_history", person_id) > 0
    assert (await _sync_status(person_id))["last_sync_result"] == "completed with 1 errors"
    assert "Skipping sleep" in caplog.text
    assert provider_text not in caplog.text


@pytest.mark.parametrize("code", ("auth_failed", "rate_limited", "network", "unknown"))
async def test_run_sync_stops_at_the_first_authentication_failure(initialized_db, monkeypatch, code):
    """A cold login that fails leaves no session, so every later read of the
    run would repeat the same failed login; stop after the first one and
    skip weight history.  The recorded result is the login's own bounded
    code, not a blanket ``auth_failed``: a transient block (a 403, a network
    error, a throttle) is retried at the next sync, and only a genuine
    credential rejection asks the person to relink."""
    person_id = await get_primary_person_id()
    calls: list[int] = []

    async def dead_link(person_id, operation):
        calls.append(person_id)
        raise garmin_registry.GarminAuthenticationError(code)

    monkeypatch.setattr(sync.garmin_registry, "call_paced", dead_link)

    result = await sync.run_sync(days=3, person_id=person_id)

    assert result == code
    assert calls == [person_id], "no further metric, date, or weight-history read after the failed login"
    assert (await _sync_status(person_id))["last_sync_result"] == code


async def test_run_sync_keeps_a_stop_result_over_an_earlier_error_count(initialized_db, monkeypatch):
    """A metric skipped before the login fails must not turn the stop into
    ``completed with 1 errors``: the stop result is the state the next sync
    and the UI act on, and an error count would hide it."""
    person_id = await get_primary_person_id()
    calls: list[int] = []

    async def skip_then_stop(person_id, operation):
        calls.append(person_id)
        if len(calls) == 1:
            raise garmin_registry.GarminOperationError("network")
        raise garmin_registry.GarminAuthenticationError("network")

    monkeypatch.setattr(sync.garmin_registry, "call_paced", skip_then_stop)

    result = await sync.run_sync(days=3, person_id=person_id)

    assert result == "network"
    assert len(calls) == 2, "the skipped metric's read, then the failed login, then nothing"
    assert (await _sync_status(person_id))["last_sync_result"] == "network"


@pytest.mark.parametrize("code", ("auth_failed", "rate_limited"))
async def test_run_sync_stops_at_a_terminal_operation_error(initialized_db, monkeypatch, code):
    """A rejected session or a throttled account fails every later read the
    same way; skipping the metric and carrying on would spend one doomed
    call per metric per date against an account already being rate
    limited.  Stop, record the code, and skip weight history."""
    person_id = await get_primary_person_id()
    calls: list[int] = []

    async def terminal(person_id, operation):
        calls.append(person_id)
        raise garmin_registry.GarminOperationError(code)

    monkeypatch.setattr(sync.garmin_registry, "call_paced", terminal)

    result = await sync.run_sync(days=3, person_id=person_id)

    assert result == code
    assert calls == [person_id], "no further metric, date, or weight-history read after a terminal error"
    assert (await _sync_status(person_id))["last_sync_result"] == code


@pytest.mark.parametrize("code", ("auth_failed", "rate_limited"))
async def test_run_sync_records_a_terminal_error_from_weight_history(
    initialized_db, fake_garmin_client, monkeypatch, code
):
    """Weight history reads through call_paced directly, not _fetch_metric,
    so it needs the same terminal handling rather than an error count."""
    person_id = await get_primary_person_id()
    real_call_paced = fake_garmin_client.registry_call

    async def throttled_weight_history(person_id, operation):
        # The weight-history read is the one lambda that names get_weigh_ins.
        if "get_weigh_ins" in operation.__code__.co_names:
            raise garmin_registry.GarminOperationError(code)
        return await real_call_paced(person_id, operation)

    monkeypatch.setattr(sync.garmin_registry, "call_paced", throttled_weight_history)

    result = await sync.run_sync(days=1, person_id=person_id)

    assert result == code
    assert (await _sync_status(person_id))["last_sync_result"] == code
    assert await _row_count("weight_history", person_id) == 0
    assert await _row_count("resting_hr", person_id) == 1, "the dates before it still synced"


async def test_run_sync_records_link_required_when_the_link_disappears(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    calls: list[int] = []

    async def unlinked(person_id, operation):
        calls.append(person_id)
        raise garmin_registry.GarminNotLinked(person_id)

    monkeypatch.setattr(sync.garmin_registry, "call_paced", unlinked)

    assert await sync.run_sync(days=3, person_id=person_id) == "link_required"
    assert calls == [person_id]
    assert (await _sync_status(person_id))["last_sync_result"] == "link_required"


async def test_scheduled_sync_skips_an_unlinked_primary(initialized_db, monkeypatch):
    """The scheduler must not fall back to deployment-wide credentials."""
    calls = []
    reached_sleep = asyncio.Event()
    keep_sleeping = asyncio.Event()

    async def unexpected_run_sync(**_kwargs):
        calls.append(True)

    async def block_after_initial_skip(_seconds):
        reached_sleep.set()
        await keep_sleeping.wait()

    monkeypatch.setattr(sync, "run_sync", unexpected_run_sync)
    monkeypatch.setattr(sync.asyncio, "sleep", block_after_initial_skip)

    task = asyncio.create_task(sync.scheduled_sync(asyncio.Lock(), sync.SyncRegistry()))
    try:
        await asyncio.wait_for(reached_sleep.wait(), timeout=5)
        assert calls == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_run_sync_preserves_backoff_until(initialized_db, fake_garmin_client):
    """run_sync must not clear sync_status.backoff_until (spec §e, the Garmin
    429 backoff). INSERT OR REPLACE deletes and reinserts the row, so every
    column the statement omits silently reverts to its default -- which would
    drop an active backoff on every sync and turn a rate limit into a ban."""
    from shared.database import get_primary_person_id

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
