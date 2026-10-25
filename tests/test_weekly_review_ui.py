"""Weekly review browser checks use only the fixture's isolated database and fake Garmin client."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import shared.database
from tests.conftest import PERSON_PREFIX

pytestmark = pytest.mark.playwright

PATTERN = "**/api/weekly-review"
SECTIONS = ("changes", "findings", "coverage", "goals", "follow-up")


def _seed_weeks():
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        for age in range(14):
            value = 80 if age < 7 else 40
            connection.execute("INSERT INTO hrv (person_id, date, last_night_avg) VALUES (?, ?, ?)",
                               (person, (today - timedelta(days=age)).isoformat(), value))


def _open(page, base):
    page.goto(f"{base}{PERSON_PREFIX}/")
    page.locator("#weeklyReview summary").click()


def test_review_is_fetched_only_on_click_and_renders_all_sections(page, dashboard_live_server):
    _seed_weeks()
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "/api/weekly-review" in request.url else None)
    _open(page, dashboard_live_server)
    page.wait_for_load_state("networkidle")
    assert requests == []
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    assert len(requests) == 1
    for name in SECTIONS:
        assert page.locator(f"#weeklyReviewOutput section[data-section={name}]").count() == 1
    assert "Overnight HRV" in page.locator("#weeklyReviewOutput section[data-section=changes]").inner_text()


def test_empty_week_message(page, dashboard_live_server):
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewStatus').textContent.includes('No data recorded this week')")
    assert page.locator("#weeklyReviewOutput section[data-section=follow-up]").count() == 1


def test_markup_in_labels_renders_as_text(page, dashboard_live_server):
    hostile = "<img src=x onerror=window.bad=true><script>window.bad=true</script>"
    period = {"start_date": "2026-10-19", "end_date": "2026-10-25", "days": 7}
    side = {"start_date": "2026-10-19", "end_date": "2026-10-25", "n": 7, "mean": 1, "dates": ["2026-10-25"]}
    report = {
        "schema_version": "1.0", "generated_at": "now", "period": period, "previous_period": period,
        "empty": False, "message": None, "sync": {"status": "success", "last_result": "success", "last_attempt_at": None},
        "changes": [{"metric": "hrv", "label": hostile, "unit": "ms", "current": side, "previous": side, "difference": 1, "percent_change": 1}],
        "findings": [{"label": hostile, "message": hostile, "period": period, "observation_dates": ["2026-10-25"]}],
        "stable": [], "coverage": [], "goals": [], "follow_up": {"kind": "none", "text": hostile, "metrics": []}, "limitations": [hostile],
    }
    page.route(PATTERN, lambda route: route.fulfill(json=report))
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    assert page.locator("#weeklyReviewOutput img, #weeklyReviewOutput script").count() == 0
    assert hostile in page.locator("#weeklyReviewOutput").inner_text()
    assert page.evaluate("window.bad === undefined")


def _report(label):
    period = {"start_date": "2026-10-19", "end_date": "2026-10-25", "days": 7}
    return {
        "schema_version": "1.0", "generated_at": "now", "period": period, "previous_period": period,
        "empty": False, "message": None, "sync": {"status": "success", "last_result": "success", "last_attempt_at": None},
        "changes": [], "stable": [], "findings": [{"label": label, "message": label, "period": period, "observation_dates": ["2026-10-25"]}],
        "coverage": [], "goals": [], "follow_up": {"kind": "none", "text": "ok", "metrics": []}, "limitations": [],
    }


def test_stale_response_arriving_after_a_newer_one_is_ignored(page, dashboard_live_server):
    held = []

    def handler(route):
        if not held:
            held.append(route)  # first request stays pending
        else:
            route.fulfill(json=_report("NEWER"))

    page.route(PATTERN, handler)
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    while not held:
        page.wait_for_timeout(50)
    page.evaluate("window.VFWeeklyReview.generate(window.VF_API_BASE)")
    page.wait_for_function("document.getElementById('weeklyReviewOutput').textContent.includes('NEWER')")
    held[0].fulfill(json=_report("STALE"))
    page.wait_for_timeout(300)
    output = page.locator("#weeklyReviewOutput").inner_text()
    assert "NEWER" in output and "STALE" not in output


def test_clear_discards_a_response_still_in_flight(page, dashboard_live_server):
    held = []
    page.route(PATTERN, lambda route: held.append(route))
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    while not held:
        page.wait_for_timeout(50)
    page.evaluate("window.VFWeeklyReview.clear('cleared')")
    held[0].fulfill(json=_report("STALE"))
    page.wait_for_timeout(300)
    assert page.locator("#weeklyReviewOutput").inner_text() == ""
    assert page.locator("#weeklyReviewStatus").inner_text() == "cleared"


def test_denied_review_locks_the_panel(page, dashboard_live_server):
    _seed_weeks()
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    page.route(PATTERN, lambda route: route.fulfill(status=404, json={"detail": "Not found"}))
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewStatus').textContent.includes('unavailable')")
    assert page.locator("#weeklyReviewOutput").inner_text() == ""
    assert page.get_by_role("button", name="Generate review").is_disabled()


def test_lost_access_detected_by_comparisons_clears_the_review(page, dashboard_live_server):
    _seed_weeks()
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    page.route("**/api/comparisons*", lambda route: route.fulfill(status=404, json={"detail": "Not found"}))
    page.get_by_role("button", name="7d", exact=True).click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length === 0")
    assert page.get_by_role("button", name="Generate review").is_disabled()


def test_unchanged_metric_is_shown_as_stable(page, dashboard_live_server):
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        for age in range(14):
            connection.execute("INSERT INTO hrv (person_id, date, last_night_avg) VALUES (?, ?, ?)",
                               (person, (today - timedelta(days=age)).isoformat(), 50))
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    changes = page.locator("#weeklyReviewOutput section[data-section=changes]").inner_text()
    assert "unchanged" in changes and "not have enough" not in changes


def test_unrelated_context_failure_clears_but_does_not_lock_the_review(page, dashboard_live_server):
    _seed_weeks()
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    page.route("**/api/context*", lambda route: route.fulfill(status=404, json={"detail": "Not found"}))
    page.get_by_role("button", name="7d", exact=True).click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length === 0")
    assert page.get_by_role("button", name="Generate review").is_enabled()
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")


def test_review_is_dropped_when_a_sync_finishes(page, dashboard_live_server):
    _seed_weeks()
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    page.evaluate("syncFinished()")
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length === 0")


@pytest.mark.parametrize("outcome", [{"status": 200, "json": {"status": "link_required", "message": "Link Garmin"}},
                                     {"status": 409, "json": {"detail": "busy"}}])
def test_real_sync_completion_path_clears_a_review_generated_mid_sync(page, dashboard_live_server, outcome):
    _seed_weeks()
    held = []
    page.route("**/api/sync", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    _open(page, dashboard_live_server)
    page.locator("#syncBtn").click()
    while not held:
        page.wait_for_timeout(50)
    page.get_by_role("button", name="Generate review").click()
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length > 0")
    held[0].fulfill(**outcome)
    page.wait_for_function("document.getElementById('weeklyReviewOutput').children.length === 0")
