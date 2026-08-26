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


def _collect_console_errors(page):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


@pytest.mark.playwright
def test_weight_page_loads_without_console_errors(page, weight_live_server):
    errors = _collect_console_errors(page)

    page.goto(weight_live_server)
    page.wait_for_selector("#recentList li")

    assert "VitalForge" in page.title()
    assert page.locator("#weightInput").is_visible()
    assert page.locator("#submitBtn").is_visible()
    assert page.locator("#recentList .empty-state").inner_text() == "No weigh-ins yet"
    assert errors == []


@pytest.mark.playwright
def test_weight_page_logs_an_entry(page, weight_live_server):
    page.goto(weight_live_server)
    page.wait_for_selector("#recentList li")

    page.locator("#weightInput").fill("175.5")
    page.locator("#submitBtn").click()

    entry = page.locator("#recentList .recent-item").first
    entry.wait_for(state="visible")
    assert "175.5" in entry.locator(".recent-weight").inner_text()


@pytest.mark.playwright
def test_dashboard_page_loads_without_console_errors(page, dashboard_live_server):
    errors = _collect_console_errors(page)

    page.goto(dashboard_live_server)
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
    running event loop" -- a synchronous connection sidesteps that.
    """
    conn = sqlite3.connect(str(shared.database.DB_PATH))
    try:
        today = datetime.now(timezone.utc)
        for n in range(5):
            date = (today - timedelta(days=n)).strftime("%Y-%m-%d")
            conn.execute("INSERT INTO resting_hr (date, value) VALUES (?, ?)", (date, 55 + n))
            conn.execute("INSERT INTO steps (date, value) VALUES (?, ?)", (date, 8000 + n * 100))
        conn.execute("INSERT INTO resting_hr (date, value) VALUES (?, ?)", ("not-a-date", 999))
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
    page.goto(dashboard_live_server)
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
