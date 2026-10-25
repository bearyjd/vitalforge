"""Browser checks use only the fixture's isolated database and fake Garmin client."""

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import shared.database
from tests.conftest import PERSON_PREFIX

pytestmark = pytest.mark.playwright


def _seed_history():
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        for age in range(-1, 31):
            date = (today - timedelta(days=age)).isoformat()
            # Include a missing calendar date, the old endpoint's extra boundary,
            # and a future row to expose accidental reuse of legacy chart series.
            if age == 3:
                continue
            value = 10000 if age == -1 else 50 + age % 5
            connection.execute("INSERT INTO hrv (person_id, date, last_night_avg) VALUES (?, ?, ?)", (person, date, value))
            connection.execute("INSERT INTO resting_hr (person_id, date, value) VALUES (?, ?, ?)", (person, date, value))
            connection.execute("INSERT INTO stress (person_id, date, avg_level) VALUES (?, ?, ?)", (person, date, value))
            connection.execute("INSERT INTO sleep (person_id, date, duration_seconds, sleep_score) VALUES (?, ?, ?, ?)", (person, date, 28800 + age * 60, value))
            connection.execute("INSERT INTO weight_history (person_id, date, weight_grams, muscle_mass_g, body_fat) VALUES (?, ?, ?, ?, ?)", (person, date, 75000 + age * 100, 30000 + age * 10, 20))
            connection.execute("INSERT INTO body_battery (person_id, date, highest, lowest) VALUES (?, ?, ?, ?)", (person, date, 80, 20))
    return today


def _chart(page, canvas):
    return page.evaluate("id => { const c = Chart.getChart(id); return c ? {labels: c.data.labels, datasets: c.data.datasets.map(d => ({label:d.label, data:d.data, fill:d.fill, spanGaps:d.spanGaps}))} : null; }", canvas)


def _wait_report(page, days):
    page.wait_for_function("days => !document.getElementById('comparisonTable').hidden && document.getElementById('comparisonWindows').textContent.includes(`(${days} calendar days)`)", arg=days)
    if page.locator("#personalComparisons").get_attribute("open") is None:
        page.locator("#personalComparisons summary").click()


def test_real_comparison_bands_calendar_gaps_units_and_raw_export(page, dashboard_live_server):
    today = _seed_history()
    page.add_init_script("localStorage.setItem('vf_unit', 'kg')")
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.get_by_role("button", name="7d", exact=True).click()
    _wait_report(page, 7)
    report = page.request.get(f"{dashboard_live_server}{PERSON_PREFIX}/api/comparisons?days=7").json()
    assert report["metrics"]["hrv"]["current"]["n"] == 6
    for canvas, metric, factor in [
        ("chartHrv", "hrv", 1), ("chartRhr", "resting_hr", 1),
        ("chartStress", "stress", 1), ("chartSleepScore", "sleep_score", 1),
        ("chartSleepDuration", "sleep_duration", 1 / 3600),
    ]:
        chart = _chart(page, canvas)
        evidence = report["metrics"][metric]
        assert chart["labels"] == [point["date"] for point in evidence["data"]]
        assert len(chart["labels"]) == 7
        assert chart["labels"][-1] == today.isoformat()
        assert chart["datasets"][0]["data"] == [None if point["value"] is None else point["value"] * factor for point in evidence["data"]]
        assert chart["datasets"][0]["spanGaps"] is False
        assert chart["datasets"][1]["data"][3] is None
        values = chart["datasets"][0]["data"]
        expected_means = []
        for index, value in enumerate(values):
            observed = [item for item in values[max(0, index - 6):index + 1] if item is not None]
            expected_means.append(sum(observed) / len(observed) if value is not None else None)
        assert chart["datasets"][1]["data"] == pytest.approx(expected_means)
        assert chart["datasets"][2]["data"] == pytest.approx([evidence["baseline"]["lower"] * factor] * 7)
        assert chart["datasets"][3]["data"] == pytest.approx([evidence["baseline"]["upper"] * factor] * 7)
        assert chart["datasets"][3]["fill"] == "-1"
    for canvas in ("chartWeight", "chartBodyFat", "chartBodyBattery"):
        chart = _chart(page, canvas)
        assert len(chart["labels"]) == 7
        assert chart["datasets"][0]["data"][3] is None
        assert chart["datasets"][1]["data"][3] is None
    weight = page.locator("#comparisonRows tr[data-metric=weight]")
    mass = page.locator("#comparisonRows tr[data-metric=muscle_mass]")
    assert "kg" in weight.inner_text()
    assert "kg" in mass.inner_text()
    kg_mean = report["metrics"]["weight"]["current"]["mean"] / 1000
    assert float(weight.locator("td").nth(1).inner_text().split()[0]) == pytest.approx(kg_mean, abs=.01)
    page.get_by_role("button", name="lbs", exact=True).click()
    page.wait_for_function("document.querySelector('#comparisonRows tr[data-metric=weight]')?.textContent.includes('lbs')")
    assert float(weight.locator("td").nth(1).inner_text().split()[0]) == pytest.approx(kg_mean * 1000 / 453.592, abs=.01)
    with page.expect_download() as download:
        page.get_by_role("link", name="Download AI-ready JSON", exact=True).click()
    package = json.loads(download.value.path().read_text())
    assert package["dictionary"]["weight"]["unit"] == "g"
    assert package["dictionary"]["sleep_duration"]["unit"] == "seconds"
    assert any(point["value"] == 75000 for point in package["data"]["weight"])
    page.locator("#readinessEvidence summary").click()
    assert "HRV 40%" in page.locator("#readinessExplanation").inner_text()
    assert "observation windows" in page.locator("#readinessExplanation").inner_text()


def _report(days, value=70, *, status="available", freshness="current", baseline=True):
    today = datetime.now(timezone.utc).date()
    current = {"start_date": (today - timedelta(days=days - 1)).isoformat(), "end_date": today.isoformat(), "days": days}
    previous = {"start_date": (today - timedelta(days=days * 2 - 1)).isoformat(), "end_date": (today - timedelta(days=days)).isoformat(), "days": days}
    metric = dict(label="HRV <script>window.bad=true</script>", unit="ms", expected_cadence="daily", latest_observation_date=today.isoformat(), freshness=freshness,
                  current={"n": days, "mean": value}, previous={"n": days, "mean": 60}, difference=value - 60, percent_change=16.67, status=status, minimum_samples=3,
                  baseline={"method": "previous_period_mean_plus_minus_sample_sd", "n": days, "mean": 60, "sample_sd": 5, "lower": 55, "upper": 65} if baseline else None,
                  data=[{"date": (today - timedelta(days=age)).isoformat(), "value": value} for age in reversed(range(days))])
    snapshot = dict(metric, label="Repeated estimate", unit="g", expected_cadence="event_or_snapshot", status="snapshot_only")
    return dict(generated_at=today.isoformat(), current_period=current, previous_period=previous, metrics={"hrv": metric, "muscle_mass": snapshot},
                findings_period=dict(current, days=30, start_date=(today - timedelta(days=29)).isoformat()), findings=[],
                readiness={"score": 75, "components": {"hrv": 75, "rhr": None, "sleep_score": None}, "status": "partial_data", "freshness": freshness, "explanation": "Partial observations", "notes": ["Only HRV is available"], "component_details": {}}, notes=[])


def test_global_generation_guard_covers_legacy_charts_and_comparisons(page, dashboard_live_server):
    pending = []

    def compare(route):
        days = int(parse_qs(urlsplit(route.request.url).query)["days"][0])
        if days == 30:
            pending.append((route, _report(days, 30)))
        else:
            route.fulfill(json=_report(days, 77))

    def steps(route):
        days = int(parse_qs(urlsplit(route.request.url).query)["days"][0])
        response = {"data": [{"date": "2026-10-25", "value": days}]}
        if days == 30:
            pending.append((route, response))
        else:
            route.fulfill(json=response)

    page.route("**/api/comparisons?*", compare)
    page.route("**/api/metrics/steps?*", steps)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.wait_for_function("document.getElementById('syncInfo').textContent !== 'Loading...'")
    page.get_by_role("button", name="7d", exact=True).click()
    _wait_report(page, 7)
    assert _chart(page, "chartSteps")["datasets"][0]["data"] == [7]
    for route, payload in pending:
        route.fulfill(json=payload)
    page.wait_for_function("performance.getEntriesByType('resource').filter(r => r.name.includes('/comparisons?days=30')).length > 0")
    assert _chart(page, "chartSteps")["datasets"][0]["data"] == [7]
    assert _chart(page, "chartHrv")["datasets"][0]["data"] == [77] * 7
    assert "(7 calendar days)" in page.locator("#comparisonWindows").inner_text()


def test_comparison_error_insufficient_stale_and_safe_snapshot_cleanup(page, dashboard_live_server):
    today = _seed_history()
    def handle(route):
        days = int(parse_qs(urlsplit(route.request.url).query)["days"][0])
        if days == 90:
            route.fulfill(status=503, json={"detail": "unavailable"})
        else:
            route.fulfill(json=_report(days, status="insufficient_data" if days == 7 else "available", freshness="stale" if days == 7 else "current", baseline=days != 7))

    page.route("**/api/comparisons?*", handle)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    assert len(_chart(page, "chartHrv")["datasets"]) == 4
    assert page.evaluate("window.bad === undefined")
    snapshot = page.locator("#comparisonRows tr[data-metric=muscle_mass]")
    assert "Stored snapshots may repeat measurements; no change assessment or band" in snapshot.inner_text()
    assert snapshot.locator("td").nth(5).inner_text() == "Stored row counts: current n=30; previous n=30"
    assert "minimum" not in snapshot.inner_text()
    assert snapshot.locator("td").nth(3).inner_text() == "—"
    page.get_by_role("button", name="7d", exact=True).click()
    _wait_report(page, 7)
    assert "insufficient data · stale" in page.locator("#comparisonRows tr[data-metric=hrv]").inner_text()
    assert len(_chart(page, "chartHrv")["datasets"]) == 2
    assert page.locator("#readinessScoreValue").inner_text() == "—"
    assert "stale" in page.locator("#readinessStatus").inner_text()
    page.get_by_role("button", name="90d", exact=True).click()
    page.wait_for_function("document.getElementById('comparisonStatus').textContent.includes('unavailable')")
    assert page.locator("#comparisonTable").is_hidden()
    assert page.locator("#comparisonRows").inner_text() == ""
    assert page.locator("#comparisonFindings").inner_text() == ""
    fallback = _chart(page, "chartHrv")
    assert len(fallback["datasets"]) == 2
    assert len(fallback["labels"]) == 90
    assert fallback["labels"][-1] == today.isoformat()
    assert fallback["datasets"][0]["data"][-4] is None
    assert 10000 not in fallback["datasets"][0]["data"]
    assert "unavailable" in page.locator("#readinessStatus").inner_text()


def test_comparisons_mobile_keeps_evidence_scroll_inside_page(page, dashboard_live_server):
    _seed_history()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    assert page.locator("#comparisonHeading").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator("#personalComparisons .quality-scroll").evaluate("el => el.scrollWidth > el.clientWidth")


def test_unit_request_race_cannot_restore_old_chart_or_evidence(page, dashboard_live_server):
    pending = []
    requests = []

    def handle(route):
        requests.append(route.request.url)
        if len(requests) == 2:
            pending.append(route)
        else:
            route.fulfill(json=_report(30, value=77 if len(requests) == 1 else 88))

    page.add_init_script("localStorage.setItem('vf_unit', 'lbs')")
    page.route("**/api/comparisons?*", handle)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    page.get_by_role("button", name="kg", exact=True).click()
    page.wait_for_function("document.getElementById('comparisonTable').hidden")
    page.get_by_role("button", name="lbs", exact=True).click()
    _wait_report(page, 30)
    assert _chart(page, "chartHrv")["datasets"][0]["data"] == [88] * 30
    assert len(pending) == 1
    pending[0].fulfill(json=_report(30, value=10))
    page.wait_for_function("performance.getEntriesByType('resource').filter(r => r.name.includes('/comparisons?')).length >= 3")
    assert _chart(page, "chartHrv")["datasets"][0]["data"] == [88] * 30
    assert "lbs" in page.locator("#comparisonRows tr[data-metric=muscle_mass]").inner_text()


def test_sync_clears_evidence_and_refreshes_when_complete(page, dashboard_live_server):
    pending = []
    reports = []

    def comparison(route):
        reports.append(route.request.url)
        route.fulfill(json=_report(30, value=77 if len(reports) == 1 else 88))

    page.route("**/api/comparisons?*", comparison)
    page.route("**/api/sync", lambda route: pending.append(route))
    page.route("**/api/sync/status", lambda route: route.fulfill(json={"syncing": False}))
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    page.get_by_role("button", name="Sync", exact=True).click()
    assert page.locator("#comparisonTable").is_hidden()
    assert _chart(page, "chartHrv") is None
    assert "Sync requested" in page.locator("#comparisonStatus").inner_text()
    assert len(pending) == 1
    pending[0].fulfill(json={"status": "started"})
    _wait_report(page, 30)
    assert _chart(page, "chartHrv")["datasets"][0]["data"] == [88] * 30


def test_real_constant_baseline_anomaly_evidence_is_not_a_z_score(page, dashboard_live_server):
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        for age in range(24):
            date = (today - timedelta(days=age)).isoformat()
            connection.execute("INSERT INTO resting_hr (person_id, date, value) VALUES (?, ?, ?)", (person, date, 85 if age < 3 else 60))
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    findings = page.locator("#comparisonFindings").inner_text()
    assert "Recent observations (3)" in findings
    assert "baseline observations (21)" in findings
    assert "Constant baseline rule flag 3" in findings
    assert "mathematical z-score is undefined" in findings
    page.get_by_role("button", name="7d", exact=True).click()
    _wait_report(page, 7)
    assert "(30 calendar days)" in page.locator("#findingsPeriod").inner_text()
    assert "Recent observations (3)" in page.locator("#comparisonFindings").inner_text()


@pytest.mark.parametrize("outcome", ["link_required", "conflict", "network"])
def test_rejected_sync_restores_dashboard_with_fresh_report(page, dashboard_live_server, outcome):
    reports = []

    def comparison(route):
        reports.append(route.request.url)
        route.fulfill(json=_report(30, value=77 if len(reports) == 1 else 88))

    def sync(route):
        if outcome == "network":
            route.abort("failed")
        elif outcome == "link_required":
            route.fulfill(json={"status": "link_required", "message": "Link Garmin before syncing"})
        else:
            route.fulfill(status=409, json={"detail": "Sync is already running"})

    page.route("**/api/comparisons?*", comparison)
    page.route("**/api/sync", sync)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    _wait_report(page, 30)
    page.get_by_role("button", name="Sync", exact=True).click()
    page.wait_for_function("Chart.getChart('chartHrv')?.data.datasets[0].data[0] === 88")
    assert page.get_by_role("button", name="Sync", exact=True).is_enabled()
    assert len(reports) == 2
    expected = {"link_required": "Link Garmin before syncing", "conflict": "Sync is already running", "network": "Sync not available"}
    assert page.locator("#syncInfo").inner_text() == expected[outcome]


def test_extreme_finite_values_do_not_overflow_observed_means(page, dashboard_live_server):
    def compare(route):
        days = int(parse_qs(urlsplit(route.request.url).query)["days"][0])
        route.fulfill(json=_report(days, value=1e308, baseline=False, status="insufficient_data"))

    page.route("**/api/comparisons?*", compare)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.get_by_role("button", name="7d", exact=True).click()
    _wait_report(page, 7)
    rolling = _chart(page, "chartHrv")["datasets"][1]["data"]
    assert all(value is not None and math.isfinite(value) for value in rolling)
    assert rolling == pytest.approx([1e308] * 7)
