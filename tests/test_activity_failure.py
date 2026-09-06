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
    # The row survives; only its Garmin bookkeeping is stale, which the next
    # re-POST repairs (the stored status stays retryable).
    row = await fetch_row()
    assert row is not None
    assert row["garmin_status"] == "pending"


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
