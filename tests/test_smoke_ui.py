"""Playwright smoke tests for the two PWA pages.

Not full e2e coverage — just enough to catch what already broke once (see
`f658cc6`, "fix weight page JS syntax error"): does the page load, render its
core elements, and run its inline JS without throwing? Both live servers use
the same faked DB/Garmin fixtures as the HTTP API tests (see conftest.py),
so no real Garmin account or `/app/data` access is involved.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

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
