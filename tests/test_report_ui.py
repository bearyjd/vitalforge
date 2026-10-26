"""Printable report browser checks use only the fixture's isolated database and fake Garmin client."""

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import shared.database
from tests.conftest import PERSON_PREFIX

pytestmark = pytest.mark.playwright

HOSTILE_NAME = "<script>window.reportBad=true</script>"


def _seed(display_name: str | None = None):
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(str(shared.database.DB_PATH)) as connection:
        person = connection.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        if display_name:
            connection.execute("UPDATE persons SET display_name = ? WHERE id = ?", (display_name, person))
        for age in range(14):
            connection.execute("INSERT INTO hrv (person_id, date, last_night_avg) VALUES (?, ?, ?)",
                               (person, (today - timedelta(days=age)).isoformat(), 80 if age < 7 else 40))


def _open_control(page, base):
    page.goto(f"{base}{PERSON_PREFIX}/")
    page.locator("#printableReport summary").click()


def _query(page) -> dict:
    return parse_qs(urlsplit(page.locator("#reportLink").get_attribute("href")).query)


def test_control_builds_the_report_url_and_opts_in_are_off_by_default(page, dashboard_live_server):
    _open_control(page, dashboard_live_server)
    link = page.locator("#reportLink")
    assert urlsplit(link.get_attribute("href")).path == f"{PERSON_PREFIX}/report"
    assert _query(page) == {"days": ["30"], "include_identity": ["false"], "include_context": ["false"]}
    assert not page.locator("#reportIdentity").is_checked() and not page.locator("#reportContext").is_checked()
    assert link.get_attribute("target") == "_blank" and "noopener" in link.get_attribute("rel")
    page.locator("#reportDays").select_option("90")
    page.locator("#reportIdentity").check()
    page.locator("#reportContext").check()
    assert _query(page) == {"days": ["90"], "include_identity": ["true"], "include_context": ["true"]}
    page.locator("#reportContext").uncheck()
    assert _query(page)["include_context"] == ["false"] and _query(page)["include_identity"] == ["true"]


def test_checkboxes_reset_on_reload(page, dashboard_live_server):
    _open_control(page, dashboard_live_server)
    page.locator("#reportIdentity").check()
    page.locator("#reportContext").check()
    page.reload()
    page.locator("#printableReport summary").click()
    assert not page.locator("#reportIdentity").is_checked() and not page.locator("#reportContext").is_checked()
    assert _query(page)["include_identity"] == ["false"] and _query(page)["include_context"] == ["false"]


def test_report_renders_light_with_the_toolbar_hidden_in_print(page, dashboard_live_server):
    _seed()
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/report?days=7")
    toolbar = page.locator(".toolbar")
    assert toolbar.is_visible()
    assert page.locator("section[data-metric=hrv] svg[role=img]").count() == 1
    background = "getComputedStyle(document.body).backgroundColor"
    page.emulate_media(media="screen")
    assert page.evaluate(background) == "rgb(255, 255, 255)"  # light on screen too, not only in print
    page.emulate_media(media="print")
    assert toolbar.is_hidden()
    assert page.evaluate(background) == "rgb(255, 255, 255)"
    assert page.locator("section[data-appendix=hrv]").is_visible()  # recorded values print, not just draw
    assert page.locator("section[data-appendix=hrv] td[data-date]").count() == 7
    assert page.get_by_role("heading", name="Health data report").is_visible()


def test_opted_in_script_name_is_text_and_does_not_execute(page, dashboard_live_server):
    _seed(HOSTILE_NAME)
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/report?days=7&include_identity=true")
    assert HOSTILE_NAME in page.locator("main").inner_text()
    assert page.evaluate("window.reportBad === undefined")
    assert page.locator("main script").count() == 0
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/report?days=7")
    assert "reportBad" not in page.content()


def test_pageshow_restore_resets_the_opt_ins(page, dashboard_live_server):
    _open_control(page, dashboard_live_server)
    page.locator("#reportDays").select_option("7")
    page.locator("#reportIdentity").check()
    page.locator("#reportContext").check()
    # A bfcache restore keeps form state and fires pageshow(persisted) without reloading the script.
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    assert not page.locator("#reportIdentity").is_checked() and not page.locator("#reportContext").is_checked()
    assert page.locator("#reportDays").input_value() == "30"
    assert _query(page) == {"days": ["30"], "include_identity": ["false"], "include_context": ["false"]}


def test_report_page_runs_no_script(page, dashboard_live_server):
    _seed()
    page.goto(f"{dashboard_live_server}{PERSON_PREFIX}/report?days=7")
    assert page.locator("script, button").count() == 0


def test_link_opens_the_report_in_a_new_tab(page, dashboard_live_server):
    _seed()
    _open_control(page, dashboard_live_server)
    page.locator("#reportDays").select_option("7")
    with page.context.expect_page() as opened:
        page.locator("#reportLink").click()
    report = opened.value
    report.wait_for_load_state()
    assert urlsplit(report.url).path == f"{PERSON_PREFIX}/report" and "days=7" in report.url
    assert report.locator("section[data-metric=hrv]").count() == 1
