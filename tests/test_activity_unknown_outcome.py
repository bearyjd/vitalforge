"""Ambiguous Garmin outcomes: 'unknown', and reconciliation by lookup.

A push that fails AFTER the request is already on the wire -- a read timeout,
a reset connection, a RemoteDisconnected -- tells you nothing about whether
Garmin created the activity. Calling that 'failed' invites a retry that files
a SECOND activity for one session, and this service has no delete path, so the
duplicate is permanent and cleaned up by hand or not at all.

'unknown' is the honest answer. It is deliberately NOT in
_RETRYABLE_GARMIN_STATUSES and is never auto-reclaimed however old its claim
gets, so nothing pushes it automatically. An explicit re-POST asks Garmin
first, by activity name over the session's local date window, and only pushes
if Garmin genuinely does not have it.

The asymmetry is the point: mistaking a real failure for 'unknown' costs one
lookup, mistaking an ambiguous success for 'failed' costs a permanent
duplicate.
"""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, timing_out_until

START = "2026-09-06T08:00:00+00:00"

BODY = {
    "session_id": "cadence-2026-09-06-a3f9",
    "session_label": "Lower A",
    "start": START,
    "duration_min": 42,
    "exercises": [{"name": "Bench Press", "garmin_category": "BENCH_PRESS", "sets": 3, "reps": 10}],
    "push_to_garmin": True,
}


# Every test in this module must be unable to reach real Garmin: app.py binds
# the push helpers into its own namespace, so patching shared.garmin_client
# alone would leave the routes calling the live client and the tests passing.
pytestmark = pytest.mark.usefixtures("no_real_garmin_client")


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def fetch_row():
    db = await get_db()
    try:
        return await (
            await db.execute(
                "SELECT * FROM strength_sessions WHERE session_id = ?", (BODY["session_id"],)
            )
        ).fetchone()
    finally:
        await db.close()


class ReadTimeout(Exception):
    """Stands in for requests.exceptions.ReadTimeout by NAME.

    The classifier matches on class names across the MRO and cause chain
    rather than isinstance, because garminconnect layers requests, urllib3 and
    curl_cffi and which one surfaces is a transport detail a version bump can
    change. Naming a local class this way is exactly what that has to cope
    with.
    """


class ConnectTimeout(Exception):
    """Pre-send: the connection was never established, so nothing was sent."""
# --- classification -----------------------------------------------------------


async def test_timeout_after_send_is_unknown_not_failed(client, weight_app_module, monkeypatch):
    def timing_out(**kwargs):
        raise ReadTimeout("timed out waiting for a response")

    monkeypatch.setattr(weight_app_module, "push_activity", timing_out)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "unknown"
    assert resp.json()["garmin_error"]

    row = await fetch_row()
    assert row["garmin_status"] == "unknown"
    assert row["garmin_activity_id"] is None


async def test_pre_send_failure_stays_failed(client, weight_app_module, monkeypatch):
    """A connection that was never established sent nothing, so the ordinary
    retryable 'failed' is correct and costs no reconciliation."""
    def refused(**kwargs):
        raise ConnectTimeout("could not connect")

    monkeypatch.setattr(weight_app_module, "push_activity", refused)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_status"] == "failed"
    assert (await fetch_row())["garmin_status"] == "failed"


async def test_wrapped_cause_is_classified(client, weight_app_module, monkeypatch):
    """requests wraps urllib3 wraps http.client, so the name that matters is
    routinely two levels down in __cause__, not on the exception raised."""
    def wrapped(**kwargs):
        try:
            raise ReadTimeout("inner")
        except ReadTimeout as inner:
            raise RuntimeError("outer wrapper") from inner

    monkeypatch.setattr(weight_app_module, "push_activity", wrapped)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_status"] == "unknown"


async def test_own_preparation_errors_are_failed_not_unknown(client, monkeypatch):
    """Nothing was sent, because the request was never built -- a bad TZ
    fails before any transport is touched."""
    monkeypatch.setenv("TZ", "Not/AZone")
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_status"] == "failed"


# --- the unknown row is inert -------------------------------------------------


async def test_unknown_row_is_not_auto_retried(client, weight_app_module, fake_garmin_client, monkeypatch):
    """Even with a long-expired claim, an 'unknown' row is never pushed
    without asking Garmin first."""
    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    db = await get_db()
    try:
        await db.execute("UPDATE strength_sessions SET garmin_claimed_at = '2020-01-01T00:00:00+00:00'")
        await db.commit()
    finally:
        await db.close()

    # Garmin now answers normally, and says it does hold the activity.
    state["failing"] = False
    fake_garmin_client.activities_by_date.append(
        {"activityId": 555, "activityName": "Cadence — Lower A [6-a3f9]"}
    )

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_activity_id"] == "555"
    assert fake_garmin_client.created_activities == [], "reconciliation must ask before pushing"


async def test_reconciliation_finds_nothing_and_pushes_once(client, weight_app_module, fake_garmin_client, monkeypatch):
    """Garmin answered and genuinely does not have it, so the push is safe."""
    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert (await fetch_row())["garmin_status"] == "unknown"

    state["failing"] = False
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert len(fake_garmin_client.created_activities) == 1
    assert (await fetch_row())["garmin_status"] == "synced"


async def test_failed_lookup_stays_unknown(client, weight_app_module, fake_garmin_client, monkeypatch):
    """A lookup that FAILS must not collapse into "not found" -- that is
    precisely the mistake that turns one session into two activities."""
    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
            "duration_seconds, exercises_json, garmin_status, created_at, updated_at) "
            "VALUES (?, ?, 'Lower A', ?, 2520, '[]', 'unknown', ?, ?)",
            (person_id, BODY["session_id"], START, now, now),
        )
        await db.commit()
    finally:
        await db.close()

    def unreachable(start_date, end_date, activity_type=None):
        raise ReadTimeout("lookup timed out too")

    monkeypatch.setattr(weight_app_module, "find_activities_by_date", unreachable)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "unknown"
    assert "reconciliation pending" in resp.json()["garmin_error"]
    assert fake_garmin_client.created_activities == []
    assert (await fetch_row())["garmin_status"] == "unknown"


async def test_reconciliation_matches_on_exact_name(client, weight_app_module, fake_garmin_client, monkeypatch):
    """A different session's activity on the same day must not be mistaken
    for this one."""
    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    state["failing"] = False
    fake_garmin_client.activities_by_date.append(
        {"activityId": 999, "activityName": "Cadence — Upper B [6-a3f9]"}
    )

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_activity_id"] != "999"
    assert len(fake_garmin_client.created_activities) == 1


async def test_reconciliation_window_is_local_date_plus_minus_a_day(
    client, weight_app_module, fake_garmin_client, monkeypatch
):
    """Garmin files by its own account-local date, and the name is composed
    from a local wall clock, so a session near midnight can land on the
    neighbouring day."""
    monkeypatch.setenv("TZ", "Europe/Paris")

    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    state["failing"] = False
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    lookup = fake_garmin_client.activity_lookups[0]
    # 08:00Z is 10:00 in Paris on 2026-09-06.
    assert lookup["startdate"] == "2026-09-05"
    assert lookup["enddate"] == "2026-09-07"
    assert lookup["activitytype"] == "strength_training"


async def test_unknown_row_is_inert_without_push_to_garmin(client, fake_garmin_client):
    """No reconciliation lookup happens unless the caller asks to push."""
    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
            "exercises_json, garmin_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 2520, '[]', 'unknown', ?, ?)",
            (person_id, BODY["session_id"], START, now, now),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": False})
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "unknown"
    assert fake_garmin_client.activity_lookups == []


async def test_same_label_same_day_sessions_do_not_collide(
    client, weight_app_module, fake_garmin_client, monkeypatch
):
    """The residual limit the session marker exists to close.

    Reconciliation can only match on a name, because Garmin offers no
    idempotency key. Without the marker, two sessions the same person
    completed on one day under one label -- a morning and an evening
    workout, or a redo -- produce the identical title, and reconciling the
    second would adopt the FIRST one's activityId: the second session would
    read 'synced' pointing at an activity that is not it, and its own push
    would never happen.
    """
    first = {**BODY, "session_id": "cadence-2026-09-06-aaaaaa"}
    second = {**BODY, "session_id": "cadence-2026-09-06-bbbbbb"}

    # The first session lands normally.
    await client.post(f"{PERSON_PREFIX}/api/activity", json=first)
    assert len(fake_garmin_client.created_activities) == 1

    # The second, same label and same day, pushes ambiguously.
    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=second)

    # Reconciling it must NOT find the first session's activity.
    state["failing"] = False
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=second)
    assert resp.json()["garmin_status"] == "synced"
    assert len(fake_garmin_client.created_activities) == 2, (
        "the second session was mistaken for the first and never reached Garmin"
    )

    names = [call["activity_name"] for call in fake_garmin_client.created_activities]
    assert names == ["Cadence — Lower A [aaaaaa]", "Cadence — Lower A [bbbbbb]"]


async def test_reconciliation_searches_for_the_name_the_push_sent(
    client, weight_app_module, fake_garmin_client, monkeypatch
):
    """The name is composed once and used for both. A second composition that
    could drift would search for a title the push never sent, report "not on
    Garmin" for something that is, and duplicate."""
    state = timing_out_until(weight_app_module, monkeypatch)
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    # Garmin holds it under exactly the name the push would have used.
    fake_garmin_client.activities_by_date.append(
        {"activityId": 777, "activityName": "Cadence — Lower A [6-a3f9]"}
    )
    state["failing"] = False

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_activity_id"] == "777"
    assert fake_garmin_client.created_activities == []
