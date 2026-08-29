"""Tests for the vitalforge-dashboard service's read-only HTTP API.

Key insight from the roadmap: dashboard read endpoints (`/api/metrics/*`,
`/api/recommendations/rules-only`, `/api/sync/status`) never call Garmin at
request time — they only read the local SQLite tables populated by
`sync.py`. So these tests seed the DB directly and never need the fake
Garmin client at all.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX


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
