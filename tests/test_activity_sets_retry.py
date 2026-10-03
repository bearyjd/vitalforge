"""Issue #66: a re-POST repairs a failed exercise-set upload on a synced activity.

After create_manual_activity succeeds the row is garmin_status='synced'. If the
follow-up set_activity_exercise_sets call fails, garmin_sets_status='failed'
and -- before this fix -- nothing ever tried again: the re-POST retry only
covers 'pending'/'failed' activities and reconciles 'unknown' ones.

The retry re-sends ONLY the sets, against the activity id the row already
holds. It must never call create_manual_activity (that would file a second,
permanent activity for one session) and never move garmin_status off
'synced'. It is gated on VITALFORGE_GARMIN_EXERCISE_SETS, which ships off.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, PRIMARY_SLUG, grant_person, seed_person, seed_user

START = "2026-09-06T08:00:00+00:00"
SESSION_ID = "cadence-2026-09-06-a3f9"
STORED_ACTIVITY_ID = "55500011"

EXERCISES_JSON = (
    '[{"name": "Bench Press", "garmin_category": "BENCH_PRESS", '
    '"garmin_exercise": "BARBELL_BENCH_PRESS", "sets": 3, "reps": 10, "weight_kg": 40.0}]'
)

BODY = {
    "session_id": SESSION_ID,
    "session_label": "Lower A",
    "start": START,
    "duration_min": 42,
    "exercises": [
        {
            "name": "Bench Press",
            "garmin_category": "BENCH_PRESS",
            "garmin_exercise": "BARBELL_BENCH_PRESS",
            "sets": 3,
            "reps": 10,
            "weight_kg": 40.0,
        }
    ],
    "push_to_garmin": True,
}


pytestmark = pytest.mark.usefixtures("no_real_garmin_client")


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sets_on(monkeypatch):
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    monkeypatch.setenv("TZ", "UTC")


async def seed_synced_row(
    person_id: int,
    *,
    session_id: str = SESSION_ID,
    garmin_activity_id: str | None = STORED_ACTIVITY_ID,
    garmin_sets_status: str = "failed",
    garmin_claimed_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
            "duration_seconds, exercises_json, garmin_status, garmin_activity_id, garmin_sets_status, "
            "garmin_claimed_at, created_at, updated_at) "
            "VALUES (?, ?, 'Lower A', ?, 2520, ?, 'synced', ?, ?, ?, ?, ?)",
            (
                person_id, session_id, START, EXERCISES_JSON, garmin_activity_id,
                garmin_sets_status, garmin_claimed_at, now, now,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def fetch_row(person_id: int, session_id: str = SESSION_ID):
    db = await get_db()
    try:
        return await (
            await db.execute(
                "SELECT * FROM strength_sessions WHERE person_id = ? AND session_id = ?",
                (person_id, session_id),
            )
        ).fetchone()
    finally:
        await db.close()


async def test_end_to_end_failed_sets_are_repaired_without_a_second_activity(
    client, fake_garmin_client, monkeypatch, activity_garmin_module, sets_on
):
    """The issue's exact sequence through the real push path: create
    succeeds, sets fail, the re-POST re-sends only the sets."""
    real_push_sets = activity_garmin_module.push_activity_sets

    def boom(activity_id, payload):
        raise RuntimeError("exerciseSets rejected")

    monkeypatch.setattr(activity_garmin_module, "push_activity_sets", boom)
    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert first.json()["garmin_status"] == "synced"
    assert first.json()["garmin_sets_status"] == "failed"
    activity_id = first.json()["garmin_activity_id"]

    monkeypatch.setattr(activity_garmin_module, "push_activity_sets", real_push_sets)
    second = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert second.status_code == 200
    assert second.json()["garmin_status"] == "synced"
    assert second.json()["garmin_sets_status"] == "synced"
    assert second.json()["garmin_activity_id"] == activity_id
    assert len(fake_garmin_client.created_activities) == 1
    assert [p["activity_id"] for p in fake_garmin_client.pushed_exercise_sets] == [activity_id]

    row = await fetch_row(await get_primary_person_id())
    assert row["garmin_status"] == "synced"
    assert row["garmin_sets_status"] == "synced"
    assert row["garmin_activity_id"] == activity_id
    assert row["garmin_claimed_at"] is None


async def test_retry_succeeds_against_the_stored_activity_id(client, fake_garmin_client, sets_on):
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["garmin_status"] == "synced"
    assert body["garmin_sets_status"] == "synced"
    assert body["garmin_activity_id"] == STORED_ACTIVITY_ID
    assert "garmin_error" not in body
    assert fake_garmin_client.created_activities == []
    assert len(fake_garmin_client.pushed_exercise_sets) == 1
    pushed = fake_garmin_client.pushed_exercise_sets[0]
    assert pushed["activity_id"] == STORED_ACTIVITY_ID
    # Rebuilt from the STORED exercises: 3 sets of the stored movement.
    assert len(pushed["payload"]["exerciseSets"]) == 3
    assert pushed["payload"]["exerciseSets"][0]["startTime"] == "2026-09-06T08:00:00.0"
    # Through the registry, with the interactive permit budget.
    assert fake_garmin_client.registry_calls == [(person_id, 1)]
    assert fake_garmin_client.registry_budgets == [10.0]

    row = await fetch_row(person_id)
    assert row["garmin_status"] == "synced"
    assert row["garmin_sets_status"] == "synced"
    assert row["garmin_activity_id"] == STORED_ACTIVITY_ID
    assert row["garmin_claimed_at"] is None


async def test_retry_fails_again_stays_failed_with_bounded_logging(
    client, fake_garmin_client, monkeypatch, activity_garmin_module, sets_on, caplog
):
    secret = "Bearer sk-live-SECRET token for jd@example.invalid"

    def boom(activity_id, payload):
        raise RuntimeError(secret)

    monkeypatch.setattr(activity_garmin_module, "push_activity_sets", boom)
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert secret not in resp.text
    assert fake_garmin_client.created_activities == []

    row = await fetch_row(person_id)
    assert row["garmin_status"] == "synced"
    assert row["garmin_sets_status"] == "failed"
    assert row["garmin_activity_id"] == STORED_ACTIVITY_ID
    assert row["garmin_error"] is None
    # Released, so the next re-POST may try again at once.
    assert row["garmin_claimed_at"] is None
    assert secret not in caplog.text
    assert "activity_sets_upload_failed" in caplog.text


async def test_registry_failure_on_retry_uses_bounded_code(
    client, fake_garmin_client, monkeypatch, sets_on, caplog
):
    from shared import garmin_registry

    async def not_linked(person_id, operation, *, max_wait_seconds: float = 0.0):
        raise garmin_registry.GarminNotLinked("person 1 has no link; secret-detail")

    monkeypatch.setattr(garmin_registry, "call", not_linked)
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert "secret-detail" not in resp.text
    assert "secret-detail" not in caplog.text
    assert "link_required" in caplog.text
    row = await fetch_row(person_id)
    assert row["garmin_status"] == "synced"
    assert row["garmin_sets_status"] == "failed"
    assert row["garmin_claimed_at"] is None


async def test_flag_off_does_not_retry(client, fake_garmin_client, monkeypatch):
    monkeypatch.delenv("VITALFORGE_GARMIN_EXERCISE_SETS", raising=False)
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert fake_garmin_client.registry_calls == []
    assert fake_garmin_client.pushed_exercise_sets == []
    assert fake_garmin_client.created_activities == []
    row = await fetch_row(person_id)
    assert row["garmin_sets_status"] == "failed"
    assert row["garmin_claimed_at"] is None


@pytest.mark.parametrize("activity_id", [None, ""])
async def test_row_without_activity_id_is_not_retried(client, fake_garmin_client, sets_on, activity_id):
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id, garmin_activity_id=activity_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert fake_garmin_client.registry_calls == []
    assert fake_garmin_client.created_activities == []
    row = await fetch_row(person_id)
    assert row["garmin_claimed_at"] is None


@pytest.mark.parametrize("sets_status", ["synced", "not_attempted"])
async def test_sets_not_failed_is_not_retried(client, fake_garmin_client, sets_on, sets_status):
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id, garmin_sets_status=sets_status)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    assert resp.json()["garmin_sets_status"] == sets_status
    assert fake_garmin_client.registry_calls == []
    assert fake_garmin_client.created_activities == []


async def test_push_to_garmin_false_does_not_retry(client, fake_garmin_client, sets_on):
    """Same shape as the push and reconcile branches: a re-POST that says
    not to push never reaches Garmin."""
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": False})

    assert resp.json()["garmin_sets_status"] == "failed"
    assert fake_garmin_client.registry_calls == []


async def test_live_claim_blocks_a_concurrent_sets_retry(client, fake_garmin_client, sets_on):
    person_id = await get_primary_person_id()
    claimed_at = datetime.now(timezone.utc).isoformat()
    await seed_synced_row(person_id, garmin_claimed_at=claimed_at)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    # Still true: the activity is synced and no sets upload has succeeded.
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "failed"
    assert fake_garmin_client.registry_calls == []
    row = await fetch_row(person_id)
    # The other claimant's claim is left alone.
    assert row["garmin_claimed_at"] == claimed_at
    assert row["garmin_sets_status"] == "failed"


async def test_stale_claim_is_retaken_for_a_sets_retry(client, fake_garmin_client, sets_on, garmin_claim_module):
    person_id = await get_primary_person_id()
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=garmin_claim_module._GARMIN_CLAIM_TIMEOUT_SECONDS + 60)
    ).isoformat()
    await seed_synced_row(person_id, garmin_claimed_at=stale)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.json()["garmin_sets_status"] == "synced"
    assert fake_garmin_client.created_activities == []
    row = await fetch_row(person_id)
    assert row["garmin_claimed_at"] is None


async def test_claim_is_held_during_the_sets_call(
    client, fake_garmin_client, monkeypatch, activity_garmin_module, sets_on
):
    """The claim is taken in the BEGIN IMMEDIATE before the Garmin call and
    released by the recorded outcome -- a concurrent re-POST arriving while
    the sets call is in flight sees a live claim."""
    seen: list = []
    real = activity_garmin_module.push_activity_sets

    def observe(activity_id, payload):
        import sqlite3

        from shared import database

        conn = sqlite3.connect(database.DB_PATH)
        try:
            seen.append(conn.execute("SELECT garmin_claimed_at, garmin_status FROM strength_sessions").fetchone())
        finally:
            conn.close()
        return real(activity_id, payload)

    monkeypatch.setattr(activity_garmin_module, "push_activity_sets", observe)
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert len(seen) == 1
    claimed_at, status = seen[0]
    assert claimed_at is not None
    # The claim never touched garmin_status.
    assert status == "synced"
    assert (await fetch_row(person_id))["garmin_claimed_at"] is None


async def test_outcome_write_failure_never_downgrades_a_synced_row(
    client, fake_garmin_client, monkeypatch, activity_routes_module, sets_on
):
    """On the push path a failed outcome write downgrades to 'unknown',
    because an activity may exist that the row does not point at. Here the
    row already points at it, so that downgrade would only make a synced
    activity unretryable -- the row must stay 'synced'."""
    async def boom(db, row_id, outcome):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(activity_routes_module, "_record_activity_garmin_outcome", boom)
    person_id = await get_primary_person_id()
    await seed_synced_row(person_id)

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    assert resp.status_code == 200
    row = await fetch_row(person_id)
    assert row["garmin_status"] == "synced"
    assert row["garmin_activity_id"] == STORED_ACTIVITY_ID
    assert fake_garmin_client.created_activities == []
    # Reports the durable state, not the in-memory hope.
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == row["garmin_sets_status"]


async def test_other_persons_identical_session_is_untouched(client, fake_garmin_client, sets_on):
    username = "alice"
    user_id = await seed_user(username)
    cookies = {"vf_session": create_session_cookie(username, user_id, 1)}
    primary_id = await get_primary_person_id()
    son_id = await seed_person("son", "Son")
    await grant_person(primary_id, user_id, access="manage")
    await grant_person(son_id, user_id, access="manage")
    await seed_synced_row(primary_id)
    await seed_synced_row(son_id, garmin_activity_id="77700022")

    resp = await client.post(f"/p/{PRIMARY_SLUG}/api/activity", json=BODY, cookies=cookies)

    assert resp.status_code == 200
    assert resp.json()["garmin_sets_status"] == "synced"
    assert [p["activity_id"] for p in fake_garmin_client.pushed_exercise_sets] == [STORED_ACTIVITY_ID]
    assert fake_garmin_client.registry_calls == [(primary_id, 1)]
    son_row = await fetch_row(son_id)
    assert son_row["garmin_sets_status"] == "failed"
    assert son_row["garmin_activity_id"] == "77700022"
    assert son_row["garmin_claimed_at"] is None
