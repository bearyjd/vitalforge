"""Tests for the vitalforge-dashboard service's read-only HTTP API.

Key insight from the roadmap: dashboard read endpoints (`/api/metrics/*`,
`/api/recommendations/rules-only`, `/api/sync/status`) never call Garmin at
request time — they only read the local SQLite tables populated by
`sync.py`. So these tests seed the DB directly and never need the fake
Garmin client at all.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, seed_person


def days_ago(n: int) -> str:
    """A synthetic date string N days before now, for seeding within the
    dashboard's default lookback window."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
async def client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def seed_metric(table: str, column: str, rows: list[tuple[str, float]]):
    """Insert (date, value) rows into a metric table for testing."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        for date, value in rows:
            await db.execute(
                f"INSERT OR REPLACE INTO [{table}] (person_id, date, [{column}]) VALUES (?, ?, ?)",
                (person_id, date, value),
            )
        await db.commit()
    finally:
        await db.close()


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "vitalforge-dashboard"}


async def test_sync_status_never_synced(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_sync_time"] is None
    assert body["last_sync_result"] == "never"


async def test_sync_status_does_not_leak_another_persons_sync(
    client, dashboard_app_module, monkeypatch
):
    """`syncing` used to report `_sync_lock.locked()` -- one module-level lock
    -- on an otherwise person-scoped response, so one household member's sync
    was visible from another's status endpoint.

    The lock's SERIALIZATION is still shared (Phase 4 fixes that); only the
    leaked flag is scoped here.
    """
    other_person = await seed_person("bryn")
    registry = dashboard_app_module.SyncRegistry()
    registry.acquire(other_person)
    monkeypatch.setattr(dashboard_app_module, "_syncing_person_ids", registry)

    resp = await client.get(f"{PERSON_PREFIX}/api/sync/status")
    assert resp.status_code == 200
    assert resp.json()["syncing"] is False, "another person's sync was visible from this one"

    # Positive control: the person who IS syncing still sees it, so the test
    # above cannot pass against a `syncing` that is hardcoded False.
    resp = await client.get("/p/bryn/api/sync/status")
    assert resp.json()["syncing"] is True


async def test_starting_a_sync_is_not_blocked_by_another_persons_sync(
    client, dashboard_app_module, monkeypatch
):
    """POST /api/sync answered "already_running" off the module-level lock, so
    a caller who had started nothing was told their sync was in progress --
    the same cross-person observable that leaked out of /api/sync/status.

    The shared lock still SERIALIZES the work (Phase 4 changes that); the
    second person's request now queues on it rather than being refused.
    """
    other_person = await seed_person("bryn")
    person_id = await get_primary_person_id()
    registry = dashboard_app_module.SyncRegistry()
    registry.acquire(other_person)
    monkeypatch.setattr(dashboard_app_module, "_syncing_person_ids", registry)

    # This test is about the routing decision, not the sync: a real run_sync
    # here reaches shared.garmin_client's authenticate path and blocks on the
    # network.
    async def _noop_run_sync(days, *, person_id):
        return "ok"

    monkeypatch.setattr(dashboard_app_module, "run_sync", _noop_run_sync)

    resp = await client.post(f"{PERSON_PREFIX}/api/sync", json={"days": 1})
    assert resp.json()["status"] == "started"

    # Positive control: the person who IS syncing is still refused a second
    # run. Asserted on the primary person, because a non-primary one is
    # refused earlier by the Garmin-source 409 and would never reach this
    # branch -- which is exactly how the first assertion could pass vacuously.
    registry.acquire(person_id)
    resp = await client.post(f"{PERSON_PREFIX}/api/sync", json={"days": 1})
    assert resp.json()["status"] == "already_running"


async def test_the_scheduled_backfill_registers_itself_as_syncing(
    client, dashboard_app_module, monkeypatch
):
    """`scheduled_sync` takes the same lock the manual trigger does, so when
    `syncing` was `_sync_lock.locked()` the boot backfill was reported. Scoping
    the flag by person regressed that unless the scheduled path registers too:
    the dashboard's poll stops as soon as `syncing` goes false, so it would
    announce a finished sync during a 90-day backfill that had not started
    writing yet -- and the manual trigger, still blocked by the lock, would
    have created no task.
    """
    import sync as sync_module

    registry = dashboard_app_module.SyncRegistry()
    monkeypatch.setattr(dashboard_app_module, "_syncing_person_ids", registry)
    person_id = await get_primary_person_id()
    observed = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_run_sync(days, *, person_id):
        observed.append(person_id in registry)
        started.set()
        await release.wait()

    monkeypatch.setattr(sync_module, "run_sync", _slow_run_sync)
    task = asyncio.create_task(
        sync_module.scheduled_sync(dashboard_app_module._sync_lock, registry)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert observed == [True], "the scheduled backfill did not register the person it syncs"
        status = (await client.get(f"{PERSON_PREFIX}/api/sync/status")).json()
        assert status["syncing"] is True, "the dashboard reports idle during the boot backfill"
    finally:
        release.set()
        task.cancel()
    # Positive control: once it finishes, the registration is released.
    for _ in range(50):
        await asyncio.sleep(0)
        if person_id not in registry:
            break
    assert person_id not in registry


async def test_a_failed_sync_does_not_leave_the_person_marked_as_syncing(
    client, dashboard_app_module, monkeypatch
):
    """Without the try/finally, run_sync raising leaves the person marked as
    syncing forever while the lock itself releases normally -- a dashboard
    stuck on "syncing" with nothing running, and a POST that answers
    "already_running" from then on."""
    ran = []

    async def _boom(**kwargs):
        ran.append(True)
        raise RuntimeError("garmin exploded")

    monkeypatch.setattr(dashboard_app_module, "run_sync", _boom)
    person_id = await get_primary_person_id()
    assert person_id not in dashboard_app_module._syncing_person_ids

    resp = await client.post(f"{PERSON_PREFIX}/api/sync", json={"days": 1})
    assert resp.status_code == 200
    # The sync runs as a detached task after the response; yield until it has
    # actually run. Asserting `ran` rather than only the end state is what stops
    # this passing against a task that never started at all.
    for _ in range(50):
        await asyncio.sleep(0)
        if ran:
            break
    assert ran, "the detached sync task never ran; the assertions below prove nothing"

    assert person_id not in dashboard_app_module._syncing_person_ids
    assert (await client.get(f"{PERSON_PREFIX}/api/sync/status")).json()["syncing"] is False
    # And a retry is accepted rather than answering "already_running" forever.
    assert (
        await client.post(f"{PERSON_PREFIX}/api/sync", json={"days": 1})
    ).json()["status"] == "started"


@pytest.mark.parametrize(
    "metric_name,table,column",
    [
        ("sleep_duration", "sleep", "duration_seconds"),
        ("sleep_score", "sleep", "sleep_score"),
        ("resting_hr", "resting_hr", "value"),
        ("hrv", "hrv", "last_night_avg"),
        ("body_battery", "body_battery", "highest"),
        ("body_battery_low", "body_battery", "lowest"),
        ("stress", "stress", "avg_level"),
        ("vo2max", "vo2max", "vo2max_value"),
        ("weight", "weight_history", "weight_grams"),
        ("body_fat", "weight_history", "body_fat"),
        ("body_water", "weight_history", "body_water"),
        ("bone_mass", "weight_history", "bone_mass_g"),
        ("muscle_mass", "weight_history", "muscle_mass_g"),
        ("training_load", "training_load", "acute_load"),
        ("steps", "steps", "value"),
        ("active_calories", "active_calories", "value"),
    ],
)
async def test_get_metric_returns_seeded_data(client, metric_name, table, column):
    # Synthetic values only — never data from a real fitness.db.
    await seed_metric(table, column, [(days_ago(2), 10.0), (days_ago(1), 20.0)])

    resp = await client.get(f"{PERSON_PREFIX}/api/metrics/{metric_name}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["metric"] == metric_name
    assert body["count"] == 2
    assert [d["value"] for d in body["data"]] == [10.0, 20.0]
    # 7-day moving average of a single point is just that point.
    assert body["data"][0]["moving_avg_7d"] == 10.0
    assert body["data"][1]["moving_avg_7d"] == 15.0


async def test_get_metric_unknown_name_returns_400(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/metrics/not_a_real_metric")
    assert resp.status_code == 400


async def test_metric_tables_includes_body_water_muscle_bone(dashboard_app_module):
    assert dashboard_app_module.METRIC_TABLES["body_water"] == ("weight_history", "body_water")
    assert dashboard_app_module.METRIC_TABLES["bone_mass"] == ("weight_history", "bone_mass_g")
    assert dashboard_app_module.METRIC_TABLES["muscle_mass"] == ("weight_history", "muscle_mass_g")


@pytest.mark.parametrize("metric_name", ["body_water", "bone_mass", "muscle_mass"])
async def test_composition_metrics_return_empty_series_when_garmin_values_null(client, metric_name):
    """Production's actual state on launch day: every composition field is
    null until the first Track B push round-trips through Garmin. The
    endpoint must return an empty series, not error or crash the moving-
    average loop."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO weight_history (person_id, date, weight_grams, bmi, body_fat, body_water, bone_mass_g, muscle_mass_g) "
            "VALUES (?, ?, 81200, 24.1, 18.4, NULL, NULL, NULL)",
            (person_id, days_ago(1)),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.get(f"{PERSON_PREFIX}/api/metrics/{metric_name}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["data"] == []


async def test_get_metric_respects_days_window(client):
    # Seed one point far outside the default 30-day window.
    await seed_metric("steps", "value", [(days_ago(365 * 5), 999.0)])

    resp = await client.get(f"{PERSON_PREFIX}/api/metrics/steps?days=30")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


async def test_recommendations_rules_only_empty_db(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/recommendations/rules-only")
    assert resp.status_code == 200
    body = resp.json()
    assert "findings" in body
    assert body["count"] == len(body["findings"])
    assert isinstance(body["findings"], list)


async def test_recommendations_rules_only_does_not_call_garmin(client, fake_garmin_client):
    """Confirms the roadmap's key insight: no Garmin call for a read endpoint."""
    await seed_metric("resting_hr", "value", [(days_ago(1), 55.0)])

    resp = await client.get(f"{PERSON_PREFIX}/api/recommendations/rules-only")
    assert resp.status_code == 200
    assert fake_garmin_client.pushed_weights == []


async def test_get_metric_excludes_other_persons_rows(client):
    """The phase's whole point: a second person's rows for the same date
    range must never leak into the primary person's read. Every existing
    test in this module has exactly one person seeded, so a scoped and an
    unscoped SELECT would both pass them -- this is the one test that would
    actually fail if the `WHERE person_id = ?` predicate were dropped from
    `get_metrics`."""
    primary_id = await get_primary_person_id()

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", datetime.now(timezone.utc).isoformat()),
        )
        second_id = cursor.lastrowid
        await db.commit()

        await db.execute(
            "INSERT OR REPLACE INTO [steps] (person_id, date, value) VALUES (?, ?, ?)",
            (primary_id, days_ago(1), 1000.0),
        )
        await db.execute(
            "INSERT OR REPLACE INTO [steps] (person_id, date, value) VALUES (?, ?, ?)",
            (second_id, days_ago(1), 9999.0),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.get(f"{PERSON_PREFIX}/api/metrics/steps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["data"][0]["value"] == 1000.0
