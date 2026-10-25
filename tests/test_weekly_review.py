"""On-demand weekly review: deterministic, rules-only, synthetic local data only."""

from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from shared import garmin_registry
from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, grant_person, seed_person, seed_token, seed_user
from tests.test_data_quality import NOW, seed, sync_status
from vitalforge_dashboard import data_quality, recommendations, weekly_review, weekly_review_routes
from vitalforge_dashboard.goals import GoalCreate, create_goal

PATH = f"{PERSON_PREFIX}/api/weekly-review"
EMPTY_MESSAGE = "No data recorded this week"
CONTEXT_WRITE = {"X-VitalForge-Write": "context"}


@pytest.fixture
async def client(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(data_quality, "utc_now", lambda: NOW)
    async with AsyncClient(transport=ASGITransport(app=dashboard_app_module.app), base_url="http://test") as client:
        yield client


def rows(start: int, end: int, value: float) -> list:
    return [((NOW.date() - timedelta(days=offset)).isoformat(), value) for offset in range(start, end)]


async def review(client) -> dict:
    response = await client.get(PATH)
    assert response.status_code == 200
    return response.json()


async def sign_in(client, username: str, access: str | None = "view") -> int:
    user = await seed_user(username)
    if access:
        await grant_person(await get_primary_person_id(), user, access)
    client.cookies.set("vf_session", create_session_cookie(username, user, 1))
    return user


async def test_empty_week_says_so_and_still_suggests_data_follow_up(client):
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    report = response.json()
    assert report["schema_version"] == "1.0"
    assert report["generated_at"] == NOW.isoformat()
    assert report["period"] == {"start_date": "2026-10-19", "end_date": "2026-10-25", "days": 7}
    assert report["previous_period"] == {"start_date": "2026-10-12", "end_date": "2026-10-18", "days": 7}
    assert report["empty"] is True and report["message"] == EMPTY_MESSAGE
    assert report["changes"] == report["findings"] == report["goals"] == []
    assert report["follow_up"]["kind"] == "data"
    assert report["follow_up"]["text"]


async def test_changes_are_ordered_linked_and_zero_difference_is_stable(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 100) + rows(7, 14, 50))
    await seed("resting_hr", "value", rows(0, 7, 60) + rows(7, 14, 50))
    await seed("stress", "avg_level", rows(0, 7, 30) + rows(7, 14, 30))
    report = await review(client)
    assert report["empty"] is False and report["message"] is None
    assert [change["metric"] for change in report["changes"]] == ["hrv", "resting_hr"]
    change = report["changes"][0]
    assert change["label"] == "Overnight HRV" and change["unit"] == "ms"
    assert change["current"] == {
        "start_date": "2026-10-19", "end_date": "2026-10-25", "n": 7, "mean": 100,
        "dates": sorted(day for day, _ in rows(0, 7, 0)),
    }
    assert change["previous"] == {
        "start_date": "2026-10-12", "end_date": "2026-10-18", "n": 7, "mean": 50,
        "dates": sorted(day for day, _ in rows(7, 14, 0)),
    }
    assert change["difference"] == 50 and change["percent_change"] == 100
    assert [item["metric"] for item in report["stable"]] == ["stress"]
    stable = report["stable"][0]
    assert stable["current"]["mean"] == stable["previous"]["mean"] == 30 and stable["current"]["n"] == 7


async def test_tie_on_percent_change_orders_by_metric_name(client):
    # Table order is resting_hr, hrv; alphabetical order is the reverse, so the tie-break must act.
    await seed("resting_hr", "value", rows(0, 7, 60) + rows(7, 14, 50))
    await seed("hrv", "last_night_avg", rows(0, 7, 60) + rows(7, 14, 50))
    report = await review(client)
    assert [change["metric"] for change in report["changes"]] == ["hrv", "resting_hr"]


async def test_sparse_data_is_suppressed_and_reported_as_partial_coverage(client):
    await seed("resting_hr", "value", rows(0, 2, 60) + rows(7, 14, 50))
    report = await review(client)
    assert report["changes"] == []
    item = next(entry for entry in report["coverage"] if entry["metric"] == "resting_hr")
    assert item["completeness"] == "partial" and item["observed_days"] == 2 and item["missing_days"] == 5
    assert report["empty"] is False


async def test_stale_data_takes_priority_over_findings_and_goals(client):
    user = await sign_in(client, "stale-user")
    await create_goal(user, GoalCreate(metric="hrv", target_value=200))
    await seed("hrv", "last_night_avg", rows(4, 11, 40))
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600) + rows(3, 10, 8 * 3600))
    report = await review(client)
    assert any(item["rule"] == "sleep_low_duration" for item in report["findings"])
    stale = next(item for item in report["coverage"] if item["metric"] == "hrv")
    assert stale["freshness"] == "stale"
    assert report["follow_up"]["kind"] == "data"
    assert "Overnight HRV" in report["follow_up"]["text"]
    assert report["follow_up"]["metrics"] == ["hrv"]


async def test_failed_last_sync_is_surfaced(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 40))
    await sync_status("auth_failed")
    report = await review(client)
    assert report["sync"]["status"] == "failed" and report["sync"]["last_result"] == "auth_failed"
    assert report["follow_up"]["kind"] == "data"


async def test_anomaly_finding_links_metric_period_and_dates(client):
    await seed("hrv", "last_night_avg", rows(4, 25, 40) + rows(0, 3, 80))
    report = await review(client)
    finding = next(item for item in report["findings"] if item["rule"] == "hrv_anomaly")
    assert finding["metric"] == "hrv" and finding["source"] == "comparison_anomaly"
    assert finding["period"] == {"start_date": "2026-09-26", "end_date": "2026-10-25", "days": 30}
    assert finding["observation_dates"] == sorted(day for day, _ in rows(0, 3, 0))
    assert finding["evidence"]["baseline_dates"] and finding["evidence"]["recent_n"] == 3
    assert report["follow_up"]["kind"] == "finding" and report["follow_up"]["metrics"] == ["hrv"]


async def test_rule_finding_evidence_is_built_from_seeded_observations(client):
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600) + rows(3, 10, 8 * 3600))
    report = await review(client)
    finding = next(item for item in report["findings"] if item["rule"] == "sleep_low_duration")
    assert finding["source"] == "rule" and finding["metric"] == "sleep_duration"
    assert finding["label"] == "Sleep duration" and finding["unit"] == "seconds"
    assert finding["period"] == {"start_date": "2026-09-26", "end_date": "2026-10-25", "days": 30}
    expected = sorted(rows(0, 3, 6 * 3600))  # the three sub-7h nights only, never the 8h nights before them
    assert finding["observation_dates"] == [day for day, _ in expected]
    assert finding["evidence"]["observations"] == [{"date": day, "value": value} for day, value in expected]
    assert finding["evidence"]["baseline_dates"] == []
    assert finding["evidence"]["span_days"] == 3
    top = report["findings"][0]
    assert top["observation_dates"][0] in report["follow_up"]["text"] and top["observation_dates"][-1] in report["follow_up"]["text"]
    assert finding["severity"] == "warning"
    assert "consecutive" not in finding["message"] and "3 recorded observations" in finding["message"]


async def test_findings_for_stale_metrics_are_suppressed(client):
    await seed("sleep", "duration_seconds", rows(5, 8, 6 * 3600) + rows(8, 15, 8 * 3600))
    report = await review(client)
    assert report["findings"] == []


async def test_run_rules_and_recommendation_shapes_are_untouched(client):
    data = {"sleep_duration": [{"date": day, "value": 6 * 3600} for day, _ in rows(0, 3, 0)]}
    before = recommendations.run_rules(data)
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600))
    await review(client)
    assert recommendations.run_rules(data) == before
    assert set(before[0]) == {"category", "severity", "rule", "message", "data"}


async def test_findings_ordered_by_severity_then_metric(client):
    await seed("hrv", "last_night_avg", rows(4, 25, 40) + rows(0, 3, 80))
    await seed("sleep", "duration_seconds", rows(0, 3, 6 * 3600) + rows(3, 10, 8 * 3600))
    findings = (await review(client))["findings"]
    ranks = {"alert": 0, "warning": 1, "info": 2}
    keys = [(ranks[item["severity"]], item["metric"], item["rule"]) for item in findings]
    assert keys == sorted(keys) and findings[0]["severity"] == "alert"


def ramp(start_value: float, step: float, days: int = 14) -> list:
    """Oldest-first slow ramp ending today at start_value + step * (days - 1)."""
    return [((NOW.date() - timedelta(days=days - 1 - i)).isoformat(), start_value + step * i) for i in range(days)]


async def test_goal_follow_up_ranks_only_off_track_goals_furthest_behind_first(client):
    user = await sign_in(client, "goal-user")
    tomorrow = (NOW.date() + timedelta(days=1)).isoformat()
    # Creation and alphabetical order both put hrv first; the larger gap is resting_hr.
    await create_goal(user, GoalCreate(metric="hrv", target_value=60, target_date=tomorrow))
    await create_goal(user, GoalCreate(metric="resting_hr", target_value=50, target_date=tomorrow))
    # Already exceeded and still rising: no projection, so never "behind", whatever its distance.
    await create_goal(user, GoalCreate(metric="steps", target_value=10000))
    await seed("hrv", "last_night_avg", ramp(50, 0.1, 12))
    await seed("resting_hr", "value", ramp(70, -0.1, 12))
    await seed("steps", "value", ramp(30000, 10, 12))
    report = await review(client)
    assert report["findings"] == []
    by_metric = {goal["metric"]: goal for goal in report["goals"]}
    assert by_metric["hrv"]["on_track"] is False and by_metric["resting_hr"]["on_track"] is False
    assert by_metric["steps"]["on_track"] is None
    assert by_metric["hrv"]["latest_value"] == pytest.approx(51.1)
    assert by_metric["hrv"]["latest_observation_date"] == NOW.date().isoformat()
    assert by_metric["hrv"]["stale"] is False
    assert by_metric["hrv"]["week_mean"] == pytest.approx(50.8) and by_metric["hrv"]["week_n"] == 7
    assert by_metric["hrv"]["previous_n"] == 5
    assert report["follow_up"]["kind"] == "goal" and report["follow_up"]["metrics"] == ["resting_hr"]


async def test_goal_with_old_latest_value_is_dated_and_marked_stale(client):
    user = await sign_in(client, "old-goal-user")
    await create_goal(user, GoalCreate(metric="hrv", target_value=60, target_date="2026-10-26"))
    await seed("hrv", "last_night_avg", [(day, 50 + i * 0.1) for i, (day, _) in enumerate(reversed(rows(10, 24, 0)))])
    goal = (await review(client))["goals"][0]
    assert goal["latest_observation_date"] == rows(10, 11, 0)[0][0] and goal["stale"] is True


async def test_goal_progress_uses_the_requested_profile_not_the_primary_one(client):
    primary = await get_primary_person_id()
    other = await seed_person("other")
    user = await sign_in(client, "two-profile")
    await grant_person(other, user, "view")
    await create_goal(user, GoalCreate(metric="hrv", target_value=100))
    await seed("hrv", "last_night_avg", rows(0, 7, 11), primary)
    await seed("hrv", "last_night_avg", rows(0, 7, 77), other)
    goal = (await client.get("/p/other/api/weekly-review")).json()["goals"][0]
    assert goal["latest_value"] == 77 and goal["week_mean"] == 77
    assert "11" not in str(goal)


async def test_nothing_to_follow_up_when_everything_is_current_and_quiet(client):
    await seed("hrv", "last_night_avg", rows(0, 14, 40))
    await sync_status("success")
    report = await review(client)
    assert report["findings"] == [] and report["goals"] == []
    assert report["follow_up"]["kind"] == "none"


async def test_goals_present_for_account_and_never_another_users(client):
    user = await sign_in(client, "owner")
    other_user = await seed_user("someone-else")
    await grant_person(await get_primary_person_id(), other_user, "view")
    await create_goal(user, GoalCreate(metric="steps", target_value=10000))
    await create_goal(other_user, GoalCreate(metric="weight", target_value=70000))
    await seed("steps", "value", rows(0, 7, 5000))
    goals = (await review(client))["goals"]
    assert [goal["metric"] for goal in goals] == ["steps"]


async def test_bearer_account_gets_its_goals(client):
    user = await seed_user("bearer-user")
    await grant_person(await get_primary_person_id(), user, "view")
    await create_goal(user, GoalCreate(metric="steps", target_value=10000))
    _, token = await seed_token(user)
    client.headers["Authorization"] = f"Bearer {token}"
    assert [goal["metric"] for goal in (await review(client))["goals"]] == ["steps"]


async def test_anonymous_profile_access_gets_empty_goals_not_401(client):
    # No account exists, so auth is open: there is no identity to own goals.
    await seed("hrv", "last_night_avg", rows(0, 7, 40))
    report = await review(client)
    assert report["goals"] == []


async def test_unauthenticated_request_is_401_once_accounts_exist(client):
    await seed_user("exists")
    assert (await client.get(PATH)).status_code == 401


async def test_no_grant_and_unknown_profiles_are_404_and_isolated(client):
    primary = await get_primary_person_id()
    other = await seed_person("other")
    await seed("steps", "value", rows(0, 7, 10), primary)
    await seed("steps", "value", rows(0, 7, 99999), other)
    user = await sign_in(client, "viewer", access="view")
    assert user
    response = await client.get(PATH)
    assert response.status_code == 200 and "99999" not in response.text
    for slug in ("other", "missing"):
        assert (await client.get(f"/p/{slug}/api/weekly-review")).status_code == 404


async def test_user_without_any_grant_is_404(client):
    await sign_in(client, "no-grant", access=None)
    assert (await client.get(PATH)).status_code == 404


async def test_archived_profile_is_404(client):
    archived = await seed_person("archived")
    user = await sign_in(client, "archivist", access=None)
    await grant_person(archived, user, "view")
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), archived))
        await db.commit()
    finally:
        await db.close()
    assert (await client.get("/p/archived/api/weekly-review")).status_code == 404


async def test_context_notes_and_tags_are_never_read_or_returned(client, monkeypatch):
    await seed("hrv", "last_night_avg", rows(0, 7, 40))
    put = await client.put(
        f"{PERSON_PREFIX}/api/context/days/2026-10-25",
        json={"note": "private-note-xyz", "tags": ["private-tag-xyz"]}, headers=CONTEXT_WRITE,
    )
    assert put.status_code == 200
    original = weekly_review.profile_snapshot
    calls = []

    async def spy(person_id, days, **kwargs):
        calls.append(kwargs)
        return await original(person_id, days, **kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("The weekly review must not read context")

    monkeypatch.setattr(weekly_review, "profile_snapshot", spy)
    monkeypatch.setattr("vitalforge_dashboard.context_store.read_context", forbidden)
    response = await client.get(PATH)
    assert response.status_code == 200
    assert "private-note-xyz" not in response.text and "private-tag-xyz" not in response.text
    assert calls and all(not call.get("include_context") for call in calls)
    assert not {"context", "notes", "tags"} & set(response.json())


async def test_review_is_local_only_and_deterministic(client, monkeypatch):
    user = await sign_in(client, "determinism")
    await create_goal(user, GoalCreate(metric="hrv", target_value=90, target_date="2026-10-26"))
    await seed("hrv", "last_night_avg", rows(4, 25, 40) + rows(0, 3, 80))

    async def forbidden(*args, **kwargs):
        pytest.fail("The weekly review must not contact Garmin or an LLM")

    monkeypatch.setattr(garmin_registry, "call", forbidden)
    monkeypatch.setattr(recommendations, "get_recommendations", forbidden)
    monkeypatch.setattr(recommendations, "get_llm_recommendations", forbidden)
    assert await review(client) == await review(client)


def test_follow_up_priority_is_fixed_and_text_is_non_diagnostic():
    finding = {"metric": "hrv", "label": "Overnight HRV", "severity": "alert", "observation_dates": ["2026-10-24", "2026-10-25"]}
    goal = {"metric": "steps", "label": "Steps", "on_track": False, "latest_value": 1, "target_value": 10, "stale": False}
    stale = [{"metric": "weight", "label": "Weight", "freshness": "stale"}]
    sync = {"status": "success"}
    assert weekly_review.choose_follow_up(stale, [finding], [goal], False, sync)["kind"] == "data"
    assert weekly_review.choose_follow_up([], [finding], [goal], False, sync)["kind"] == "finding"
    assert weekly_review.choose_follow_up([], [], [goal], False, sync)["kind"] == "goal"
    assert weekly_review.choose_follow_up([], [], [], False, sync)["kind"] == "none"
    for unqualified in ({"on_track": None}, {"on_track": True}, {"stale": True}, {"latest_value": None}):
        assert weekly_review.choose_follow_up([], [], [{**goal, **unqualified}], False, sync)["kind"] == "none"
    assert weekly_review.choose_follow_up([], [finding], [goal], True, sync)["kind"] == "data"
    for chosen in (stale, []):
        text = weekly_review.choose_follow_up(chosen, [finding], [goal], False, sync)["text"].lower()
        assert not any(word in text for word in ("diagnos", "treat", "because", "caused", "medical"))


def test_choose_follow_up_does_not_mutate_inputs():
    findings = [{"metric": "b", "label": "B", "severity": "warning", "observation_dates": ["2026-10-25"]},
                {"metric": "a", "label": "A", "severity": "warning", "observation_dates": ["2026-10-25"]}]
    snapshot = [dict(item) for item in findings]
    chosen = weekly_review.choose_follow_up([], findings, [], False, {"status": "success"})
    assert findings == snapshot and chosen["metrics"] == ["a"]


async def test_reads_one_snapshot_with_one_pinned_now(client, monkeypatch):
    user = await sign_in(client, "one-snapshot")
    await create_goal(user, GoalCreate(metric="hrv", target_value=90))
    await seed("hrv", "last_night_avg", rows(0, 14, 40))
    clock = iter([NOW, NOW + timedelta(days=1)] + [NOW + timedelta(days=2)] * 20)
    calls = []
    monkeypatch.setattr(data_quality, "utc_now", lambda: calls.append(1) or next(clock))
    report = await review(client)
    assert len(calls) == 1
    assert report["period"]["end_date"] == NOW.date().isoformat()
    assert report["generated_at"] == NOW.isoformat()
    assert report["changes"] == [] and report["stable"][0]["current"]["dates"][-1] == NOW.date().isoformat()


async def test_non_401_identity_errors_are_reraised_not_swallowed(client, monkeypatch):
    from fastapi import HTTPException

    async def denied(request):
        raise HTTPException(status_code=403, detail="scope")

    monkeypatch.setattr(weekly_review_routes, "require_account_identity", denied)
    assert (await client.get(PATH)).status_code == 403


async def test_account_without_user_id_gets_no_goals(client, monkeypatch):
    class Anonymous:
        user_id = None

    async def anonymous(request):
        return Anonymous()

    monkeypatch.setattr(weekly_review_routes, "require_account_identity", anonymous)
    assert (await review(client))["goals"] == []


def _days(count: int) -> list[str]:
    start = date(2026, 9, 26)
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]


def _series(values: list) -> list[dict]:
    return [{"date": day, "value": value} for day, value in zip(_days(len(values)), values, strict=True)]


def _flat(value: float, tail: list) -> list:
    return [value] * (30 - len(tail)) + tail


# rule -> (metric, 30 oldest-first values making it fire, TRIGGERING rows, baseline rows or None).
# Slices are written out independently of the module. Triggering rows are only those that
# satisfied the claim (a streak's own rows, a trend's fit window, the compared week), never
# the wider window the rule scanned; baselines are listed separately.
RULE_CASES = {
    "sleep_low_duration": ("sleep_duration", _flat(28800, [21600] * 3), slice(27, 30), None),
    "sleep_declining": ("sleep_duration", [30000 - 200 * i for i in range(30)], slice(16, 30), None),
    "sleep_low_score": ("sleep_score", _flat(80, [60] * 3), slice(27, 30), None),
    "hrv_below_baseline": ("hrv", _flat(60, [30] * 3), slice(27, 30), slice(0, 30)),
    "hrv_weekly_drop": ("hrv", _flat(60, [30] * 3), slice(23, 30), slice(16, 23)),
    "rhr_elevated": ("resting_hr", _flat(50, [60]), slice(29, 30), slice(0, 30)),
    "rhr_trending_up": ("resting_hr", [50 + 0.5 * i for i in range(30)], slice(16, 30), None),
    "body_battery_low": ("body_battery", _flat(90, [50] * 3), slice(27, 30), None),
    "stress_high": ("stress", _flat(30, [70] * 3), slice(27, 30), None),
    "stress_trending_up": ("stress", [30 + i for i in range(30)], slice(16, 30), None),
}


def _stub_comparison(metric: str) -> dict:
    return {
        "metrics": {metric: {"freshness": "current", "label": metric.title(), "unit": "u"}},
        "findings_period": {"start_date": _days(30)[0], "end_date": _days(30)[-1], "days": 30},
    }


def test_every_allowlisted_rule_is_covered_by_an_evidence_case():
    assert set(RULE_CASES) == set(weekly_review.RULE_METRICS)


@pytest.mark.parametrize("rule", sorted(RULE_CASES))
def test_rule_evidence_dates_equal_every_row_the_rule_read(rule):
    metric, values, current, baseline = RULE_CASES[rule]
    data_rows = _series(values)
    found = weekly_review._rule_findings(_stub_comparison(metric), {metric: data_rows})
    finding = next(item for item in found if item["rule"] == rule)
    assert finding["metric"] == metric and finding["period"]["days"] == 30
    assert finding["observation_dates"] == [row["date"] for row in data_rows[current]]
    expected_baseline = [row["date"] for row in data_rows[baseline]] if baseline else []
    assert finding["evidence"]["baseline_dates"] == expected_baseline
    assert finding["evidence"]["observations"] == data_rows[current]
    assert finding["evidence"]["baseline_observations"] == (data_rows[baseline] if baseline else [])
    first, last = (date.fromisoformat(day) for day in (finding["observation_dates"][0], finding["observation_dates"][-1]))
    assert finding["evidence"]["span_days"] == (last - first).days + 1
    for word in ("consecutive", "2 weeks", "week-over-week"):
        assert word not in finding["message"]


def test_rule_evidence_reports_calendar_span_when_rows_have_gaps():
    metric, values, _, _ = RULE_CASES["sleep_low_duration"]
    gapped = _series(values)[:24] + [{"date": day, "value": 21600} for day in ("2026-10-21", "2026-10-24", "2026-10-26")]
    found = weekly_review._rule_findings(_stub_comparison(metric), {metric: gapped})
    finding = next(item for item in found if item["rule"] == "sleep_low_duration")
    assert finding["observation_dates"] == ["2026-10-21", "2026-10-24", "2026-10-26"]
    assert finding["evidence"]["span_days"] == 6


@pytest.mark.parametrize("streak", [3, 4, 5])
def test_streak_evidence_is_exactly_the_qualifying_run_for_any_length(streak):
    values = _flat(28800, [21600] * streak)
    data_rows = _series(values)
    found = weekly_review._rule_findings(_stub_comparison("sleep_duration"), {"sleep_duration": data_rows})
    finding = next(item for item in found if item["rule"] == "sleep_low_duration")
    assert finding["observation_dates"] == [row["date"] for row in data_rows[-streak:]]
    assert all(row["value"] == 21600 for row in finding["evidence"]["observations"])


def test_follow_up_date_range_derives_from_triggering_rows_only():
    data_rows = _series(_flat(28800, [21600] * 3))
    found = weekly_review._rule_findings(_stub_comparison("sleep_duration"), {"sleep_duration": data_rows})
    finding = next(item for item in found if item["rule"] == "sleep_low_duration")
    text = weekly_review.choose_follow_up([], [finding], [], False, {"status": "success"})["text"]
    assert data_rows[-3]["date"] in text and data_rows[-1]["date"] in text and data_rows[-4]["date"] not in text


async def test_goal_progress_agrees_with_goals_api_on_a_sparse_metric(client, monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(data_quality, "utc_now", lambda: now)
    user = await sign_in(client, "weigher")
    far = (now.date() + timedelta(days=3650)).isoformat()
    await create_goal(user, GoalCreate(metric="weight", target_value=60000, target_date=far))
    # Weekly weigh-ins over 80 days, quadratic so a 30-day slope differs from a 90-day one.
    weigh_ins = [((now.date() - timedelta(days=2 + 7 * i)).isoformat(), 90000 - 20 * (11 - i) ** 2) for i in range(12)]
    await seed("weight_history", "weight_grams", weigh_ins)
    api = (await client.get(f"{PERSON_PREFIX}/api/goals")).json()[0]["progress"]
    goal = next(item for item in (await review(client))["goals"] if item["metric"] == "weight")
    assert goal["latest_value"] == api["latest_value"]
    assert goal["eta_date"] == api["eta_date"] and goal["on_track"] == api["on_track"]


async def test_goal_whose_projection_overflows_is_skipped_with_a_note_not_a_500(client):
    user = await sign_in(client, "overflow")
    await create_goal(user, GoalCreate(metric="hrv", target_value=1.7e308))
    await create_goal(user, GoalCreate(metric="hrv", target_value=90, target_date="2026-10-26"))
    await seed("hrv", "last_night_avg", [(day, 40 + i * 0.1) for i, (day, _) in enumerate(reversed(rows(0, 12, 0)))])
    response = await client.get(PATH)
    assert response.status_code == 200
    report = response.json()
    assert [goal["target_value"] for goal in report["goals"]] == [90]
    assert any("Overnight HRV" in note and "skipped" in note for note in report["limitations"])


async def test_means_equal_up_to_float_noise_are_stable_not_a_change(client):
    await seed("resting_hr", "value", rows(0, 7, 0.30000000000000004) + rows(7, 14, 0.3))
    report = await review(client)
    assert [item["metric"] for item in report["stable"]] == ["resting_hr"]
    assert report["changes"] == []


async def test_exactly_equal_means_are_stable_not_insufficient(client):
    await seed("hrv", "last_night_avg", rows(0, 14, 50))
    report = await review(client)
    assert [item["metric"] for item in report["stable"]] == ["hrv"] and report["changes"] == []


async def test_comparison_report_rejects_a_snapshot_shorter_than_it_needs(client):
    from vitalforge_dashboard.comparisons import comparison_report

    quality, data, _ = await data_quality.profile_snapshot(await get_primary_person_id(), 7)
    with pytest.raises(ValueError, match="at least"):
        await comparison_report(await get_primary_person_id(), 7, (quality, data))


async def test_week_with_only_snapshot_metrics_is_not_empty_but_asks_for_daily_data(client):
    await seed("weight_history", "weight_grams", rows(0, 3, 70000))
    report = await review(client)
    assert report["empty"] is False and report["message"] is None
    assert report["follow_up"]["kind"] == "data" and "daily" in report["follow_up"]["text"]
    assert any(item["completeness"] == "empty" for item in report["coverage"])


async def test_week_with_any_daily_observation_does_not_get_the_no_daily_data_follow_up(client):
    await seed("weight_history", "weight_grams", rows(0, 3, 70000))
    await seed("hrv", "last_night_avg", rows(0, 7, 50) + rows(7, 14, 50))
    assert (await review(client))["follow_up"]["kind"] == "none"


async def test_rules_see_only_the_30_date_window_not_older_history(client):
    # Older history would raise the all-rows average enough to hide the elevation.
    await seed("resting_hr", "value", rows(0, 1, 56) + rows(1, 30, 50) + rows(31, 90, 100))
    findings = (await review(client))["findings"]
    finding = next(item for item in findings if item["rule"] == "rhr_elevated")
    assert min(finding["evidence"]["baseline_dates"]) >= "2026-09-26"


async def test_goal_basis_includes_exactly_90_days_back_like_the_goals_api(client, monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(data_quality, "utc_now", lambda: now)
    user = await sign_in(client, "edge")
    await create_goal(user, GoalCreate(metric="weight", target_value=60000))
    edge = (now.date() - timedelta(days=90)).isoformat()
    beyond = (now.date() - timedelta(days=91)).isoformat()
    await seed("weight_history", "weight_grams", [(edge, 81000), (beyond, 99999)])
    api = (await client.get(f"{PERSON_PREFIX}/api/goals")).json()[0]["progress"]
    goal = (await review(client))["goals"][0]
    assert api["latest_value"] == 81000 and goal["latest_value"] == 81000


async def test_review_deliberately_ignores_rows_dated_after_today_for_goals(client):
    # profile_snapshot never reads dates after today (UTC); /api/goals has no upper bound.
    # The review stays consistent with its own period instead of using a not-yet-current value.
    user = await sign_in(client, "future")
    await create_goal(user, GoalCreate(metric="hrv", target_value=90))
    future = (NOW.date() + timedelta(days=1)).isoformat()
    await seed("hrv", "last_night_avg", rows(0, 7, 50) + [(future, 58)])
    assert (await review(client))["goals"][0]["latest_value"] == 50


async def test_exactly_seven_observations_is_complete_and_six_is_partial(client):
    await seed("hrv", "last_night_avg", rows(0, 7, 50))
    await seed("resting_hr", "value", rows(0, 6, 50))
    coverage = {item["metric"]: item for item in (await review(client))["coverage"]}
    assert "hrv" not in coverage
    assert coverage["resting_hr"]["completeness"] == "partial" and coverage["resting_hr"]["observed_days"] == 6


@pytest.mark.parametrize(("age", "stale"), [(3, False), (4, True)])
async def test_goal_staleness_boundary_is_more_than_three_days(client, age, stale):
    user = await sign_in(client, f"age-{age}")
    await create_goal(user, GoalCreate(metric="hrv", target_value=90))
    await seed("hrv", "last_night_avg", rows(age, age + 10, 50))
    assert (await review(client))["goals"][0]["stale"] is stale


def test_goal_gap_is_relative_to_the_target_not_absolute():
    small_relative = {"metric": "a", "label": "A", "on_track": False, "latest_value": 900, "target_value": 1000, "stale": False}
    large_relative = {"metric": "b", "label": "B", "on_track": False, "latest_value": 5, "target_value": 10, "stale": False}
    chosen = weekly_review.choose_follow_up([], [], [small_relative, large_relative], False, {"status": "success"})
    assert chosen["metrics"] == ["b"]
