"""Quality/export semantics, privacy and atomic local snapshots (synthetic data only)."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared import garmin_registry
from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, grant_person, seed_person, seed_token, seed_user
from vitalforge_dashboard import data_quality
from vitalforge_dashboard.metric_metadata import METRIC_METADATA
from vitalforge_dashboard.metrics import METRIC_TABLES

NOW = datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)
ENDPOINTS = ("data-quality", "export/package")


@pytest.fixture
async def client(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(data_quality, "utc_now", lambda: NOW)
    async with AsyncClient(transport=ASGITransport(app=dashboard_app_module.app), base_url="http://test") as ac:
        yield ac


async def seed(table: str, column: str, rows: list, person_id: int | None = None) -> None:
    person_id = person_id or await get_primary_person_id()
    db = await get_db()
    try:
        await db.executemany(
            f"INSERT OR REPLACE INTO [{table}] (person_id, date, [{column}]) VALUES (?, ?, ?)",
            [(person_id, date, value) for date, value in rows],
        )
        await db.commit()
    finally:
        await db.close()


async def sync_status(result: str, person_id: int | None = None, timestamp: str = NOW.isoformat(), backoff=None) -> None:
    person_id = person_id or await get_primary_person_id()
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO sync_status (person_id, last_sync_time, last_sync_result, backoff_until) "
            "VALUES (?, ?, ?, ?)", (person_id, timestamp, result, backoff),
        )
        await db.commit()
    finally:
        await db.close()


async def test_empty_quality_explicit_semantics(client):
    response = await client.get(f"{PERSON_PREFIX}/api/data-quality?days=7")
    assert response.status_code == 200
    report = response.json()
    assert set(report["metrics"]) == set(METRIC_TABLES) == set(METRIC_METADATA)
    assert len(report["metrics"]) == 16
    assert report["period"]["start_date"] == "2026-10-19"
    assert report["period"]["end_date"] == "2026-10-25"
    assert report["period"]["window_timezone"] == "UTC"
    assert "timezone is unknown" in report["period"]["observation_date_semantics"]
    assert report["sync"] == {
        "last_attempt_at": None, "last_result": "unknown", "status": "never_attempted",
        "backoff_until": None, "metric_outcomes": "unknown",
    }
    for metric in report["metrics"].values():
        assert metric["latest_observation_date"] is None
        assert metric["observed_days"] == 0
        assert metric["missing_days"] == 7
        assert metric["coverage_percent"] == 0
        assert metric["availability"] == "empty"
        assert metric["completeness"] == "empty"
        assert metric["freshness"] == "unknown"
    assert any("Today may be incomplete" in note for note in report["notes"])


async def test_exact_inclusive_period_zero_null_future_malformed_and_latest(client):
    await seed("steps", "value", [
        ("2026-10-18", 18), ("2026-10-19", 0), ("2026-10-20", None), ("2026-10-25", 25),
        ("2026-10-26", 26), ("2026-10-2x", 99), ("2026-02-30", 99), ("2026-10-24extra", 99),
    ])
    await seed("resting_hr", "value", [("2026-10-18", 55), ("2026-10-25", None)])
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=7")).json()
    assert package["data"]["steps"] == [{"date": "2026-10-19", "value": 0}, {"date": "2026-10-25", "value": 25}]
    steps = package["quality"]["metrics"]["steps"]
    assert (steps["observed_days"], steps["missing_days"], steps["coverage_percent"]) == (2, 5, 28.6)
    assert steps["latest_observation_date"] == "2026-10-25"
    assert steps["completeness"] == "partial"
    hr = package["quality"]["metrics"]["resting_hr"]
    assert hr["latest_observation_date"] == "2026-10-18"
    assert hr["availability"] == "available" and hr["completeness"] == "empty" and hr["freshness"] == "stale"
    assert package["data"]["resting_hr"] == []


async def test_daily_threshold_event_cadence_and_complete_today(client):
    await seed("steps", "value", [("2026-10-22", 0)])
    await seed("resting_hr", "value", [("2026-10-21", 50)])
    await seed("weight_history", "weight_grams", [("2026-01-01", 75000)])
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality?days=1")).json()
    assert report["metrics"]["steps"]["freshness"] == "current"
    assert report["metrics"]["steps"]["freshness_stale_after_days"] == 3
    assert report["metrics"]["resting_hr"]["freshness"] == "stale"
    assert report["metrics"]["weight"]["freshness"] == "unknown"
    assert report["metrics"]["weight"]["freshness_stale_after_days"] is None
    await seed("steps", "value", [("2026-10-25", 0)])
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality?days=1")).json()
    assert report["metrics"]["steps"]["completeness"] == "complete"
    assert report["metrics"]["steps"]["coverage_percent"] == 100


async def test_package_matches_report_units_all_metrics_and_privacy_headers(client):
    for name, (table, column) in METRIC_TABLES.items():
        # UPDATE preserves siblings that share the same daily table row.
        db = await get_db()
        try:
            await db.execute(f"INSERT OR IGNORE INTO [{table}] (person_id, date) VALUES (?, ?)",
                             (await get_primary_person_id(), "2026-10-25"))
            await db.execute(f"UPDATE [{table}] SET [{column}] = 0 WHERE date = ?", ("2026-10-25",))
            await db.commit()
        finally:
            await db.close()
    quality = await client.get(f"{PERSON_PREFIX}/api/data-quality?days=1")
    response = await client.get(f"{PERSON_PREFIX}/api/export/package?days=1")
    package = response.json()
    assert package["quality"] == quality.json()
    assert package["period"] == package["quality"]["period"]
    assert package["generated_at"] == NOW.isoformat()
    assert package["schema_version"] == "1.0"
    assert package["dictionary"] == METRIC_METADATA
    assert all(rows == [{"date": "2026-10-25", "value": 0}] for rows in package["data"].values())
    for name in ("weight", "bone_mass", "muscle_mass"):
        assert package["dictionary"][name]["unit"] == "g"
    assert package["dictionary"]["sleep_duration"]["unit"] == "seconds"
    assert package["dictionary"]["body_fat"]["unit"] == "%"
    assert package["dictionary"]["body_water"]["unit"] == "%"
    for resp in (quality, response):
        assert resp.headers["cache-control"] == "no-store, private"
        assert resp.headers["pragma"] == "no-cache"
        for forbidden in ('"person_id"', '"slug"', '"email"', '"token"', '"password"'):
            assert forbidden not in resp.text
    assert response.headers["content-disposition"] == 'attachment; filename="vitalforge-ai-export-1d.json"'


@pytest.mark.parametrize("result,expected,status", [
    ("success", "success", "success"), ("completed with 2 errors", "partial", "partial"),
    ("rate_limited", "rate_limited", "failed"), ("auth_failed", "auth_failed", "failed"),
    ("link_required", "link_required", "failed"), ("network", "network", "failed"),
    ("error", "error", "failed"), ("secret@example.test credential error", "unknown", "unknown"),
])
async def test_sync_bounded_results_not_freeform_or_metric_inferences(client, result, expected, status):
    await sync_status(result)
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality")).json()
    assert report["sync"]["last_result"] == expected
    assert report["sync"]["status"] == status
    assert report["sync"]["last_attempt_at"] == NOW.isoformat()
    assert report["sync"]["metric_outcomes"] == "unknown"
    assert "credential" not in str(report)
    assert report["metrics"]["steps"]["freshness"] == "unknown"


async def test_sync_timestamps_are_validated_and_backoff_disclosed(client):
    await sync_status("rate_limited", timestamp="sensitive account text", backoff="2026-10-26T00:00:00+02:00")
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality")).json()
    assert report["sync"]["last_attempt_at"] is None
    assert report["sync"]["backoff_until"] == "2026-10-25T22:00:00+00:00"
    assert report["sync"]["status"] == "backoff"
    await sync_status("success", timestamp="2026-10-25T12:30:00", backoff="sensitive backoff text")
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality")).json()
    assert report["sync"]["last_attempt_at"] is None and report["sync"]["backoff_until"] is None
    await sync_status("success", timestamp="0001-01-01T00:00:00+23:59")
    report = (await client.get(f"{PERSON_PREFIX}/api/data-quality")).json()
    assert report["sync"]["last_attempt_at"] is None


async def test_reports_never_call_garmin(client, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Quality and package requests must read local storage only")

    monkeypatch.setattr(garmin_registry, "call", forbidden)
    monkeypatch.setattr(garmin_registry, "call_paced", forbidden)
    for endpoint in ENDPOINTS:
        assert (await client.get(f"{PERSON_PREFIX}/api/{endpoint}")).status_code == 200


async def test_profiles_isolate_data_latest_and_sync(client):
    second = await seed_person("other-private-profile")
    await seed("steps", "value", [("2026-10-25", 888)], second)
    await sync_status("auth_failed", second)
    await seed("steps", "value", [("2026-10-01", 111)])
    await sync_status("success")
    primary = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=7")).json()
    other = (await client.get("/p/other-private-profile/api/export/package?days=7")).json()
    assert primary["data"]["steps"] == []
    assert primary["quality"]["metrics"]["steps"]["latest_observation_date"] == "2026-10-01"
    assert primary["quality"]["sync"]["last_result"] == "success"
    assert other["data"]["steps"] == [{"date": "2026-10-25", "value": 888}]
    assert other["quality"]["sync"]["last_result"] == "auth_failed"
    assert "other-private-profile" not in str(other)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("days", [0, 366, -1, "not-a-number"])
async def test_invalid_days_rejected(client, endpoint, days):
    assert (await client.get(f"{PERSON_PREFIX}/api/{endpoint}?days={days}")).status_code == 422


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("authentication", ["session", "bearer"])
async def test_view_auth_and_profile_denials(client, endpoint, authentication):
    user = await seed_user("viewer")
    person = await get_primary_person_id()
    await grant_person(person, user, "view")
    denied = await seed_person("ungranted")
    archived = await seed_person("archived")
    await grant_person(archived, user, "view")
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), archived))
        await db.commit()
    finally:
        await db.close()
    assert denied != person
    assert (await client.get(f"{PERSON_PREFIX}/api/{endpoint}")).status_code == 401
    if authentication == "session":
        client.cookies.set("vf_session", create_session_cookie("viewer", user, 1))
    else:
        _, token = await seed_token(user)
        client.headers["Authorization"] = f"Bearer {token}"
    assert (await client.get(f"{PERSON_PREFIX}/api/{endpoint}")).status_code == 200
    for slug in ("ungranted", "missing", "archived"):
        assert (await client.get(f"/p/{slug}/api/{endpoint}")).status_code == 404


async def test_nonfinite_text_values_and_invalid_calendar_dates_are_missing(client):
    await seed("steps", "value", [("2026-10-25", float("inf")), ("2026-10-24", "private-text"), ("2026-10-23", 0)])
    await seed("resting_hr", "value", [("2026-02-30", 50), ("2026-02-27", 49)])
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=365")).json()
    assert package["data"]["steps"] == [{"date": "2026-10-23", "value": 0}]
    assert package["quality"]["metrics"]["steps"]["latest_observation_date"] == "2026-10-23"
    assert package["quality"]["metrics"]["resting_hr"]["latest_observation_date"] == "2026-02-27"


async def test_database_failure_cannot_send_partial_success(client, dashboard_app_module, monkeypatch):
    class BrokenConnection:
        closed = False

        async def execute(self, *args):
            raise RuntimeError("synthetic database failure")

        async def close(self):
            self.closed = True

    db = BrokenConnection()

    async def broken_get_db():
        return db

    monkeypatch.setattr(data_quality, "get_db", broken_get_db)
    transport = ASGITransport(app=dashboard_app_module.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(f"{PERSON_PREFIX}/api/export/package")
    assert response.status_code == 500
    assert "content-disposition" not in response.headers
    assert db.closed


async def test_report_and_sync_use_one_read_transaction(client, monkeypatch):
    await seed("steps", "value", [("2026-10-25", 10)])
    await sync_status("success")
    db = await get_db()
    try:
        await db.execute("PRAGMA journal_mode=WAL")
    finally:
        await db.close()

    class ConcurrentConnection:
        changed = False

        def __init__(self, db):
            self.db = db

        async def execute(self, sql, *args):
            cursor = await self.db.execute(sql, *args)
            if sql.startswith("SELECT") and not self.changed:
                self.changed = True
                await seed("steps", "value", [("2026-10-25", 999)])
                await sync_status("auth_failed")
            return cursor

        async def close(self):
            await self.db.close()

    async def concurrent_get_db():
        return ConcurrentConnection(await get_db())

    monkeypatch.setattr(data_quality, "get_db", concurrent_get_db)
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=1")).json()
    assert package["data"]["steps"] == [{"date": "2026-10-25", "value": 10}]
    assert package["quality"]["sync"]["last_result"] == "success"
