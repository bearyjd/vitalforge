"""B5: `sync_weight_history()` reading Garmin's composition fields
(bodyWater/boneMass/muscleMass) off `latestWeight` into `weight_history`.
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

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

    Observes the boot backfill AND a later tick, not just the first. An
    earlier version only ever saw the backfill call (the loop's
    `asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)` never completed before the
    test cancelled the task), so a lock-free later tick would have left it
    green (Phase 4 fix-review finding). Setting SYNC_INTERVAL_HOURS to 0
    collapses that sleep to effectively-zero, letting a second call happen
    inside the test's timeout."""
    monkeypatch.setattr(sync, "SYNC_INTERVAL_HOURS", 0)
    lock = asyncio.Lock()
    seen = []
    second_call = asyncio.Event()

    async def fake_run_sync(days, *, person_id):
        seen.append((days, lock.locked()))
        if len(seen) >= 2:
            second_call.set()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)
    # A real link row: the scheduler's cursor reads garmin_links in SQL.
    await _link(await get_primary_person_id())

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


# --- #67: the scheduler rotates over every linked person ---------------------


async def _link(person_id: int, state: str = "linked") -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at)
            VALUES (?, ?, ?, 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')
            """,
            (person_id, state, f"person-{person_id}@example.test"),
        )
        await db.commit()
    finally:
        await db.close()


async def _set_sync_status(person_id: int, last_sync_time: str | None, backoff_until: str | None = None) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sync_status (person_id, last_sync_time, last_sync_result, last_sync_days, backoff_until) "
            "VALUES (?, ?, 'success', 3, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET last_sync_time = excluded.last_sync_time, "
            "backoff_until = excluded.backoff_until",
            (person_id, last_sync_time, backoff_until),
        )
        await db.commit()
    finally:
        await db.close()


async def _drive_scheduler(monkeypatch, ticks: int, outcome=None):
    """Run scheduled_sync for `ticks` run_sync calls and return them as
    (person_id, days) pairs. The fake writes sync_status the way the real
    run_sync does (start time, ON CONFLICT keeps backoff_until), with
    strictly increasing timestamps so rotation order is deterministic.
    `outcome(person_id, call_index)` may return a result or raise."""
    monkeypatch.setattr(sync, "SYNC_INTERVAL_HOURS", 0)
    calls: list[tuple[int, int]] = []
    done = asyncio.Event()
    clock = iter(range(1, 10_000))

    async def fake_run_sync(days, *, person_id):
        calls.append((person_id, days))
        if len(calls) == ticks:
            done.set()
            await asyncio.Event().wait()  # park here; the test cancels us
        if len(calls) > ticks:
            # Reachable only if a change broke cancellation: the parked call
            # above swallowed its cancel and the loop ticked again. Never
            # park twice, so _cancel can still stop it (see _cancel).
            return "success"
        result = outcome(person_id, len(calls) - 1) if outcome else "success"
        await _set_sync_status(person_id, f"2026-10-01T00:00:{next(clock):02d}+00:00")
        return result

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)
    task = asyncio.create_task(sync.scheduled_sync(asyncio.Lock(), sync.SyncRegistry()))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await _cancel(task)
    return calls


async def _cancel(task: asyncio.Task) -> None:
    """Cleanup that cannot hang the suite. A scheduler that wrongly swallowed
    cancellation can still be stopped while it sleeps between ticks (that
    await is outside its try), so keep cancelling until one lands there.
    asyncio.wait, not wait_for: on timeout wait_for re-cancels and then
    AWAITS the task, which hangs on exactly that broken scheduler.
    test_cancelling_the_scheduler_mid_sync_releases_everything is what
    asserts a single cancel is enough."""
    for _ in range(100):
        if task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=0.05)
    assert task.done(), "scheduled_sync could not be stopped"


async def test_scheduled_sync_rotates_every_linked_person_oldest_first(initialized_db, monkeypatch):
    """One person per tick, never-synced first, then oldest last_sync_time;
    each gets its own 90-day backfill once, then the 3-day incremental."""
    from tests.conftest import seed_person

    primary = await get_primary_person_id()
    second = await seed_person("bryn")
    await _link(primary)
    await _link(second)

    calls = await _drive_scheduler(monkeypatch, ticks=5)

    assert calls == [(primary, 90), (second, 90), (primary, 3), (second, 3), (primary, 3)]


async def test_scheduled_sync_never_picks_an_unlinked_archived_or_retired_person(initialized_db, monkeypatch):
    from tests.conftest import seed_person

    primary = await get_primary_person_id()
    await _link(primary)
    await seed_person("unlinked")
    retired = await seed_person("retired")
    await _link(retired, state="legacy_bound")
    archived = await seed_person("archived")
    await _link(archived)
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = '2026-09-30T00:00:00Z' WHERE id = ?", (archived,))
        await db.commit()
    finally:
        await db.close()

    calls = await _drive_scheduler(monkeypatch, ticks=3)

    assert {person_id for person_id, _ in calls} == {primary}


async def test_scheduled_sync_skips_a_person_in_backoff(initialized_db, monkeypatch):
    from tests.conftest import seed_person

    primary = await get_primary_person_id()
    second = await seed_person("bryn")
    await _link(primary)
    await _link(second)
    # Primary is the older row, so without the backoff it would go first.
    await _set_sync_status(primary, "2026-09-01T00:00:00+00:00", backoff_until="2999-01-01T00:00:00+00:00")
    await _set_sync_status(second, "2026-09-02T00:00:00+00:00")

    calls = await _drive_scheduler(monkeypatch, ticks=2)

    assert [person_id for person_id, _ in calls] == [second, second]


@pytest.mark.parametrize("stopped", ("network", "link_required", "auth_failed", "unknown"))
async def test_a_cheaply_interrupted_backfill_is_retried_as_a_backfill(initialized_db, monkeypatch, stopped):
    """run_sync writes last_sync_time even when it stops early, so a
    'has a row' rule would demote a half-done backfill to 3-day runs and
    the rest of the 90 days would never be fetched. These results stop at
    the first Garmin call, so retrying the backfill costs about one call."""
    primary = await get_primary_person_id()
    await _link(primary)

    calls = await _drive_scheduler(
        monkeypatch, ticks=3, outcome=lambda _p, i: stopped if i == 0 else "success"
    )

    assert calls == [(primary, 90), (primary, 90), (primary, 3)]


async def test_a_failed_tick_record_preserves_backoff_until(initialized_db):
    """_record_failed_tick must UPDATE, not REPLACE: a REPLACE resets every
    column it does not name, and clearing an active backoff_until is how a
    rate limit turns into a ban (see run_sync's sync_status write)."""
    from datetime import datetime, timezone

    primary = await get_primary_person_id()
    await _set_sync_status(primary, "2026-09-01T00:00:00+00:00", backoff_until="2999-01-01T00:00:00+00:00")

    await sync._record_failed_tick(primary, datetime(2026, 10, 1, tzinfo=timezone.utc), 90)

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT last_sync_time, last_sync_result, last_sync_days, backoff_until "
                "FROM sync_status WHERE person_id = ?",
                (primary,),
            )
        ).fetchone()
    finally:
        await db.close()
    assert dict(row) == {
        "last_sync_time": "2026-10-01T00:00:00+00:00",
        "last_sync_result": "error",
        "last_sync_days": 90,
        "backoff_until": "2999-01-01T00:00:00+00:00",
    }


async def test_cancelling_the_scheduler_mid_sync_releases_everything(initialized_db, monkeypatch):
    """The lifespan cancels scheduled_sync on shutdown, often mid-run. The
    cancel must end the task -- not be recorded as an 'error' tick and
    swallowed -- and must release the lock and the syncing registration."""
    primary = await get_primary_person_id()
    await _link(primary)
    lock = asyncio.Lock()
    registry = sync.SyncRegistry()
    started = asyncio.Event()

    async def parked_run_sync(days, *, person_id):
        if not started.is_set():
            started.set()
            await asyncio.Event().wait()  # park once; see _cancel

    monkeypatch.setattr(sync, "run_sync", parked_run_sync)
    task = asyncio.create_task(sync.scheduled_sync(lock, registry))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert primary in registry and lock.locked()

    task.cancel()
    await asyncio.wait({task}, timeout=5)
    one_cancel_was_enough = task.done()
    await _cancel(task)  # cleanup if it was not

    assert one_cancel_was_enough, "scheduled_sync swallowed its cancellation and kept running"
    assert task.cancelled()
    assert not lock.locked()
    assert primary not in registry
    assert await _sync_status(primary) is None, "a cancel must not be recorded as a failed tick"


async def test_a_raising_sync_does_not_starve_the_next_person(initialized_db, monkeypatch, caplog):
    """If run_sync raises before writing sync_status, that person's
    last_sync_time never moves and an oldest-first cursor would pick them
    on every tick forever. The scheduler records the failed tick itself."""
    from tests.conftest import seed_person

    primary = await get_primary_person_id()
    second = await seed_person("bryn")
    await _link(primary)
    await _link(second)
    monkeypatch.setattr(sync, "SYNC_INTERVAL_HOURS", 0)
    calls: list[int] = []
    done = asyncio.Event()

    async def fake_run_sync(days, *, person_id):
        calls.append(person_id)
        if person_id == primary:
            raise RuntimeError("boom before sync_status was written")
        if not done.is_set():
            done.set()
            await asyncio.Event().wait()  # park once; see _cancel

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)
    task = asyncio.create_task(sync.scheduled_sync(asyncio.Lock(), sync.SyncRegistry()))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await _cancel(task)

    assert calls == [primary, second]
    assert (await _sync_status(primary))["last_sync_result"] == "error"


# --- #88: a Garmin 429 sets backoff_until -------------------------------------


async def _backoff_state(person_id: int) -> dict | None:
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT last_sync_result, backoff_until, backoff_streak FROM sync_status WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
    finally:
        await db.close()
    return dict(row) if row else None


async def _seed_backoff(person_id: int, streak: int | None, until: str | None) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sync_status (person_id, last_sync_time, last_sync_result, last_sync_days, "
            "backoff_until, backoff_streak) VALUES (?, '2026-09-01T00:00:00+00:00', 'rate_limited', 3, ?, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET backoff_until = excluded.backoff_until, "
            "backoff_streak = excluded.backoff_streak",
            (person_id, until, streak),
        )
        await db.commit()
    finally:
        await db.close()


def _stop_every_read_with(monkeypatch, exc: Exception) -> None:
    async def stop(person_id, operation):
        raise exc

    monkeypatch.setattr(sync.garmin_registry, "call_paced", stop)


@pytest.mark.parametrize(
    "streak, minutes",
    ((1, 15), (2, 30), (3, 60), (4, 120), (5, 240), (6, 360), (7, 360), (50, 360)),
)
def test_backoff_delay_doubles_from_15_minutes_and_caps_at_6_hours(streak, minutes):
    assert sync.backoff_delay(streak) == timedelta(minutes=minutes)


@pytest.mark.parametrize("streak", (0, -1))
def test_backoff_delay_rejects_a_streak_that_is_not_a_streak(streak):
    with pytest.raises(ValueError):
        sync.backoff_delay(streak)


@pytest.mark.parametrize(
    "exc",
    (
        garmin_registry.GarminOperationError("rate_limited"),
        garmin_registry.GarminAuthenticationError("rate_limited"),
    ),
    ids=("throttled-read", "throttled-cold-login"),
)
async def test_a_rate_limited_sync_sets_a_15_minute_backoff(initialized_db, monkeypatch, exc):
    person_id = await get_primary_person_id()
    _stop_every_read_with(monkeypatch, exc)

    before = datetime.now(timezone.utc)
    assert await sync.run_sync(days=1, person_id=person_id) == "rate_limited"
    after = datetime.now(timezone.utc)

    state = await _backoff_state(person_id)
    assert state["backoff_streak"] == 1
    # The cursor compares this as TEXT, so the form is part of the contract.
    assert state["backoff_until"].endswith("+00:00")
    until = datetime.fromisoformat(state["backoff_until"])
    assert before + timedelta(minutes=15) <= until <= after + timedelta(minutes=15)


async def test_consecutive_rate_limits_escalate_the_backoff(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    _stop_every_read_with(monkeypatch, garmin_registry.GarminOperationError("rate_limited"))

    await sync.run_sync(days=1, person_id=person_id)
    before = datetime.now(timezone.utc)
    await sync.run_sync(days=1, person_id=person_id)

    state = await _backoff_state(person_id)
    assert state["backoff_streak"] == 2
    until = datetime.fromisoformat(state["backoff_until"])
    assert until >= before + timedelta(minutes=30)
    assert until <= datetime.now(timezone.utc) + timedelta(minutes=30)


async def test_the_backoff_never_exceeds_6_hours(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _seed_backoff(person_id, streak=40, until="2026-09-01T00:00:00+00:00")
    _stop_every_read_with(monkeypatch, garmin_registry.GarminOperationError("rate_limited"))

    await sync.run_sync(days=1, person_id=person_id)

    state = await _backoff_state(person_id)
    assert state["backoff_streak"] == 41
    until = datetime.fromisoformat(state["backoff_until"])
    assert until <= datetime.now(timezone.utc) + timedelta(hours=6)


@pytest.mark.parametrize(
    "exc, result",
    (
        (garmin_registry.GarminAuthenticationError("network"), "network"),
        (garmin_registry.GarminAuthenticationError("unknown"), "unknown"),
        (garmin_registry.GarminAuthenticationError("auth_failed"), "auth_failed"),
        (garmin_registry.GarminNotLinked(1), "link_required"),
    ),
)
async def test_other_stopped_results_leave_an_active_backoff_alone(initialized_db, monkeypatch, exc, result):
    """Only a 429 is evidence about the throttle. A run that stopped for a
    network blip or a relink says nothing about it: the streak must not
    reset (that would let flapping connectivity erase a ban in progress)
    nor grow."""
    person_id = await get_primary_person_id()
    until = "2999-01-01T00:00:00+00:00"
    await _seed_backoff(person_id, streak=3, until=until)
    _stop_every_read_with(monkeypatch, exc)

    assert await sync.run_sync(days=1, person_id=person_id) == result

    state = await _backoff_state(person_id)
    assert state["backoff_until"] == until
    assert state["backoff_streak"] == 3
    assert state["last_sync_result"] == result


async def test_a_failed_tick_leaves_an_active_backoff_alone(initialized_db):
    person_id = await get_primary_person_id()
    until = "2999-01-01T00:00:00+00:00"
    await _seed_backoff(person_id, streak=3, until=until)

    await sync._record_failed_tick(person_id, datetime(2026, 10, 1, tzinfo=timezone.utc), 3)

    assert await _backoff_state(person_id) == {
        "last_sync_result": "error",
        "backoff_until": until,
        "backoff_streak": 3,
    }


async def test_a_successful_sync_clears_the_backoff(initialized_db, fake_garmin_client):
    person_id = await get_primary_person_id()
    await _seed_backoff(person_id, streak=4, until="2999-01-01T00:00:00+00:00")

    await sync.run_sync(days=1, person_id=person_id)

    state = await _backoff_state(person_id)
    assert state["backoff_until"] is None
    assert state["backoff_streak"] is None
    assert state["last_sync_result"] == "success"


async def test_a_sync_that_only_skipped_some_metrics_clears_the_backoff(initialized_db, monkeypatch):
    """'completed with N errors' means Garmin answered the account; a
    skipped metric is not a throttle."""
    person_id = await get_primary_person_id()
    await _seed_backoff(person_id, streak=2, until="2999-01-01T00:00:00+00:00")
    _stop_every_read_with(monkeypatch, garmin_registry.GarminOperationError("network"))

    result = await sync.run_sync(days=1, person_id=person_id)

    assert result.startswith("completed with")
    state = await _backoff_state(person_id)
    assert state["backoff_until"] is None and state["backoff_streak"] is None


async def test_the_rotation_skips_a_throttled_person_until_the_backoff_passes(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    _stop_every_read_with(monkeypatch, garmin_registry.GarminOperationError("rate_limited"))
    await sync.run_sync(days=1, person_id=person_id)

    now = datetime.now(timezone.utc)
    assert await sync.next_person_to_sync(now.isoformat()) is None
    assert await sync.next_person_to_sync((now + timedelta(minutes=14)).isoformat()) is None
    assert await sync.next_person_to_sync((now + timedelta(minutes=16)).isoformat()) == person_id


async def test_a_rate_limited_backfill_is_retried_as_a_backfill_once_the_backoff_passes(
    initialized_db, monkeypatch
):
    """With a backoff gating the retry, a throttled 90-day backfill no longer
    needs demoting to the incremental window: the next attempt waits out the
    backoff and then resumes the backfill (run_sync skips dates it already
    stored)."""
    person_id = await get_primary_person_id()
    await _link(person_id)
    _stop_every_read_with(monkeypatch, garmin_registry.GarminOperationError("rate_limited"))
    real_run_sync = sync.run_sync
    days_seen: list[int] = []

    async def spy(days, *, person_id):
        days_seen.append(days)
        return await real_run_sync(days=days, person_id=person_id)

    monkeypatch.setattr(sync, "run_sync", spy)
    lock, registry, backfilled = asyncio.Lock(), sync.SyncRegistry(), set()

    await sync._sync_next_person(lock, registry, backfilled)
    assert days_seen == [90]

    await sync._sync_next_person(lock, registry, backfilled)
    assert days_seen == [90], "ran again inside the backoff"

    await _seed_backoff(person_id, streak=1, until="2026-09-01T00:00:00+00:00")  # expired
    await sync._sync_next_person(lock, registry, backfilled)
    assert days_seen == [90, 90], "the backfill was demoted instead of retried"
    assert (await _backoff_state(person_id))["backoff_streak"] == 2
