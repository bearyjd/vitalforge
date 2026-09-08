"""Garmin failure handling: the session is stored either way.

The invariant across this whole file is that ONCE THE ROW IS COMMITTED,
nothing downstream may turn it into a 5xx. A 500 over already-durable data
tells the client the whole request failed when it did not, and sends it into a
retry that can create a SECOND Garmin activity for the same session.

The three failure points degrade independently, which is why
garmin_sets_status is a separate column rather than an extra garmin_status
value: the activity can genuinely exist on Garmin while its exercise sets do
not.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db
from tests.conftest import PERSON_PREFIX

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
            await db.execute("SELECT * FROM strength_sessions WHERE session_id = ?", (BODY["session_id"],))
        ).fetchone()
    finally:
        await db.close()


async def test_garmin_failure_still_returns_202(client, weight_app_module, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("garmin is down")

    monkeypatch.setattr(weight_app_module, "push_activity", boom)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "failed"
    assert resp.json()["garmin_error"]

    row = await fetch_row()
    assert row is not None
    assert row["garmin_status"] == "failed"
    assert "garmin is down" in row["garmin_error"]


async def test_sets_failure_leaves_activity_synced(client, weight_app_module, monkeypatch):
    """Only garmin_sets_status degrades. Flipping garmin_status to 'failed'
    here would make the client re-POST and create a SECOND activity for a
    session Garmin already has."""
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")

    def boom(activity_id, payload):
        raise RuntimeError("exerciseSets rejected")

    monkeypatch.setattr(weight_app_module, "push_activity_sets", boom)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert "garmin_error" not in resp.json()

    row = await fetch_row()
    assert row["garmin_status"] == "synced"
    assert row["garmin_sets_status"] == "failed"
    assert row["garmin_activity_id"] == "19283746501"


async def test_post_commit_update_failure_does_not_500(client, weight_app_module, monkeypatch):
    """The row is committed before the push is attempted, so a failure while
    RECORDING the outcome must be logged and swallowed."""
    async def boom(db, row_id, outcome):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(weight_app_module, "_record_activity_garmin_outcome", boom)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    row = await fetch_row()
    assert row is not None
    # 'unknown', NOT 'pending'. The push succeeded and only the write of its
    # outcome failed, so an activity exists on Garmin that this row does not
    # point at. 'pending' is retryable, so once the claim aged out something
    # would push again and duplicate it permanently. The fallback downgrade
    # takes the row out of the retry set and leaves reconciliation as the only
    # way forward.
    assert row["garmin_status"] == "unknown"
    assert row["garmin_error"]
    # The claim is released, so a deliberate re-POST can reconcile at once
    # rather than waiting out the timeout.
    assert row["garmin_claimed_at"] is None
    # The response reports the DURABLE state, not what this request hoped to
    # write -- answering 'synced' over a row that says otherwise would stop
    # the client retrying a session nothing recorded.
    assert resp.json()["garmin_status"] == "unknown"


async def test_no_activity_id_returned_is_synced_not_failed(client, weight_app_module, monkeypatch):
    """The activity really is on Garmin; there is just no id to address it
    by. Never index a response blindly, and never fall back to
    get_last_activity() -- that read is racy and would attach one session's
    sets to a different activity."""
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    monkeypatch.setattr(weight_app_module, "push_activity", lambda **kwargs: {"messages": []})

    called = []
    monkeypatch.setattr(
        weight_app_module, "push_activity_sets", lambda activity_id, payload: called.append(activity_id)
    )

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_activity_id"] is None
    assert resp.json()["garmin_sets_status"] == "not_attempted"
    assert called == []


async def test_bad_tz_degrades_to_failed_not_500(client, monkeypatch):
    """A typo'd TZ is an operator error. It must land as garmin_status
    'failed' with the message stored, not as a 500 over a committed row."""
    monkeypatch.setenv("TZ", "Not/AZone")
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "failed"
    assert (await fetch_row())["garmin_error"]


async def test_unparsable_stored_start_time_does_not_500_on_retry(client):
    """A retry pushes the STORED start_time_utc, which comes back out of
    SQLite as text. Parsing it must happen inside the never-raise helper: a
    row Python's fromisoformat() cannot read would otherwise raise on the
    request path AFTER the row was committed, turning durable data into a
    500 and telling the client the whole request failed when it did not.

    Unreachable through this route today -- every row is written by its own
    .isoformat(). That was equally true of post_weight's timestamp parse
    when the identical bug was found there, which is why it now parses
    defensively too. SQLite's julianday() accepts strings fromisoformat()
    rejects, so a hand-written row or a future write path can produce one.
    """
    from datetime import datetime, timezone

    from shared.database import get_primary_person_id

    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
            "duration_seconds, exercises_json, garmin_status, created_at, updated_at) "
            "VALUES (?, ?, 'Lower A', '2026-09-06 08:00:00 UTC', 2520, '[]', 'failed', ?, ?)",
            (person_id, BODY["session_id"], now, now),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["deduplicated"] is True
    assert resp.json()["garmin_status"] == "failed"
    assert resp.json()["garmin_error"]
    assert (await fetch_row())["garmin_status"] == "failed"


async def test_unparsable_stored_exercises_does_not_500_on_retry(client):
    """The companion to the start_time_utc parse guard.

    A retry pushes the STORED exercises_json, which comes back out of SQLite
    as text. Parsing it in the route -- after commit -- meant an unreadable
    row raised on the request path over durable data, 500ing and sending the
    client into a retry that could create a second Garmin activity.

    Parsed inside _push_activity's try instead, so it degrades to
    garmin_status='failed' with the message stored. 'failed' rather than
    pushing an activity with no exercises: filing a session on Garmin while
    unable to read what was actually done is worse than saying so.
    """
    from datetime import datetime, timezone

    from shared.database import get_primary_person_id

    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
            "duration_seconds, exercises_json, garmin_status, created_at, updated_at) "
            "VALUES (?, ?, 'Lower A', ?, 2520, '{not json', 'failed', ?, ?)",
            (person_id, BODY["session_id"], START, now, now),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "failed"
    assert resp.json()["garmin_error"]
    assert (await fetch_row())["garmin_status"] == "failed"


async def test_corrupt_exercises_does_not_reach_garmin(client, fake_garmin_client):
    """The activity itself needs only start, duration and name, so a corrupt
    exercises blob COULD have been pushed as a bare activity. Deliberately is
    not: an unreadable row is not one to file on someone's Garmin account."""
    from datetime import datetime, timezone

    from shared.database import get_primary_person_id

    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
            "exercises_json, garmin_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 2520, '{not json', 'failed', ?, ?)",
            (person_id, BODY["session_id"], START, now, now),
        )
        await db.commit()
    finally:
        await db.close()

    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert fake_garmin_client.created_activities == []


async def test_garmin_error_redacts_email_and_token(client, weight_app_module, monkeypatch):
    """garmin_error is stored AND returned to the client, so it is the one
    place a raw garminconnect exception string crosses a trust boundary.
    Those strings quote the request being made: a login failure carries the
    account email, and a data call can carry a URL with a token in it."""
    secret_token = "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop"

    def leaky(**kwargs):
        raise RuntimeError(
            f"401 for user jd@beary.us using Bearer {secret_token} at /activity-service"
        )

    monkeypatch.setattr(weight_app_module, "push_activity", leaky)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    error = resp.json()["garmin_error"]
    assert "jd@beary.us" not in error
    assert secret_token not in error
    assert "[redacted]" in error
    # The row is sanitised too, not just the response -- it is read back by
    # the status route and by anyone querying the database directly.
    assert "jd@beary.us" not in (await fetch_row())["garmin_error"]
    assert secret_token not in (await fetch_row())["garmin_error"]


async def test_garmin_error_is_truncated(client, weight_app_module, monkeypatch):
    """An unbounded exception string would be stored per row and echoed on
    every status poll. 300 characters is enough to identify the failure."""
    # Deliberately NOT one long run of word characters: the token redactor
    # would collapse that to "[redacted]" before truncation ever applied, and
    # the test would pass without exercising the length bound at all.
    def verbose(**kwargs):
        raise RuntimeError("Garmin refused the request. " * 200)

    monkeypatch.setattr(weight_app_module, "push_activity", verbose)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    error = resp.json()["garmin_error"]
    assert len(error) <= 300
    assert error.endswith("…")
    assert error.startswith("Garmin refused the request.")


async def test_short_error_is_left_intact(client, weight_app_module, monkeypatch):
    """The sanitiser must not mangle an ordinary message -- an operator
    reading 'failed' rows needs them legible."""
    def plain(**kwargs):
        raise RuntimeError("Garmin returned 503 Service Unavailable")

    monkeypatch.setattr(weight_app_module, "push_activity", plain)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_error"] == "Garmin returned 503 Service Unavailable"


async def test_long_alphanumeric_run_is_redacted_not_just_truncated(client, weight_app_module, monkeypatch):
    """The token pattern is deliberately blunt: any run of 24+ word
    characters goes, because a bearer token, a session id and a signed URL
    fragment all look like that and none of them may be stored. A legitimate
    long identifier being redacted is the accepted cost."""
    def leaky(**kwargs):
        raise RuntimeError("failed with correlation abcdefghijklmnopqrstuvwxyz012345")

    monkeypatch.setattr(weight_app_module, "push_activity", leaky)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.json()["garmin_error"] == "failed with correlation [redacted]"
