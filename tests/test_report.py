"""Printable clinician report: server-rendered, opt-in identity/notes, synthetic local data only."""

import json
import re
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, grant_person, seed_person, seed_user
from tests.test_data_quality import NOW, seed, sync_status
from vitalforge_dashboard import data_quality, report_data
from vitalforge_dashboard.goals import GoalCreate, create_goal
from vitalforge_dashboard.report_svg import SPARK_HEIGHT, SPARK_PAD, sparkline
from vitalforge_dashboard.weekly_review import LIMITATIONS

PATH = f"{PERSON_PREFIX}/report"
WRITE = {"X-VitalForge-Write": "context"}
HOSTILE_NAME = "<script>alert(1)</script>"
HOSTILE_NOTE = "<img src=x onerror=alert(2)> note-marker"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@pytest.fixture
async def client(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(data_quality, "utc_now", lambda: NOW)
    async with AsyncClient(transport=ASGITransport(app=dashboard_app_module.app), base_url="http://test") as ac:
        yield ac


def rows(start: int, end: int, value: float) -> list:
    return [((NOW.date() - timedelta(days=offset)).isoformat(), value) for offset in range(start, end)]


def day(offset: int) -> str:
    return (NOW.date() - timedelta(days=offset)).isoformat()


async def page(client, query: str = "") -> str:
    response = await client.get(f"{PATH}{query}")
    assert response.status_code == 200, response.text
    return response.text


def block(html: str, metric: str) -> str:
    match = re.search(rf'<section[^>]*data-metric="{metric}".*?</section>', html, re.DOTALL)
    assert match, f"no section for {metric}"
    return match.group(0)


def appendix(html: str, metric: str) -> str:
    match = re.search(rf'<section[^>]*data-appendix="{metric}".*?</section>', html, re.DOTALL)
    assert match, f"no appendix for {metric}"
    return match.group(0)


def print_css(html: str) -> str:
    """The brace-balanced body of the @media print block."""
    start = html.index("@media print") + len("@media print")
    depth, begin = 0, html.index("{", start)
    for index in range(begin, len(html)):
        depth += {"{": 1, "}": -1}.get(html[index], 0)
        if depth == 0:
            return html[begin + 1:index]
    raise AssertionError("unbalanced @media print block")


async def insert_note(person_id: int, when: str, note: str, tags: list[str]) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO daily_context (person_id,date,note,tags_json,updated_at) VALUES (?,?,?,?,?)",
            (person_id, when, note, json.dumps(tags), NOW.isoformat()))
        await db.commit()
    finally:
        await db.close()


def svg_of(html: str, metric: str) -> str:
    match = re.search(r"<svg.*?</svg>", block(html, metric), re.DOTALL)
    assert match, f"no sparkline for {metric}"
    return match.group(0)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


async def rename_primary(name: str) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET display_name = ? WHERE id = ?", (name, await get_primary_person_id()))
        await db.commit()
    finally:
        await db.close()


async def put_note(client, when: str, note: str, tags: list[str]) -> None:
    response = await client.put(f"{PERSON_PREFIX}/api/context/days/{when}", json={"note": note, "tags": tags}, headers=WRITE)
    assert response.status_code == 200


@pytest.mark.parametrize("query", ["?days=14", "?days=0", "?days=-7", "?days=abc", "?days=30&include_identity=maybe",
                                   "?include_context=2x"])
async def test_invalid_query_is_rejected(client, query):
    assert (await client.get(f"{PATH}{query}")).status_code == 422


@pytest.mark.parametrize("days", [7, 30, 90])
async def test_allowed_periods_render_and_default_is_30(client, days):
    html = await page(client, f"?days={days}")
    assert f"Last {days} days" in text_of(html)
    assert "Last 30 days" in text_of(await page(client))


async def test_private_headers_and_standalone_html(client):
    response = await client.get(PATH)
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["content-type"].startswith("text/html")
    assert "<script src" not in response.text and "Chart" not in response.text


async def test_authorization_denials_and_grant(client):
    user = await seed_user("report-viewer")
    person = await get_primary_person_id()
    anonymous = await client.get(PATH)
    assert anonymous.status_code == 302 and "login" in anonymous.headers["location"]  # same as the dashboard page
    assert "<h1>Health data report" not in anonymous.text
    client.cookies.set("vf_session", create_session_cookie("report-viewer", user, 1))
    assert (await client.get(PATH)).status_code == 404  # no grant
    await grant_person(person, user, "view")
    assert (await client.get(PATH)).status_code == 200
    ungranted = await seed_person("ungranted")
    archived = await seed_person("archived")
    await grant_person(archived, user, "view")
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), archived))
        await db.commit()
    finally:
        await db.close()
    assert ungranted != person
    for slug in ("ungranted", "archived", "missing"):
        assert (await client.get(f"/p/{slug}/report")).status_code == 404


async def test_other_profiles_data_never_appears(client):
    other = await seed_person("other-profile")
    await seed("hrv", "last_night_avg", rows(0, 7, 4242), person_id=other)
    await seed("hrv", "last_night_avg", rows(0, 7, 55))
    html = await page(client, "?days=7")
    assert "4242" not in html and "55" in block(html, "hrv")


async def test_defaults_exclude_identity_notes_and_tags_but_opt_ins_include_them_escaped(client):
    await rename_primary(HOSTILE_NAME)
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    await put_note(client, day(1), HOSTILE_NOTE, ["tag-marker"])
    default = await page(client, "?days=7")
    for secret in ("alert(1)", "note-marker", "tag-marker", "Profile:", "Your notes"):
        assert secret not in default
    assert not EMAIL.search(default)
    assert "person_id" not in default and "garmin_email" not in default
    named = await page(client, "?days=7&include_identity=true")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in named
    assert "<script>alert(1)" not in named and "note-marker" not in named
    noted = await page(client, "?days=7&include_context=true")
    assert "&lt;img src=x onerror=alert(2)&gt; note-marker" in noted
    assert "<img src=x" not in noted and "tag-marker" in noted
    assert "alert(1)" not in noted
    assert "entered by you" in noted.lower()


async def test_context_is_limited_to_the_selected_period(client):
    await seed("hrv", "last_night_avg", rows(0, 14, 60))
    await put_note(client, day(2), "inside-note", [])
    await put_note(client, day(10), "previous-period-note", ["old-tag"])
    html = await page(client, "?days=7&include_context=true")
    assert "inside-note" in html
    assert "previous-period-note" not in html and "old-tag" not in html
    wider = await page(client, "?days=30&include_context=true")
    assert "previous-period-note" in wider


async def test_display_name_helper_handles_archived_and_missing(client):
    person = await seed_person("gone", display_name="Gone Person")
    assert await report_data.profile_display_name(person) == "Gone Person"
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), person))
        await db.commit()
    finally:
        await db.close()
    assert await report_data.profile_display_name(person) is None
    assert await report_data.profile_display_name(987654) is None


async def test_units_are_converted_for_display(client):
    await seed("weight_history", "weight_grams", rows(0, 7, 81500) + rows(7, 14, 80000))
    await seed("sleep", "duration_seconds", rows(0, 7, 27000) + rows(7, 14, 28800))
    html = await page(client, "?days=7")
    weight, sleep = text_of(block(html, "weight")), text_of(block(html, "sleep_duration"))
    assert "81.5" in weight and "kg" in weight and "81500" not in weight and "80000" not in weight
    assert "mean 80 kg" in weight and "+1.5" not in weight  # previous mean in kg; snapshot metrics are not compared
    assert "7.5" in sleep and " h" in sleep and "27000" not in sleep and "28800" not in sleep
    assert "-0.5" in sleep  # difference in hours (27000 s - 28800 s = -1800 s)


async def test_percent_and_provider_scores_keep_honest_units(client):
    await seed("weight_history", "body_fat", rows(0, 7, 18.5))
    await seed("sleep", "sleep_score", rows(0, 7, 82))
    html = await page(client, "?days=7")
    assert "%" in text_of(block(html, "body_fat")) and "18.5" in block(html, "body_fat")
    score = text_of(block(html, "sleep_score"))
    assert "provider score" in score and "not a percentage" in score.lower()


async def test_source_text_comes_from_metric_metadata(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    assert "original measurement origin is unknown" in text_of(block(await page(client, "?days=7"), "hrv"))


@pytest.mark.parametrize("days", [7, 30, 90])
async def test_coverage_matches_profile_snapshot(client, days):
    await seed("hrv", "last_night_avg", [(day(offset), 50 + offset % 5) for offset in range(0, 170, 3)])
    await seed("steps", "value", rows(0, 5, 9000))
    quality, _, _ = await data_quality.profile_snapshot(await get_primary_person_id(), days)
    html = await page(client, f"?days={days}")
    for metric in ("hrv", "steps"):
        expected = quality["metrics"][metric]
        text = text_of(block(html, metric))
        assert f"{expected['observed_days']} of {days} days" in text
        assert f"{expected['coverage_percent']}%" in text
        assert f"{expected['missing_days']} missing" in text
        assert expected["completeness"].capitalize() in text
        assert expected["latest_observation_date"] in text


async def test_ninety_day_report_uses_one_wide_snapshot_and_a_valid_previous_period(client, monkeypatch):
    calls = []
    original = report_data.profile_snapshot

    async def spy(person_id, days, **kwargs):
        calls.append((days, kwargs))
        return await original(person_id, days, **kwargs)

    monkeypatch.setattr(report_data, "profile_snapshot", spy)
    await seed("hrv", "last_night_avg", rows(0, 90, 70) + rows(90, 180, 50))
    html = await page(client, "?days=90&include_context=true")
    assert calls == [(180, {"include_context": True})]
    hrv = text_of(block(html, "hrv"))
    assert "Previous 90 days" in hrv and "+20" in hrv and "90 of 90 days" in hrv


async def test_stable_zero_difference_versus_insufficient_data(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60) + rows(7, 14, 60))
    await seed("resting_hr", "value", rows(0, 2, 58) + rows(7, 14, 50))
    html = await page(client, "?days=7")
    stable, thin = text_of(block(html, "hrv")), text_of(block(html, "resting_hr"))
    assert "Stable" in stable and "Insufficient data" not in stable
    assert "Insufficient data" in thin and "Stable" not in thin
    assert "+8" not in thin and "Difference" not in thin  # interpretation suppressed


async def test_changed_metric_shows_signed_difference_and_baseline_band(client):
    await seed("hrv", "last_night_avg", [(d, v) for d, v in rows(0, 7, 70)] +
               [(day(7 + i), 40 + 2 * i) for i in range(7)])
    hrv = text_of(block(await page(client, "?days=7"), "hrv"))
    assert "Difference" in hrv and "+" in hrv and "Stable" not in hrv
    assert "Previous-period range" in hrv and "not a normal range" in hrv.lower()


async def test_real_zero_is_plotted_and_gaps_split_the_line(client):
    # Recorded: today (0), yesterday (10), and 5 days ago (0); the dates between are gaps.
    await seed("stress", "avg_level", [(day(0), 0), (day(1), 10), (day(5), 0)])
    svg = svg_of(await page(client, "?days=7"), "stress")
    circles = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)
    assert len(circles) == 3
    bottom = f"{SPARK_HEIGHT - SPARK_PAD:.1f}"
    assert [cy for _, cy in circles].count(bottom) == 2  # both zeros sit on the baseline, not dropped
    assert len(re.findall(r"<polyline", svg)) == 1  # only the adjacent pair is joined
    assert 'role="img"' in svg and "aria-label=" in svg


async def test_constant_series_does_not_break_the_plot(client):
    await seed("steps", "value", rows(0, 7, 5000))
    svg = svg_of(await page(client, "?days=7"), "steps")
    assert len(re.findall(r"<circle", svg)) == 7 and "nan" not in svg.lower() and "inf" not in svg.lower()


async def test_printed_values_are_an_always_visible_appendix_not_a_closed_details(client):
    await seed("steps", "value", [(day(0), 1234), (day(3), 0), (day(6), 4321)])
    html = await page(client, "?days=7")
    assert "<details" not in html  # a closed <details> prints collapsed, losing the per-date values
    section = appendix(html, "steps")
    assert section.count("data-date=") == 3
    for when, value in ((day(0), "1234"), (day(3), "0"), (day(6), "4321")):
        assert re.search(rf'data-date="{when}">{when}</td><td>{value}</td>', section.replace("\n", "").replace("  ", ""))
    assert day(1) not in section  # gaps are not listed as values
    assert "break-inside: avoid" in print_css(html) and "table-header-group" in print_css(html)


async def test_ninety_day_appendix_packs_dates_into_columns(client):
    await seed("steps", "value", rows(0, 90, 100))
    section = appendix(await page(client, "?days=90"), "steps")
    assert section.count("data-date=") == 90
    assert section.count("<tr>") <= 1 + 30  # three date/value pairs per row


async def test_empty_period_says_so_plainly(client):
    html = await page(client, "?days=7")
    assert "No observations were recorded in this period" in text_of(html)
    assert 'data-metric="' not in html
    assert LIMITATIONS[-1] in text_of(html)


async def test_metric_without_period_observations_is_listed_not_charted(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    await seed("steps", "value", rows(20, 25, 3000))
    html = await page(client, "?days=7")
    assert 'data-metric="steps"' not in html and 'data-metric="hrv"' in html
    assert "Steps" in text_of(html.split("No observations in this period", 1)[1])


async def test_header_facts_window_sync_and_limitations(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    await sync_status("success")
    html = text_of(await page(client, "?days=7"))
    assert "Generated 2026-10-25" in html
    assert "2026-10-19 to 2026-10-25" in html and "2026-10-12 to 2026-10-18" in html
    assert "UTC" in html and "Garmin source calendar dates" in html
    assert "Last sync attempt" in html and "Successful" in html
    assert LIMITATIONS[-1] in html
    assert "7 calendar dates" not in html  # weekly-only limitation text must not leak into 30/90 reports


def findings_of(html: str) -> list[str]:
    return [text_of(item) for item in re.findall(r'<div class="finding"[^>]*>.*?</div>', html, re.DOTALL)]


async def test_findings_show_exact_evidence_dates_inside_the_finding(client):
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600) + rows(3, 10, 8 * 3600))
    html = await page(client, "?days=90")
    mine = [item for item in findings_of(html) if "Sleep duration was under 7 hours" in item]
    assert len(mine) == 1
    assert f"Evidence dates: {day(2)}, {day(1)}, {day(0)}." in mine[0]
    assert "last 30 calendar dates" in text_of(html)


async def test_findings_list_the_baseline_dates_behind_every_comparison(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 40) + rows(7, 14, 80))
    mine = [item for item in findings_of(await page(client, "?days=7")) if "lower over the latest 7" in item]
    assert len(mine) == 1
    recent = ", ".join(sorted(day(offset) for offset in range(0, 7)))
    compared = ", ".join(sorted(day(offset) for offset in range(7, 14)))
    assert f"Evidence dates: {recent}." in mine[0]
    assert f"Compared with: {compared}." in mine[0]


async def test_finding_text_never_uses_alarm_words(client):
    await seed("steps", "value", rows(0, 3, 20000) + rows(3, 30, 10000))
    html = await page(client, "?days=30")
    assert findings_of(html), "the constant-baseline case should produce a finding"
    lowered = text_of(html).lower()
    for word in ("alert", "warning", "flag"):
        assert word not in lowered


@pytest.mark.parametrize("raw, expected", [
    ("the existing rule assigns an alert flag, not an estimated z-score", "the existing rule assigns a notable marker, not an estimated z-score"),
    ("Alert: check", "Notice: check"),
    ("two alert flags", "two notable markers"),
    ("a warning here", "a note here"),
    ("Warnings issued", "Notes issued"),
    ("Flagged day", "Marked day"),
    ("an alert", "a notice"),
    ("alerts", "notices"),
    ("Flagship metric", "Flagship metric"),
])
def test_neutral_wording_is_grammatical_and_keeps_case(raw, expected):
    assert report_data._neutral(raw) == expected


async def test_constant_baseline_sentence_is_exact_and_lists_the_anomaly_baseline(client):
    await seed("steps", "value", rows(0, 3, 20000) + rows(3, 30, 10000))
    html = await page(client, "?days=30")
    text = text_of(html)
    assert "the existing rule assigns a notable marker, not an estimated z-score" in text
    assert "an notable" not in text
    mine = [item for item in findings_of(html) if "notable marker" in item]
    assert len(mine) == 1
    baseline = ", ".join(sorted(day(offset) for offset in range(3, 24)))  # rows[-24:-3] of the 30 dates
    assert f"Compared with: {baseline}." in mine[0]


@pytest.mark.parametrize("table, column, recent, older, phrase", [
    ("hrv", "last_night_avg", 48, 50, "Overnight HRV was below its observed 30-date average"),
    ("resting_hr", "value", 70, 50, "The latest resting heart rate (70 bpm) is above its observed 30-date average"),
])
async def test_all_rows_baseline_rules_list_every_compared_date(client, table, column, recent, older, phrase):
    latest_only = 1 if table == "resting_hr" else 3
    await seed(table, column, rows(0, latest_only, recent) + rows(latest_only, 30, older))
    mine = [item for item in findings_of(await page(client, "?days=30")) if phrase in item]
    assert len(mine) == 1
    everything = ", ".join(sorted(day(offset) for offset in range(0, 30)))
    assert f"Compared with: {everything}." in mine[0]


async def test_descriptions_never_name_a_unit_other_than_the_displayed_one(client):
    from vitalforge_dashboard.metric_metadata import METRIC_METADATA
    converted = {name: meta for name, meta in METRIC_METADATA.items() if meta["unit"] in report_data.DISPLAY_UNITS}
    assert {"weight", "bone_mass", "muscle_mass", "sleep_duration"} <= set(converted)
    await seed("weight_history", "weight_grams", rows(0, 1, 81500))
    await seed("weight_history", "bone_mass_g", rows(1, 2, 3200))  # one row per date: separate dates, no overwrite
    await seed("weight_history", "muscle_mass_g", rows(2, 3, 35000))
    await seed("sleep", "duration_seconds", rows(0, 3, 27000))
    html = await page(client, "?days=7")
    for name, meta in converted.items():
        shown, _ = report_data.DISPLAY_UNITS[meta["unit"]]
        text = text_of(block(html, name))
        assert f"({shown})" in text, name
        for stored in ("grams", "seconds", " in g"):
            assert stored not in text, (name, stored)


async def test_no_findings_is_stated(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    assert "No rule findings" in text_of(await page(client, "?days=7"))


async def test_goals_never_appear(client):
    user = await seed_user("goal-owner")
    await grant_person(await get_primary_person_id(), user, "view")
    await create_goal(user, GoalCreate(metric="hrv", target_value=777))
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    client.cookies.set("vf_session", create_session_cookie("goal-owner", user, 1))
    html = await page(client, "?days=7")
    assert "777" not in html and "goal" not in html.lower()


async def test_page_has_no_javascript_and_explains_printing(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    html = await page(client, "?days=7")
    assert "<script" not in html.lower() and "<button" not in html
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)  # no inline event handlers
    assert "Use your browser's Print command (Ctrl/Cmd+P) and choose Save as PDF" in text_of(html).replace("&#39;", "'")
    assert f'href="{PERSON_PREFIX}/"' in html and 'name="robots" content="noindex' in html


async def test_print_stylesheet_hides_toolbar_and_keeps_blocks_together(client):
    css = print_css(await page(client, "?days=7"))
    assert re.search(r"\.toolbar\s*\{[^}]*display:\s*none", css)
    assert re.search(r"\.metric\b[^{}]*\{[^}]*break-inside:\s*avoid", css)
    assert re.search(r"\.finding\b[^{}]*\{[^}]*break-inside:\s*avoid", css)


async def test_sparkline_scale_is_printed_in_display_units(client):
    await seed("sleep", "duration_seconds", [(day(0), 18000), (day(1), 28800)])
    text = text_of(block(await page(client, "?days=7"), "sleep_duration"))
    assert "Plot range: 5 to 8 h" in text


@pytest.mark.parametrize("values", [[1e308, -1e308, 0.0], [1e308, -1e308], [float("inf"), 1.0, float("nan")]])
def test_sparkline_never_emits_non_finite_coordinates(values):
    spark = sparkline(values)
    joined = " ".join([*spark["segments"], *(f"{d['cx']} {d['cy']}" for d in spark["dots"])]).lower()
    assert "nan" not in joined and "inf" not in joined
    assert len(spark["dots"]) == len([v for v in values if v == v and abs(v) != float("inf")])


async def test_overflowing_values_render_without_nan(client):
    await seed("stress", "avg_level", [(day(0), 1e308), (day(1), -1e308), (day(2), 0)])
    svg = svg_of(await page(client, "?days=7"), "stress")
    assert "nan" not in svg.lower() and "inf" not in svg.lower() and svg.count("<circle") == 3


def test_gaps_split_the_polyline_into_separate_segments():
    spark = sparkline([1.0, 2.0, None, None, 3.0, 4.0, 5.0, None, 9.0])
    assert [len(points.split()) for points in spark["segments"]] == [2, 3]  # the lone point is a dot, not a line
    assert len(spark["dots"]) == 6
    assert sparkline([5.0, None, 7.0])["segments"] == []  # two isolated points never join


async def test_severity_does_not_read_as_a_clinical_alarm(client):
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600) + rows(3, 10, 8 * 3600))
    html = await page(client, "?days=7")
    finding = re.search(r'<div class="finding".*?</div>', html, re.DOTALL).group(0)
    assert not re.search(r"\b(alert|warning)\b", finding, re.IGNORECASE)
    assert re.search(r"\((worth noting|notable|for information)\)", finding)
    assert "not a diagnosis" in text_of(html)


async def test_difference_that_rounds_to_zero_is_not_called_a_difference(client):
    await seed("sleep", "duration_seconds", rows(0, 7, 28810) + rows(7, 14, 28800))
    sleep = text_of(block(await page(client, "?days=7"), "sleep_duration"))
    assert "No meaningful change at the displayed precision" in sleep
    assert "Means differ" not in sleep and "Difference (current minus previous)" not in sleep and "Stable" not in sleep


async def test_findings_use_only_the_last_30_dates_even_for_a_90_day_report(client):
    # Recent 30 dates average ~50 with the last 3 slightly lower; the older 150 dates (inside the
    # 180-day read) average 10. Only the 30-date slice makes the recent dip "below baseline".
    await seed("hrv", "last_night_avg", rows(0, 3, 48) + rows(3, 30, 50) + rows(30, 180, 10))
    html = text_of(await page(client, "?days=90"))
    assert "Overnight HRV was below its observed 30-date average in each of the latest 3" in html


async def test_never_recorded_metrics_are_stated_not_silently_absent(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    html = text_of(await page(client, "?days=7"))
    assert "Steps (no value ever recorded)" in html and "Weight (no value ever recorded)" in html
    assert "Overnight HRV (no value" not in html


async def test_blank_display_name_and_multiline_notes(client):
    await rename_primary("")
    await seed("hrv", "last_night_avg", rows(0, 7, 60))
    await put_note(client, day(1), "line one\nline two", [])
    html = await page(client, "?days=7&include_identity=true&include_context=true")
    assert "Profile:" not in html
    assert re.search(r"\.note\s*\{[^}]*white-space:\s*pre-wrap", html)


async def test_comparison_notes_do_not_mention_readiness(client):
    assert "readiness" not in (await page(client, "?days=7")).lower()


async def test_error_and_ok_responses_are_not_cacheable_or_referring(client):
    bad = await client.get(f"{PATH}?days=14")
    assert bad.status_code == 422 and bad.headers["cache-control"] == "no-store, private"
    ok = await client.get(PATH)
    assert ok.headers["referrer-policy"] == "no-referrer" and "noindex" in ok.headers["x-robots-tag"]


async def test_report_is_scoped_to_the_path_person(client):
    other = await seed_person("other-profile", display_name="Other Marker")
    primary = await get_primary_person_id()
    # Disjoint dates and values so a leak cannot be masked by the one-value-per-date collapse.
    await seed("hrv", "last_night_avg", [(day(0), 4242), (day(1), 4243)], person_id=other)
    await seed("hrv", "last_night_avg", [(day(4), 77), (day(5), 78)])
    await seed("sleep", "duration_seconds", [(day(2), 25200)])
    await insert_note(other, day(1), "OTHER-NOTE", ["other-tag"])
    await insert_note(primary, day(3), "PRIMARY-NOTE", ["primary-tag"])
    query = "?days=7&include_context=true&include_identity=true"
    mine = await page(client, query)
    assert "77" in mine and "PRIMARY-NOTE" in mine and "primary-tag" in mine
    for leaked in ("4242", "4243", "OTHER-NOTE", "other-tag", "Other Marker"):
        assert leaked not in mine
    viewer = await seed_user("other-viewer")
    await grant_person(other, viewer, "view")
    client.cookies.set("vf_session", create_session_cookie("other-viewer", viewer, 1))
    theirs = (await client.get(f"/p/other-profile/report{query}")).text
    assert "4242" in theirs and "OTHER-NOTE" in theirs and "Other Marker" in theirs
    for leaked in ("77", "78", "PRIMARY-NOTE", "primary-tag", "25200", 'data-metric="sleep_duration"'):
        assert leaked not in theirs
