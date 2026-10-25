"""Personal comparison semantics and authorization with synthetic local data only."""

import math
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from shared import garmin_registry
from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, grant_person, seed_person, seed_token, seed_user
from tests.test_data_quality import NOW, seed
from vitalforge_dashboard import comparisons, data_quality, recommendations
from vitalforge_dashboard.readiness import score_readiness

PATH = f"{PERSON_PREFIX}/api/comparisons"


@pytest.fixture
async def client(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(data_quality, "utc_now", lambda: NOW)
    async with AsyncClient(transport=ASGITransport(app=dashboard_app_module.app), base_url="http://test") as client:
        yield client


def rows(start: int, end: int, value: float) -> list:
    return [((NOW.date() - timedelta(days=offset)).isoformat(), value) for offset in range(start, end)]


@pytest.mark.parametrize("days", [7, 30, 90])
async def test_exact_adjacent_periods_and_uncontaminated_baseline(client, days):
    await seed("hrv", "last_night_avg", rows(0, days, 100) + rows(days, days * 2, 40) + rows(days * 2, days * 2 + 1, 999))
    report = (await client.get(PATH, params={"days": days})).json()
    assert report["current_period"] == comparisons._period(NOW.date(), days)
    assert report["previous_period"] == comparisons._period(NOW.date() - timedelta(days=days), days)
    assert report["previous_period"]["end_date"] < report["current_period"]["start_date"]
    metric = report["metrics"]["hrv"]
    assert metric["current"] == {"n": days, "mean": 100}
    assert metric["previous"] == {"n": days, "mean": 40}
    assert metric["difference"] == 60 and metric["percent_change"] == 150
    assert metric["minimum_samples"] == max(3, math.ceil(days / 2))
    assert metric["baseline"] == {
        "method": "previous_period_mean_plus_minus_sample_sd", "n": days,
        "mean": 40, "sample_sd": 0, "lower": 40, "upper": 40,
    }
    assert len(metric["data"]) == days
    assert metric["data"][0]["date"] == report["current_period"]["start_date"]
    assert report["findings_period"] == comparisons._period(NOW.date(), 30)


@pytest.mark.parametrize("days", [0, 1, 14, 31, 365, "oops"])
async def test_invalid_periods(client, days):
    assert (await client.get(PATH, params={"days": days})).status_code == 422


async def test_zero_null_sparse_malformed_future_nonfinite(client):
    await seed("hrv", "last_night_avg", rows(0, 4, 20) + rows(7, 11, 0) + [
        ("2026-10-21", None), ("2026-10-20", float("inf")), ("2026-10-19", "sensitive text"),
        ("2026-10-26", 999), ("2026-10-2x", 999), ("2026-10-24bad", 999),
    ])
    response = await client.get(PATH, params={"days": 7})
    assert response.status_code == 200
    metric = response.json()["metrics"]["hrv"]
    assert metric["status"] == "available"
    assert metric["difference"] == 20 and metric["percent_change"] is None
    assert metric["current"] == {"n": 4, "mean": 20}
    assert len(metric["data"]) == 7 and sum(row["value"] is None for row in metric["data"]) == 3
    assert "sensitive" not in response.text
    await seed("resting_hr", "value", rows(0, 3, 50) + rows(7, 11, 40))
    sparse = (await client.get(PATH, params={"days": 7})).json()["metrics"]["resting_hr"]
    assert sparse["current"] == {"n": 3, "mean": 50}
    assert sparse["status"] == "insufficient_data"
    assert sparse["difference"] is sparse["percent_change"] is sparse["baseline"] is None


async def test_stale_reports_counts_without_interpretation(client):
    await seed("hrv", "last_night_avg", rows(4, 15, 30) + rows(30, 60, 40))
    metric = (await client.get(PATH)).json()["metrics"]["hrv"]
    assert metric["status"] == metric["freshness"] == "stale"
    assert metric["current"] == {"n": 11, "mean": 30}
    assert metric["difference"] is metric["baseline"] is None


async def test_snapshot_counts_units_and_no_interpretation(client):
    await seed("weight_history", "weight_grams", rows(0, 14, 70000))
    report = (await client.get(PATH, params={"days": 7})).json()
    metric = report["metrics"]["weight"]
    assert metric["unit"] == "g" and metric["freshness"] == "unknown"
    assert metric["current"] == metric["previous"] == {"n": 7, "mean": 70000}
    assert metric["status"] == "snapshot_only"
    assert metric["difference"] is metric["percent_change"] is metric["baseline"] is None
    assert "repeat" in " ".join(metric["notes"])
    assert report["findings"] == []


async def test_extreme_finite_values_do_not_overflow_means_or_response(client):
    await seed("hrv", "last_night_avg", rows(0, 14, 1e308))
    await seed("resting_hr", "value", rows(0, 7, 1e308) + rows(7, 14, -1e308))
    await seed("stress", "avg_level", rows(0, 7, 0) + rows(7, 10, 1.7e308) + rows(10, 14, -1.7e308))
    response = await client.get(PATH, params={"days": 7})
    assert response.status_code == 200
    report = response.json()
    assert comparisons._all_finite(report)
    assert report["metrics"]["hrv"]["current"]["mean"] == 1e308
    assert report["metrics"]["hrv"]["baseline"]["sample_sd"] == 0
    assert report["metrics"]["resting_hr"]["difference"] is None
    assert report["metrics"]["resting_hr"]["status"] == "insufficient_data"
    assert report["metrics"]["stress"]["baseline"] is None
    assert report["readiness"]["score"] is None
    assert any("arithmetic" in note for note in report["readiness"]["notes"])


async def test_findings_actual_observation_dates_and_constant_baseline_flag(client):
    # A missing day makes the baseline an observation-count window, not 21 dates.
    baseline_rows = rows(4, 25, 40)
    await seed("hrv", "last_night_avg", baseline_rows + rows(0, 3, 80))
    report = (await client.get(PATH, params={"days": 7})).json()
    finding = next(item for item in report["findings"] if item["metric"] == "hrv")
    evidence = finding["evidence"]
    assert finding["severity"] == "alert"
    assert evidence["recent_dates"] == sorted(day for day, _ in rows(0, 3, 80))
    assert evidence["baseline_dates"] == sorted(day for day, _ in baseline_rows)
    assert evidence["recent_n"] == 3 and evidence["baseline_n"] == 21
    assert evidence["recent_avg"] == 80 and evidence["baseline_mean"] == 40 and evidence["baseline_sd"] == 0
    assert evidence["z_score"] is finding["data"]["z_score"] is None
    assert evidence["assigned_rule_score"] == 3
    assert evidence["z_score_kind"] == "constant_baseline_rule_flag"
    assert "not an estimated z-score" in finding["message"]


async def test_variable_baseline_unchanged_rule_and_stale_recent_findings_suppressed(client):
    await seed("hrv", "last_night_avg", [(day, 40 + i % 2) for i, (day, _) in enumerate(rows(3, 24, 0))] + rows(0, 3, 80))
    report = (await client.get(PATH)).json()
    finding = next(item for item in report["findings"] if item["metric"] == "hrv")
    assert finding["evidence"]["z_score"] > 3 and finding["evidence"]["assigned_rule_score"] is None
    # Latest date alone is insufficient: all three recent observations must be fresh.
    await seed("resting_hr", "value", rows(6, 27, 40) + rows(4, 6, 80) + rows(0, 1, 80))
    report = (await client.get(PATH)).json()
    assert not any(finding["metric"] == "resting_hr" for finding in report["findings"])


async def test_readiness_existing_formula_weights_missing_inputs_and_staleness(client):
    input_rows = rows(0, 7, 80)
    await seed("sleep", "sleep_score", input_rows)
    report = (await client.get(PATH)).json()
    readiness = report["readiness"]
    expected = score_readiness({"sleep_score": [{"date": day, "value": value} for day, value in reversed(input_rows)]})
    assert {key: readiness[key] for key in expected} == expected
    assert readiness["freshness"] == "current"
    assert readiness["component_details"]["sleep_score"]["effective_weight"] == 1
    assert readiness["component_details"]["hrv"]["available"] is False
    await seed("hrv", "last_night_avg", rows(4, 12, 40))
    readiness = (await client.get(PATH)).json()["readiness"]
    assert readiness["freshness"] == "stale"
    assert readiness["component_details"]["hrv"]["freshness"] == "stale"
    assert any("does not describe current health" in note for note in readiness["notes"])
    assert readiness["component_details"]["hrv"]["effective_weight"] == pytest.approx(4 / 7)


@pytest.mark.parametrize("authentication", ["session", "bearer"])
async def test_view_auth_grants_archive_and_profile_isolation(client, authentication):
    user = await seed_user("viewer")
    primary = await get_primary_person_id()
    await grant_person(primary, user, "view")
    other = await seed_person("other")
    archived = await seed_person("archived")
    await grant_person(archived, user, "view")
    await seed("steps", "value", rows(0, 1, 10), primary)
    await seed("steps", "value", rows(0, 1, 99999), other)
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), archived))
        await db.commit()
    finally:
        await db.close()
    assert (await client.get(PATH)).status_code == 401
    if authentication == "session":
        client.cookies.set("vf_session", create_session_cookie("viewer", user, 1))
    else:
        _, token = await seed_token(user)
        client.headers["Authorization"] = f"Bearer {token}"
    response = await client.get(PATH)
    assert response.status_code == 200 and "99999" not in response.text
    for slug in ("other", "archived", "missing"):
        assert (await client.get(f"/p/{slug}/api/comparisons")).status_code == 404


async def test_admin_access_and_local_only_private_snapshot_once(client, monkeypatch):
    admin = await seed_user("admin", role="admin")
    client.cookies.set("vf_session", create_session_cookie("admin", admin, 1))
    other = await seed_person("other")
    await seed("steps", "value", rows(0, 1, 42), other)
    original = comparisons.metric_snapshot
    calls = []

    async def snapshot(person_id, days):
        calls.append((person_id, days))
        return await original(person_id, days)

    async def forbidden(*args, **kwargs):
        pytest.fail("Comparisons must not contact Garmin, LLM, or fetch other metric snapshots")

    monkeypatch.setattr(comparisons, "metric_snapshot", snapshot)
    monkeypatch.setattr(garmin_registry, "call", forbidden)
    monkeypatch.setattr(recommendations, "get_all_metrics", forbidden)
    monkeypatch.setattr(recommendations, "get_recommendations", forbidden)
    response = await client.get("/p/other/api/comparisons?days=90")
    assert response.status_code == 200
    assert response.json()["metrics"]["steps"]["current"]["mean"] == 42
    assert calls == [(other, 180)]
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"


async def test_comparisons_read_one_wal_snapshot(client, monkeypatch):
    await seed("hrv", "last_night_avg", rows(0, 14, 40))
    await seed("steps", "value", rows(0, 14, 10))
    db = await get_db()
    try:
        await db.execute("PRAGMA journal_mode=WAL")
    finally:
        await db.close()

    class ConcurrentConnection:
        def __init__(self, db):
            self.db, self.changed = db, False

        async def execute(self, sql, *args):
            cursor = await self.db.execute(sql, *args)
            if sql.startswith("SELECT") and not self.changed:
                self.changed = True
                await seed("hrv", "last_night_avg", rows(0, 14, 999))
                await seed("steps", "value", rows(0, 14, 888))
            return cursor

        async def close(self):
            await self.db.close()

    async def concurrent_get_db():
        return ConcurrentConnection(await get_db())

    monkeypatch.setattr(data_quality, "get_db", concurrent_get_db)
    report = (await client.get(PATH, params={"days": 7})).json()
    assert report["metrics"]["hrv"]["current"]["mean"] == 40
    assert report["metrics"]["steps"]["current"]["mean"] == 10
    assert report["readiness"]["components"]["hrv"] == 50
