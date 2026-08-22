"""Playwright smoke tests for the two PWA pages.

Not full e2e coverage — just enough to catch what already broke once (see
`f658cc6`, "fix weight page JS syntax error"): does the page load, render its
core elements, and run its inline JS without throwing? Both live servers use
the same faked DB/Garmin fixtures as the HTTP API tests (see conftest.py),
so no real Garmin account or `/app/data` access is involved.
"""

import pytest


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
