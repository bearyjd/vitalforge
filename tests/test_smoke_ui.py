"""Playwright smoke tests for the two PWA pages.

Not full e2e coverage — just enough to catch what already broke once (see
`f658cc6`, "fix weight page JS syntax error"): does the page load, render its
core elements, and run its inline JS without throwing? Both live servers use
the same faked DB/Garmin fixtures as the HTTP API tests (see conftest.py),
so no real Garmin account or `/app/data` access is involved.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import shared.database
from tests.conftest import PERSON_PREFIX


def _collect_console_errors(page):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


@pytest.mark.playwright
def test_weight_page_loads_without_console_errors(page, weight_live_server):
    errors = _collect_console_errors(page)

    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#recentList li")

    assert "VitalForge" in page.title()
    assert page.locator("#weightInput").is_visible()
    assert page.locator("#submitBtn").is_visible()
    assert page.locator("#recentList .empty-state").inner_text() == "No weigh-ins yet"
    assert errors == []


@pytest.mark.playwright
def test_weight_page_logs_an_entry(page, weight_live_server):
    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#recentList li")

    page.locator("#weightInput").fill("175.5")
    page.locator("#submitBtn").click()

    entry = page.locator("#recentList .recent-item").first
    entry.wait_for(state="visible")
    assert "175.5" in entry.locator(".recent-weight").inner_text()


@pytest.mark.playwright
def test_dashboard_page_loads_without_console_errors(page, dashboard_live_server):
    errors = _collect_console_errors(page)

    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.wait_for_function("document.getElementById('syncInfo').textContent !== 'Loading...'")

    assert "VitalForge" in page.title()
    assert page.locator("#cardWeight").is_visible()
    assert page.locator("#syncBtn").is_visible()
    assert errors == []


@pytest.mark.playwright
def test_dashboard_download_json_follows_profile_and_range(page, dashboard_live_server):
    """Download actual files and check range filtering and profile isolation."""
    today = datetime.now(timezone.utc)
    expected_steps = []
    with sqlite3.connect(str(shared.database.DB_PATH)) as conn:
        person_id = conn.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        other_id = conn.execute(
            "INSERT INTO persons (slug, display_name, created_at) VALUES (?, ?, ?)",
            ("export-other", "Other profile", today.isoformat()),
        ).lastrowid
        for age, value in [(2, 0), (14, 6400), (60, 4100), (100, 2300)]:
            date = (today - timedelta(days=age)).strftime("%Y-%m-%d")
            conn.execute("INSERT INTO steps (person_id, date, value) VALUES (?, ?, ?)", (person_id, date, value))
            expected_steps.append((age, {"metric": "steps", "date": date, "value": value}))
        recent_date = expected_steps[0][1]["date"]
        conn.execute(
            "INSERT INTO steps (person_id, date, value) VALUES (?, ?, ?)", (other_id, recent_date, 999999)
        )
        conn.execute(
            "INSERT INTO weight_history (person_id, date, weight_grams) VALUES (?, ?, ?)",
            (person_id, recent_date, 75000),
        )
        conn.execute(
            "INSERT INTO sleep (person_id, date, duration_seconds) VALUES (?, ?, ?)",
            (person_id, recent_date, 28800),
        )

    # The default link is useful even before the page's JavaScript runs.
    response = page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    assert f'href="{PERSON_PREFIX}/api/export?metric=all&amp;format=json&amp;days=30"' in response.text()
    export_link = page.get_by_role("link", name="Download JSON", exact=True)
    assert export_link.is_visible()
    for days in (30, 7, 90):
        if days != 30:
            page.get_by_role("button", name=f"{days}d", exact=True).click()
        assert page.locator("#exportRange").inner_text() == f"All metrics for this profile from the last {days} days."
        assert export_link.get_attribute("aria-describedby") == "exportRange exportHelp"
        with page.expect_download() as download_info:
            export_link.click()
        download = download_info.value
        assert download.suggested_filename == f"vitalforge-export-all-{days}d.json"
        request_url = urlsplit(download.url)
        assert request_url.path == f"{PERSON_PREFIX}/api/export"
        assert parse_qs(request_url.query) == {"metric": ["all"], "format": ["json"], "days": [str(days)]}
        records = json.loads(download.path().read_text())
        expected = [record for age, record in expected_steps if age <= days] + [
            {"metric": "weight", "date": recent_date, "value": 75000},
            {"metric": "sleep_duration", "date": recent_date, "value": 28800},
        ]
        assert sorted(records, key=lambda row: (row["metric"], row["date"])) == sorted(
            expected, key=lambda row: (row["metric"], row["date"])
        )

        page.get_by_role("button", name="kg", exact=True).click()
        package_link = page.get_by_role("link", name="Download AI-ready JSON", exact=True)
        assert parse_qs(urlsplit(package_link.get_attribute("href")).query) == {"days": [str(days)]}
        with page.expect_download() as package_info:
            package_link.click()
        package_download = package_info.value
        assert package_download.suggested_filename == f"vitalforge-ai-export-{days}d.json"
        package = json.loads(package_download.path().read_text())
        assert package["period"]["days"] == days
        assert package["quality"]["period"] == package["period"]
        assert set(package["data"]) == set(package["dictionary"]) == set(package["quality"]["metrics"])
        assert len(package["dictionary"]) == 16
        assert package["quality"]["metrics"]["steps"]["observed_days"] == len([age for age, _ in expected_steps if age < days])
        assert package["dictionary"]["weight"]["unit"] == "g"
        assert package["dictionary"]["sleep_duration"]["unit"] == "seconds"
        enriched_records = [dict(metric=name, **record) for name, series in package["data"].items() for record in series]
        assert sorted(enriched_records, key=lambda row: (row["metric"], row["date"])) == sorted(
            expected, key=lambda row: (row["metric"], row["date"])
        )


@pytest.mark.playwright
def test_dashboard_quality_handles_zero_stale_snapshot_failure_and_races(page, dashboard_live_server):
    pending = []

    def report(days):
        def metric(name, **values):
            return dict(label=name, unit="count", expected_cadence="daily",
                        latest_observation_date=None, observed_days=0, missing_days=days,
                        coverage_percent=0, availability="empty", completeness="empty",
                        freshness="unknown", **values)

        recent = metric("Zero steps")
        recent.update(latest_observation_date="2026-10-25", observed_days=1,
                      missing_days=days - 1, coverage_percent=14.3,
                      availability="available", completeness="partial", freshness="current")
        stale = metric("Old pulse")
        stale.update(latest_observation_date="2026-10-01", availability="available", freshness="stale")
        snapshot = metric("Snapshot")
        snapshot.update(expected_cadence="event", availability="available")
        return dict(period=dict(start_date="2026-10-19", end_date="2026-10-25", days=days),
                    sync=dict(last_attempt_at="2026-10-25T10:00:00Z", last_result="error",
                              status="idle", metric_outcomes="unknown"),
                    metrics=dict(steps=recent, pulse=stale, hrv=metric("No HRV"), snapshot=snapshot))

    def handle(route):
        days = int(parse_qs(urlsplit(route.request.url).query)["days"][0])
        if days == 30:
            pending.append(route)
        elif days == 90:
            route.fulfill(status=503, json={"detail": "unavailable"})
        else:
            route.fulfill(json=report(days))

    page.route("**/api/data-quality?*", handle)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.wait_for_function("document.getElementById('syncInfo').textContent !== 'Loading...'")
    page.locator("#dataQuality summary").click()
    page.get_by_role("button", name="7d", exact=True).click()
    page.wait_for_function("document.querySelectorAll('#qualityRows tr').length === 4")
    pending[0].fulfill(json=report(30))
    page.wait_for_timeout(100)
    assert "7 days" in page.locator("#qualityStatus").inner_text()
    rows = page.locator("#qualityRows tr")
    assert "1 / 7 days" in rows.nth(0).inner_text()
    assert "Recent" in rows.nth(0).inner_text()
    assert "Stale" in rows.nth(1).inner_text()
    assert "No observations / No observations" in rows.nth(2).inner_text()
    assert "Not assessed (snapshot)" in rows.nth(3).inner_text()
    assert "Last sync attempt:" in page.locator("#qualitySync").inner_text()
    assert "Per-metric sync results are unknown" in page.locator("#qualitySync").inner_text()
    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="90d", exact=True).click()
    page.wait_for_function("document.getElementById('qualityStatus').textContent.includes('unavailable')")
    assert page.locator("#qualityTable").is_hidden()
    assert page.locator("#qualityRows tr").count() == 0


def _seed_resting_hr_and_steps_with_a_malformed_date():
    """A malformed date can reach a metric table the same way it reaches
    `weight_history` in `test_correlations_malformed_date_returns_200_and_excludes_row`
    (see `tests/test_correlations_api.py`) -- the server already tolerates
    this for the heatmap itself, but the drill-down scatter re-fetches raw
    series and aligns them client-side, which is what this test guards.

    Plain sqlite3 rather than the app's async get_db(): by the time this
    runs, dashboard_live_server's uvicorn thread already owns the async
    event loop, so asyncio.run() here would raise "cannot be called from a
    running event loop" -- a synchronous connection sidesteps that. The
    primary person's id is looked up on this same synchronous connection
    for the same reason -- shared.database.get_primary_person_id() is async
    and would hit the identical event-loop conflict.
    """
    conn = sqlite3.connect(str(shared.database.DB_PATH))
    try:
        person_id = conn.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        today = datetime.now(timezone.utc)
        for n in range(5):
            date = (today - timedelta(days=n)).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO resting_hr (person_id, date, value) VALUES (?, ?, ?)", (person_id, date, 55 + n)
            )
            conn.execute(
                "INSERT INTO steps (person_id, date, value) VALUES (?, ?, ?)", (person_id, date, 8000 + n * 100)
            )
        conn.execute(
            "INSERT INTO resting_hr (person_id, date, value) VALUES (?, ?, ?)", (person_id, "not-a-date", 999)
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.playwright
def test_correlations_drilldown_survives_a_malformed_date_with_nonzero_lag(page, dashboard_live_server):
    """Regression test for the client-side counterpart of the malformed-date
    bug fixed server-side in `correlations.py::align_series` (PR #27): with
    `lag != 0`, `correlations.js`'s own `shiftDate` used to throw
    `RangeError: Invalid time value` on a malformed date instead of skipping
    it, crashing the scatter drill-down the moment a user clicked a heatmap
    cell backed by such a row."""
    _seed_resting_hr_and_steps_with_a_malformed_date()

    errors = _collect_console_errors(page)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector(".corr-cell")

    lag_input = page.locator(".corr-field", has_text="Lag (days)").locator("input")
    lag_input.fill("1")
    lag_input.press("Tab")  # triggers the input's "change" listener

    cell = page.locator('.corr-cell[title^="resting_hr × steps"]')
    cell.wait_for(state="visible")
    cell.click()

    # showDrilldown is async: it sets the label, THEN awaits two fetches,
    # THEN calls alignForScatter (where the bug's RangeError used to throw),
    # THEN builds the Chart.js instance -- so neither "the label updated" nor
    # "#chartCorrScatter exists" (a static <canvas> in the page markup either
    # way) proves the function ran to completion without throwing. Poll for
    # an actual Chart.js instance being attached to that canvas instead,
    # which is only reached after alignForScatter returns successfully.
    page.wait_for_function(
        "typeof Chart !== 'undefined' "
        "&& Chart.getChart(document.getElementById('chartCorrScatter')) !== undefined",
        timeout=5000,
    )
    assert errors == []


def _seed_recent_weigh_ins(count: int = 3):
    """Enough recent weight_log rows for `/api/weight/trend` to return >= 2
    points, which is what makes the trend chart render at all. Same sync
    sqlite3 approach as `_seed_resting_hr_and_steps_with_a_malformed_date`
    and for the same event-loop reason."""
    conn = sqlite3.connect(str(shared.database.DB_PATH))
    try:
        person_id = conn.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        now = datetime.now(timezone.utc)
        for n in range(count):
            lbs = 180.0 - n
            conn.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (person_id, lbs, lbs * 0.45359237, round(lbs * 453.59237), (now - timedelta(days=n)).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.playwright
def test_weight_trend_chart_height_settles(page, weight_live_server):
    """The 30-day trend canvas must reach a fixed size and stay there.

    Chart.js with `maintainAspectRatio: false` sizes the canvas to its parent
    container's content height. If that container's height is itself derived
    from the canvas (canvas as a sibling of the section title, no explicit
    height), every resize grows the container, the ResizeObserver fires
    again, and the page grows without bound -- the "infinite scroll down"
    reported on prod. Chart.js's own docs require a dedicated, relatively
    positioned container for exactly this reason.
    """
    _seed_recent_weigh_ins()
    errors = _collect_console_errors(page)

    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#trendSection", state="visible")
    page.wait_for_function(
        "typeof Chart !== 'undefined' && Chart.getChart(document.getElementById('trendChart')) !== undefined",
        timeout=5000,
    )

    def canvas_height():
        return page.evaluate("document.getElementById('trendChart').getBoundingClientRect().height")

    page.wait_for_timeout(500)
    first = canvas_height()
    scroll_first = page.evaluate("document.documentElement.scrollHeight")
    page.wait_for_timeout(2000)
    second = canvas_height()
    scroll_second = page.evaluate("document.documentElement.scrollHeight")

    wrap_height = page.evaluate(
        "getComputedStyle(document.querySelector('.trend-chart-wrap')).height"
    )

    assert first > 0
    assert second == first, f"trend canvas kept growing: {first}px -> {second}px"
    assert scroll_second == scroll_first, f"page kept growing: {scroll_first}px -> {scroll_second}px"
    assert second < 400, f"trend canvas is {second}px tall; it should be a short strip"
    # Dedicating the wrapper stops the runaway; the fixed height is what makes
    # the settled size deterministic, and so what makes `second == first` above
    # a reliable assertion rather than a lucky one. Without it the canvas still
    # settles, just at whatever the layout happens to give it.
    assert wrap_height == "140px", f"trend chart wrapper must keep its fixed height, got {wrap_height}"
    assert errors == []


def _chart_values(page):
    return page.evaluate("Chart.getChart(document.getElementById('trendChart')).data.datasets[0].data")


def _wait_for_chart(page):
    page.wait_for_selector("#trendSection", state="visible")
    page.wait_for_function(
        "typeof Chart !== 'undefined' && Chart.getChart(document.getElementById('trendChart')) !== undefined",
        timeout=5000,
    )


@pytest.mark.playwright
def test_weight_unit_toggle_refreshes_recent_list_and_trend_chart(page, weight_live_server):
    """Issue #76: both loaders read `currentUnit` only when they render, so the
    toggle has to re-run them -- otherwise the Recent list and the chart keep
    the previous unit's numbers under the newly selected unit."""
    _seed_recent_weigh_ins()
    errors = _collect_console_errors(page)

    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#recentList .recent-item")
    _wait_for_chart(page)

    first_weight = page.locator("#recentList .recent-weight").first
    assert first_weight.inner_text() == "180 lbs"
    lbs_values = _chart_values(page)

    page.locator(".unit-btn[data-unit='kg']").click()
    page.wait_for_function(
        "document.querySelector('#recentList .recent-weight').textContent.trim().endsWith(' kg')",
        timeout=5000,
    )
    page.wait_for_function(
        "(prev) => JSON.stringify(Chart.getChart(document.getElementById('trendChart')).data.datasets[0].data)"
        " !== JSON.stringify(prev)",
        arg=lbs_values,
        timeout=5000,
    )

    kg_text = first_weight.inner_text()
    assert abs(float(kg_text.split()[0]) - 180.0 * 0.45359237) < 0.1, kg_text
    kg_values = _chart_values(page)
    assert len(kg_values) == len(lbs_values)
    for kg, lbs in zip(kg_values, lbs_values):
        assert abs(kg - lbs * 0.45359237) < 0.1, (kg_values, lbs_values)

    page.locator(".unit-btn[data-unit='lbs']").click()
    page.wait_for_function(
        "document.querySelector('#recentList .recent-weight').textContent.trim() === '180 lbs'",
        timeout=5000,
    )
    assert errors == []


@pytest.mark.playwright
def test_weight_delete_refreshes_trend_chart(page, weight_live_server):
    """Deleting a weigh-in reloaded the Recent list but left the deleted
    point on the trend chart until something else reloaded it."""
    _seed_recent_weigh_ins()
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#recentList .recent-item")
    _wait_for_chart(page)
    assert len(_chart_values(page)) == 3

    page.locator("#recentList .recent-item").first.locator(".delete-btn").click()
    page.wait_for_function(
        "Chart.getChart(document.getElementById('trendChart')).data.datasets[0].data.length === 2",
        timeout=5000,
    )

    # Below two points there is no trend: the section hides and the stale
    # chart is destroyed rather than left showing the deleted entry. Wait for
    # the Recent list to drop the first row before clicking the next one.
    page.wait_for_function("document.querySelectorAll('#recentList .recent-item').length === 2", timeout=5000)
    page.locator("#recentList .recent-item").first.locator(".delete-btn").click()
    page.wait_for_selector("#trendSection", state="hidden", timeout=5000)
    assert page.evaluate("Chart.getChart(document.getElementById('trendChart')) === undefined")


@pytest.mark.playwright
def test_weight_recent_list_ignores_a_late_stale_response(page, weight_live_server):
    """Toggle, delete and submit each start a reload; an older response that
    lands after a newer one must not overwrite it. The first post-load
    /weight/recent request is held, a second one renders, then the held one
    is released with a body the page must ignore."""
    _seed_recent_weigh_ins()
    page.goto(f"{weight_live_server}{PERSON_PREFIX}/")
    page.wait_for_selector("#recentList .recent-item")

    held = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    page.route("**/weight/recent", hold_first)
    # Count /weight/recent bodies the page has finished handling. The counter
    # bumps in a macrotask queued after json() resolves, so it runs only after
    # the loader's own continuation (a microtask) has rendered -- or bailed.
    page.evaluate(
        """() => {
            window.__recentHandled = 0;
            const json = Response.prototype.json;
            Response.prototype.json = async function () {
                const body = await json.call(this);
                if (this.url.endsWith("/weight/recent")) {
                    setTimeout(() => { window.__recentHandled += 1; }, 0);
                }
                return body;
            };
        }"""
    )

    page.locator(".unit-btn[data-unit='kg']").click()  # request A: held
    page.locator(".unit-btn[data-unit='lbs']").click()  # request B: renders
    # Wait for B to be HANDLED, not for "180 lbs" -- the first load already
    # shows that, so a text wait would release A before B lands and B would
    # then overwrite A for the wrong reason.
    page.wait_for_function("window.__recentHandled >= 1", timeout=5000)
    assert len(held) == 1, "the first reload was never intercepted"

    stale = '[{"id": 999, "weight_lbs": 999, "weight_kg": 453.1, "timestamp": "2026-01-01T00:00:00+00:00", "synced_to_garmin": false}]'
    held[0].fulfill(status=200, content_type="application/json", body=stale)
    page.wait_for_function("window.__recentHandled >= 2", timeout=5000)

    assert page.locator("#recentList .recent-weight").first.inner_text() == "180 lbs"
    assert page.locator("#recentList .recent-item").count() == 3
