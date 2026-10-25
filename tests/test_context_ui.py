"""Context browser checks exercise only isolated DB/API fixtures, never live health data."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

import shared.database
from shared.auth import create_session_cookie
from tests.conftest import PERSON_PREFIX

pytestmark = pytest.mark.playwright
WRITE = {"X-VitalForge-Write": "context"}


def _date(age=0):
    return (datetime.now(timezone.utc).date() - timedelta(days=age)).isoformat()


def _api(server):
    return f"{server}{PERSON_PREFIX}/api/context"


def _put(page, server, path, body):
    response = page.request.put(f"{_api(server)}{path}", data=body, headers=WRITE)
    assert response.ok, response.status
    return response.json()


def _open(page, server):
    page.goto(f"{server}{PERSON_PREFIX}/")
    page.locator("#contextSection > summary").click()
    expect(page.locator("#contextStatus")).not_to_contain_text("Loading")
    return page.locator("#contextDayForm")


def _ready(page, date):
    page.locator("#contextDate").fill(date)
    expect(page.locator("#contextSave")).to_be_enabled()


def _save(page):
    with page.expect_response(lambda response: "/api/context/days/" in response.url and response.request.method == "PUT"):
        page.locator("#contextSave").click()
    expect(page.locator("#contextDayStatus")).to_have_text("Saved day loaded.")


def _seed_metrics():
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        for age in range(8):
            if age == 3:
                continue
            connection.execute("INSERT INTO sleep (person_id, date, duration_seconds, sleep_score) VALUES (?, ?, ?, ?)", (person, _date(age), 28800 if age < 3 else 25200, 80))
            connection.execute("INSERT INTO steps (person_id, date, value) VALUES (?, ?, ?)", (person, _date(age), 1000 + age))


def _plan(page, server):
    return _put(page, server, "/experiment", {"title": "My chosen routine", "tag": "Routine", "metric": "sleep_duration", "start_date": _date(6), "end_date": _date()})


def test_context_daily_crud_literal_text_empty_log_and_old_date(page, dashboard_live_server):
    old_date = _date(110)
    _put(page, dashboard_live_server, f"/days/{old_date}", {"note": "Older saved note", "tags": ["Travel"]})
    _open(page, dashboard_live_server)
    expect(page.locator("#contextSection")).to_contain_text("Anyone with view access")
    _ready(page, old_date)
    expect(page.locator("#contextNote")).to_have_value("Older saved note")
    page.locator("#contextNote").fill("Older edited note")
    _save(page)
    assert page.request.get(f"{_api(dashboard_live_server)}/days/{old_date}").json()["note"] == "Older edited note"
    _ready(page, _date())
    note = '<img src=x onerror="window.contextXSS=true"> shared note'
    page.locator("#contextNote").fill(note)
    page.locator("#contextTags").fill("Routine, ROUTINE, <svg onload=evil()>")
    _save(page)
    page.get_by_text("Logged context · selected range and chart markers", exact=True).click()
    expect(page.locator("#contextEntries")).to_contain_text(note)
    assert page.locator("#contextEntries img, #contextEntries svg").count() == 0
    assert page.evaluate("window.contextXSS") is None
    entry = page.request.get(f"{_api(dashboard_live_server)}/days/{_date()}").json()
    assert entry["tags"] == ["routine", "<svg onload=evil()>"]
    page.locator("#contextNote").fill("")
    page.locator("#contextTags").fill("")
    _save(page)
    entry = page.request.get(f"{_api(dashboard_live_server)}/days/{_date()}").json()
    assert entry["logged"] and entry["tags"] == [] and entry["note"] == ""
    expect(page.locator("#contextEntries")).to_contain_text("Logged without tags")
    page.locator("#contextDelete").click()
    expect(page.locator("#contextDayStatus")).to_contain_text("No log for this date")
    assert not page.request.get(f"{_api(dashboard_live_server)}/days/{_date()}").json()["logged"]


def test_context_saved_plan_real_associations_and_explicit_replacement(page, dashboard_live_server):
    _seed_metrics()
    for age in [0, 1, 2, 3, 4, 5, 6]:
        _put(page, dashboard_live_server, f"/days/{_date(age)}", {"note": "", "tags": ["routine", "travel"] if age < 3 else []})
    _open(page, dashboard_live_server)
    page.get_by_text("One chosen routine plan and association evidence", exact=True).click()
    page.locator("#contextPlanTitle").fill("My chosen routine")
    page.locator("#contextPlanTag").fill("Routine")
    page.locator("#contextPlanStart").fill(_date(6))
    page.locator("#contextPlanEnd").fill(_date())
    page.locator("#contextPlanSave").click()
    expect(page.locator("#contextPlanSummary")).to_contain_text("Saved plan: My chosen routine")
    expect(page.locator("#contextAssociation")).to_contain_text("3 date-paired observations")
    expect(page.locator("#contextAssociation")).to_contain_text("mean 8 hours")
    expect(page.locator("#contextAssociation")).to_contain_text("mean 7 hours")
    expect(page.locator("#contextAssociation")).to_contain_text("1 hours")
    report = page.request.get(f"{_api(dashboard_live_server)}/associations?days=30").json()
    assert report["groups"]["untagged"]["paired_days"] == 3
    assert report["groups"]["untagged"]["missing_metric_dates"] == [_date(3)]
    assert _date(7) not in report["groups"]["untagged"]["dates"]
    assert report["difference"]["absolute"] == 3600
    expect(page.locator("#contextAssociation")).to_contain_text("travel: 3 / 0")
    assert "weight" not in page.locator("#contextPlanMetric option").evaluate_all("options => options.map(option => option.value)")
    page.locator("#contextPlanTitle").fill("Replacement label")
    page.locator("#contextPlanSave").click()
    assert page.request.get(f"{_api(dashboard_live_server)}/experiment").json()["title"] == "My chosen routine"
    page.locator("#contextReplace").check()
    page.locator("#contextPlanSave").click()
    expect(page.locator("#contextPlanSummary")).to_contain_text("Replacement label")
    page.locator("#contextPlanDelete").click()
    expect(page.locator("#contextPlanSummary")).to_have_text("No routine plan saved.")
    expect(page.locator("#contextAssociation")).to_contain_text("Save a chosen routine plan")


def test_context_export_explicit_optin_range_and_reload_reset(page, dashboard_live_server, tmp_path):
    secret_note = "Synthetic identifying note for export consent"
    _put(page, dashboard_live_server, f"/days/{_date()}", {"note": secret_note, "tags": ["travel"]})
    _plan(page, dashboard_live_server)
    _open(page, dashboard_live_server)
    page.locator("#packagePreview > summary").click()
    checkbox = page.locator("#includeContext")
    expect(checkbox).not_to_be_checked()
    expect(page.locator("#contextExportPreview")).not_to_contain_text(secret_note)
    raw_url = page.locator("#exportJson").get_attribute("href")
    assert "context" not in raw_url
    for include in [False, True]:
        if include:
            checkbox.check()
            expect(page.locator("#contextExportPreview")).to_contain_text(secret_note)
        with page.expect_download() as download_info:
            page.locator("#exportPackage").click()
        path = tmp_path / f"package-{include}.json"
        download_info.value.save_as(path)
        package = json.loads(path.read_text())
        assert ("context" in package) is include
        if include:
            assert package["context"]["entries"][0]["note"] == secret_note
    page.get_by_role("button", name="7d", exact=True).click()
    expect(checkbox).to_be_checked()
    query = parse_qs(urlsplit(page.locator("#exportPackage").get_attribute("href")).query)
    assert query == {"days": ["7"], "include_context": ["true"]}
    assert "include_context" not in page.locator("#exportJson").get_attribute("href")
    page.reload()
    expect(page.locator("#includeContext")).not_to_be_checked()
    assert "include_context" not in page.locator("#exportPackage").get_attribute("href")
    assert secret_note not in page.evaluate("JSON.stringify(localStorage) + JSON.stringify(sessionStorage)")


def test_context_markers_calendar_gap_draft_refresh_and_failure(page, dashboard_live_server):
    _seed_metrics()
    for age in [1, 3]:
        _put(page, dashboard_live_server, f"/days/{_date(age)}", {"note": f"Note age {age}", "tags": ["routine"]})
    _open(page, dashboard_live_server)
    page.get_by_role("button", name="7d", exact=True).click()
    page.wait_for_function("Chart.getChart('chartSleepDuration')?.$contextMarkerDates?.length === 1")
    assert page.evaluate("Chart.getChart('chartSleepDuration').$contextMarkerDates") == [_date(1)]
    chart = page.evaluate("() => {const c=Chart.getChart('chartSleepDuration'); return {labels:c.data.labels,values:c.data.datasets[0].data};}")
    assert chart["values"][chart["labels"].index(_date(3))] is None
    page.get_by_text("Logged context · selected range and chart markers", exact=True).click()
    expect(page.locator("#contextEntries")).to_contain_text("Note age 3")
    _ready(page, _date())
    page.locator("#contextNote").fill("Unsaved memory-only draft")
    page.get_by_role("button", name="kg", exact=True).click()
    expect(page.locator("#contextNote")).to_have_value("Unsaved memory-only draft")
    page.get_by_role("button", name="90d", exact=True).click()
    expect(page.locator("#contextNote")).to_have_value("Unsaved memory-only draft")
    expect(page.locator("#contextDate")).to_be_disabled()
    assert not page.request.get(f"{_api(dashboard_live_server)}/days/{_date()}").json()["logged"]
    page.route("**/api/context?**", lambda route: route.fulfill(status=500, json={"detail": "not shown"}))
    page.get_by_role("button", name="7d", exact=True).click()
    expect(page.locator("#contextStatus")).to_contain_text("Context is unavailable")
    page.wait_for_function("Chart.getChart('chartSleepDuration')?.$contextMarkerDates?.length === 0")
    assert page.locator("#contextEntries").inner_text() == "No context logged in this range."
    expect(page.locator("#contextNote")).to_have_value("Unsaved memory-only draft")
    expect(page.locator("#cardWeight")).to_be_visible()


def test_context_date_fetch_race_cannot_overwrite_new_date_or_allow_early_save(page, dashboard_live_server):
    old_date, new_date = _date(100), _date(101)
    for date, note in [(old_date, "Older A"), (new_date, "Older B")]:
        _put(page, dashboard_live_server, f"/days/{date}", {"note": note, "tags": []})
    _open(page, dashboard_live_server)
    held = []
    page.route(f"**/api/context/days/{old_date}", lambda route: held.append(route))
    page.locator("#contextDate").fill(old_date)
    page.wait_for_function("document.getElementById('contextDayStatus').textContent.includes('Loading this date')")
    expect(page.locator("#contextSave")).to_be_disabled()
    _ready(page, new_date)
    expect(page.locator("#contextNote")).to_have_value("Older B")
    held[0].fulfill(status=200, json={"date": old_date, "logged": True, "note": "Older A", "tags": [], "updated_at": "synthetic"})
    expect(page.locator("#contextNote")).to_have_value("Older B")
    page.locator("#contextNote").fill("Changed B")
    _save(page)
    assert page.request.get(f"{_api(dashboard_live_server)}/days/{old_date}").json()["note"] == "Older A"
    assert page.request.get(f"{_api(dashboard_live_server)}/days/{new_date}").json()["note"] == "Changed B"


def test_context_real_viewer_is_readonly_and_revocation_clears_draft(page, dashboard_live_server):
    _put(page, dashboard_live_server, f"/days/{_date()}", {"note": "Shared synthetic context", "tags": []})
    _open(page, dashboard_live_server)
    _ready(page, _date())
    page.locator("#contextNote").fill("Unsaved private draft")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        user = connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('contextviewer','unused','user',?)", (now,)).lastrowid
        connection.execute("INSERT INTO person_grants (person_id,user_id,access,granted_at) VALUES (?,?,'view',?)", (person, user, now))
    page.context.add_cookies([{"name": "vf_session", "value": create_session_cookie("contextviewer", user, 1), "url": dashboard_live_server}])
    page.get_by_role("button", name="7d", exact=True).click()
    expect(page.locator("#contextStatus")).to_contain_text("View access")
    expect(page.locator("#contextDayForm")).to_be_hidden()
    expect(page.locator("#contextPlanForm")).to_be_hidden()
    assert page.locator("#contextNote").input_value() == ""
    response = page.request.put(f"{_api(dashboard_live_server)}/days/{_date()}", data={"note": "forbidden", "tags": []}, headers=WRITE)
    assert response.status == 404
    assert page.request.get(f"{_api(dashboard_live_server)}/days/{_date()}").json()["note"] == "Shared synthetic context"
    page.get_by_text("Logged context · selected range and chart markers", exact=True).click()
    expect(page.locator("#contextEntries")).to_contain_text("Shared synthetic context")
    assert page.locator("#contextEntries button").count() == 0
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_context_revoked_grant_404_clears_drafts_and_export_selection(page, dashboard_live_server):
    _seed_metrics()
    secret_tag = "synthetic-revoked-secret-tag"
    _put(page, dashboard_live_server, f"/days/{_date()}", {"note": "Shared context", "tags": [secret_tag]})
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        user = connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('contextmanager','unused','user',?)", (now,)).lastrowid
        connection.execute("INSERT INTO person_grants (person_id,user_id,access,granted_at) VALUES (?,?,'manage',?)", (person, user, now))
    page.context.add_cookies([{"name": "vf_session", "value": create_session_cookie("contextmanager", user, 1), "url": dashboard_live_server}])
    _open(page, dashboard_live_server)
    _ready(page, _date())
    expect(page.locator("#contextSuggestions")).to_contain_text(secret_tag)
    page.locator("#contextNote").fill("Draft before revocation")
    page.locator("#packagePreview > summary").click()
    page.locator("#includeContext").check()
    page.wait_for_function("Chart.getChart('chartSleepDuration')?.$contextMarkerDates?.length === 1")
    page.locator("#chartSleepDuration").scroll_into_view_if_needed()
    hover = page.evaluate("""() => {
        const chart = Chart.getChart('chartSleepDuration');
        const index = chart.data.labels.length - 1;
        const box = chart.canvas.getBoundingClientRect();
        return {x:box.left + chart.scales.x.getPixelForValue(index), y:box.top + chart.getDatasetMeta(0).data[index].y};
    }""")
    page.mouse.move(hover["x"], hover["y"])
    page.wait_for_function("tag => Chart.getChart('chartSleepDuration').tooltip.afterBody.join(' ').includes(tag)", arg=secret_tag)
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        connection.execute("DELETE FROM person_grants WHERE user_id = ?", (user,))
    page.evaluate("VFContext.refresh(7, 'kg')")
    expect(page.locator("#contextStatus")).to_contain_text("Context access changed")
    expect(page.locator("#contextDayForm")).to_be_hidden()
    expect(page.locator("#contextPlanForm")).to_be_hidden()
    assert page.locator("#contextNote").input_value() == ""
    expect(page.locator("#includeContext")).not_to_be_checked()
    assert "include_context" not in page.locator("#exportPackage").get_attribute("href")
    assert page.locator("#contextEntries").inner_text() == ""
    assert page.evaluate("Chart.getChart('chartSleepDuration').tooltip.afterBody") == []
    assert page.evaluate("Chart.getChart('chartSleepDuration').$contextMarkerDates") == []
    assert not page.evaluate("tag => document.documentElement.textContent.includes(tag) || [...document.querySelectorAll('input, textarea')].some(field => field.value.includes(tag))", secret_tag)


def test_context_range_and_association_races_never_restore_old_evidence(page, dashboard_live_server):
    _seed_metrics()
    for age in [0, 40]:
        _put(page, dashboard_live_server, f"/days/{_date(age)}", {"note": f"Range context {age}", "tags": ["routine"]})
    _put(page, dashboard_live_server, "/experiment", {"title": "Range routine", "tag": "routine", "metric": "sleep_duration", "start_date": _date(60), "end_date": _date()})
    old_context = page.request.get(f"{_api(dashboard_live_server)}?days=7").json()
    old_association = page.request.get(f"{_api(dashboard_live_server)}/associations?days=7").json()
    _open(page, dashboard_live_server)
    page.get_by_text("Logged context · selected range and chart markers", exact=True).click()
    page.get_by_text("One chosen routine plan and association evidence", exact=True).click()
    held_context = []
    page.route("**/api/context?days=7", lambda route: held_context.append(route))
    page.get_by_role("button", name="7d", exact=True).click()
    expect(page.locator("#contextStatus")).to_have_text("Loading context…")
    expect(page.locator("#contextSuggestions")).to_be_empty()
    page.get_by_role("button", name="90d", exact=True).click()
    expect(page.locator("#contextEntries")).to_contain_text("Range context 40")
    held_context[0].fulfill(status=200, json=old_context)
    expect(page.locator("#contextEntries")).to_contain_text("Range context 40")
    page.unroute("**/api/context?days=7")
    held_association = []
    page.route("**/api/context/associations?days=7", lambda route: held_association.append(route))
    page.get_by_role("button", name="7d", exact=True).click()
    expect(page.locator("#contextStatus")).to_contain_text("1 logged days")
    page.wait_for_function("document.getElementById('contextAssociation').textContent === 'Loading association evidence…'")
    page.get_by_role("button", name="90d", exact=True).click()
    expect(page.locator("#contextAssociation")).to_contain_text("61 calendar days")
    held_association[0].fulfill(status=200, json=old_association)
    expect(page.locator("#contextAssociation")).to_contain_text("61 calendar days")
    page.unroute("**/api/context/associations?days=7")
    page.route("**/api/context/associations?days=7", lambda route: route.fulfill(status=500, json={"detail": "payload must not be rendered"}))
    page.get_by_role("button", name="7d", exact=True).click()
    expect(page.locator("#contextAssociation")).to_contain_text("Association evidence is unavailable")
    expect(page.locator("#contextAssociation")).not_to_contain_text("61 calendar days")
    expect(page.locator("#contextAssociation")).not_to_contain_text("payload must not be rendered")


def test_context_new_plan_draft_survives_slow_saved_plan_delete(page, dashboard_live_server):
    _plan(page, dashboard_live_server)
    _open(page, dashboard_live_server)
    page.get_by_text("One chosen routine plan and association evidence", exact=True).click()
    expect(page.locator("#contextPlanTitle")).to_have_value("My chosen routine")
    held = []

    def delay_delete(route):
        if route.request.method == "DELETE":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/context/experiment", delay_delete)
    page.locator("#contextPlanDelete").click()
    page.locator("#contextPlanTitle").fill("New title typed during delete")
    page.locator("#contextPlanTag").fill("new routine")
    assert len(held) == 1
    held[0].continue_()
    expect(page.locator("#contextPlanSummary")).to_have_text("No routine plan saved.")
    expect(page.locator("#contextPlanTitle")).to_have_value("New title typed during delete")
    expect(page.locator("#contextPlanTag")).to_have_value("new routine")
    expect(page.locator("#contextPlanStatus")).to_contain_text("Newer edits are still an unsaved draft")
    assert page.request.get(f"{_api(dashboard_live_server)}/experiment").json() is None
    page.locator("#contextPlanSave").click()
    expect(page.locator("#contextPlanSummary")).to_contain_text("Saved plan: New title typed during delete")
