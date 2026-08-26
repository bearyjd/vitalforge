"""Tests for the vitalforge-dashboard service's GET /api/export endpoint.

Same isolation story as test_dashboard_api.py: export reads only from the
local SQLite tables populated by sync.py, so tests seed the DB directly and
never need the fake Garmin client.
"""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db


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
    db = await get_db()
    try:
        for date, value in rows:
            await db.execute(
                f"INSERT OR REPLACE INTO [{table}] (date, [{column}]) VALUES (?, ?)",
                (date, value),
            )
        await db.commit()
    finally:
        await db.close()


async def test_export_single_metric_csv_happy_path(client):
    await seed_metric("steps", "value", [(days_ago(2), 1000.0), (days_ago(1), 2000.0)])

    resp = await client.get("/api/export?metric=steps&format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="vitalforge-export-steps-30d.csv"' in resp.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["date", "value"]
    # `steps.value` has INTEGER affinity (shared/database.py), so SQLite stores
    # these whole-number floats as ints and aiosqlite returns them as such.
    assert rows[1:] == [[days_ago(2), "1000"], [days_ago(1), "2000"]]


async def test_export_single_metric_json_happy_path(client):
    await seed_metric("resting_hr", "value", [(days_ago(1), 55.0)])

    resp = await client.get("/api/export?metric=resting_hr&format=json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert 'filename="vitalforge-export-resting_hr-30d.json"' in resp.headers["content-disposition"]

    body = resp.json()
    assert body == [{"date": days_ago(1), "value": 55.0}]


async def test_export_all_metrics_long_format(client):
    await seed_metric("steps", "value", [(days_ago(1), 1000.0)])
    await seed_metric("resting_hr", "value", [(days_ago(1), 55.0)])

    resp = await client.get("/api/export?metric=all&format=json")
    assert resp.status_code == 200
    body = resp.json()

    steps_rows = [r for r in body if r["metric"] == "steps"]
    hr_rows = [r for r in body if r["metric"] == "resting_hr"]
    assert steps_rows == [{"metric": "steps", "date": days_ago(1), "value": 1000.0}]
    assert hr_rows == [{"metric": "resting_hr", "date": days_ago(1), "value": 55.0}]


async def test_export_all_metrics_csv_has_metric_column(client):
    await seed_metric("steps", "value", [(days_ago(1), 1000.0)])
    await seed_metric("resting_hr", "value", [(days_ago(1), 55.0)])

    resp = await client.get("/api/export?metric=all&format=csv")
    assert resp.status_code == 200

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["metric", "date", "value"]
    data_rows = rows[1:]
    # Both `resting_hr.value` and `steps.value` have INTEGER affinity
    # (shared/database.py), so these whole-number floats round-trip as ints.
    assert ["resting_hr", days_ago(1), "55"] in data_rows
    assert ["steps", days_ago(1), "1000"] in data_rows


async def test_export_unknown_metric_returns_400(client):
    resp = await client.get("/api/export?metric=not_a_real_metric&format=csv")
    assert resp.status_code == 400


async def test_export_bad_format_returns_400(client):
    resp = await client.get("/api/export?metric=steps&format=xml")
    assert resp.status_code == 400


async def test_export_respects_days_window(client):
    # Seed one point far outside the requested window.
    await seed_metric("steps", "value", [(days_ago(365 * 5), 999.0)])
    await seed_metric("steps", "value", [(days_ago(1), 42.0)])

    resp = await client.get("/api/export?metric=steps&format=json&days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [{"date": days_ago(1), "value": 42.0}]


async def test_export_empty_table_returns_empty_csv_without_crashing(client):
    resp = await client.get("/api/export?metric=steps&format=csv")
    assert resp.status_code == 200

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows == [["date", "value"]]


async def test_export_empty_table_returns_empty_json_array_without_crashing(client):
    resp = await client.get("/api/export?metric=steps&format=json")
    assert resp.status_code == 200
    assert resp.json() == []
