"""POST /p/{slug}/api/activity -- request model and session_id idempotency.

Modelled on tests/test_client_id_idempotency.py: same harness (no Docker, no
network; `weight_app_module` fakes Garmin and points the DB at a tmp_path
SQLite file), same shape of matrix.

The property under test throughout is that `session_id` is an IDENTITY, not a
timestamp window: a repeat POST of the same id never inserts a second row and
never creates a second Garmin activity, however far apart the two requests
are, and the stored payload is never rewritten by the second one.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, seed_person

# Fixed and safely in the past, so the +60s future tolerance never turns a
# slow test run into a spurious 422.
START = "2026-09-06T08:00:00+00:00"


# Every test in this module must be unable to reach real Garmin: app.py binds
# the push helpers into its own namespace, so patching shared.garmin_client
# alone would leave the routes calling the live client and the tests passing.
pytestmark = pytest.mark.usefixtures("no_real_garmin_client")


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def body(**overrides) -> dict:
    payload = {
        "session_id": "cadence-2026-09-06-a3f9",
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
                "rest_s": 90,
            }
        ],
        "notes": "felt strong",
        "source": "cadence",
    }
    payload.update(overrides)
    return payload


async def row_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM strength_sessions")).fetchone())[0]
    finally:
        await db.close()


async def fetch_row(session_id: str = "cadence-2026-09-06-a3f9"):
    db = await get_db()
    try:
        return await (
            await db.execute("SELECT * FROM strength_sessions WHERE session_id = ?", (session_id,))
        ).fetchone()
    finally:
        await db.close()


# --- 1-4: fresh insert, then idempotent repeat --------------------------------


async def test_post_activity_fresh_returns_202(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["success"] is True
    assert payload["id"] > 0
    assert payload["session_id"] == "cadence-2026-09-06-a3f9"
    assert payload["person_id"] == await get_primary_person_id()
    # push_to_garmin defaults to False, so the safe default is store-only.
    assert payload["garmin_status"] == "skipped"
    assert payload["garmin_sets_status"] == "not_attempted"


async def test_repeat_post_returns_200_deduplicated(client):
    """Guards the status_code=202 decorator trap: the decorator's status
    applies to EVERY return, so without an explicit override the dedup branch
    also answers 202 and the client cannot tell "created" from "already had
    it" by status code alone."""
    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    second = await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["id"] == first.json()["id"]


async def test_repeat_post_does_not_insert_second_row(client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    assert await row_count() == 1


async def test_repeat_post_does_not_create_second_garmin_activity(client, fake_garmin_client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert len(fake_garmin_client.created_activities) == 1


# --- 5-6: the retry gate ------------------------------------------------------


async def test_repeat_post_retries_a_previously_failed_push(client, weight_app_module, monkeypatch, fake_garmin_client):
    """A session whose Garmin push failed must be re-pushable by re-POSTing
    the same session_id -- that is the ONLY retry mechanism this codebase has
    (there is no background worker, for anything). Without it a transient
    Garmin outage would strand the session as 'failed' forever."""
    calls = {"n": 0}
    real = weight_app_module.push_activity

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("garmin exploded")
        return real(**kwargs)

    monkeypatch.setattr(weight_app_module, "push_activity", flaky)

    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert first.status_code == 202
    assert first.json()["garmin_status"] == "failed"
    assert (await fetch_row())["garmin_status"] == "failed"

    second = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert second.status_code == 200
    assert second.json()["garmin_status"] == "synced"
    row = await fetch_row()
    assert row["garmin_status"] == "synced"
    assert row["garmin_error"] is None
    assert await row_count() == 1
    assert len(fake_garmin_client.created_activities) == 1


async def test_repeat_post_does_not_repush_when_already_synced(client, fake_garmin_client):
    """The companion property to the retry above: the gate is
    `garmin_status in ('pending','failed')`, not "any repeat re-pushes"."""
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert len(fake_garmin_client.created_activities) == 1
    assert (await fetch_row())["garmin_status"] == "synced"


# --- 7: first-write-wins ------------------------------------------------------


async def test_first_write_wins_on_differing_body(client, caplog):
    """A repeat POST that disagrees is reported, never applied -- the same
    convention ENRICHABLE_FIELDS follows for weight. And NOT a 409: a client
    replaying a session it edited locally should still get its idempotent
    200, with the disagreement made visible."""
    await client.post(f"{PERSON_PREFIX}/api/activity", json=body())
    with caplog.at_level("WARNING"):
        second = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(duration_min=55))

    assert second.status_code == 200
    assert second.json()["conflict"] is True
    assert "duration_min" in second.json()["conflict_fields"]
    assert "duration_min" in caplog.text
    # The stored payload is untouched: 42 minutes, not 55.
    assert (await fetch_row())["duration_seconds"] == 42 * 60


# --- 9-10: the DB-level backstop ---------------------------------------------


async def test_unique_constraint_rejects_duplicate_session_id_for_same_person(initialized_db):
    """Independent of the request path's BEGIN IMMEDIATE serialization: a
    direct second INSERT must fail, so a future write path that bypasses the
    transaction cannot quietly double-store a session."""
    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
            "exercises_json, created_at, updated_at) VALUES (?, 'dup', ?, 60, '[]', ?, ?)",
            (person_id, now, now, now),
        )
        await db.commit()
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            await db.execute(
                "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
                "exercises_json, created_at, updated_at) VALUES (?, 'dup', ?, 120, '[]', ?, ?)",
                (person_id, now, now, now),
            )
    finally:
        await db.close()


async def test_same_session_id_allowed_for_different_persons(initialized_db):
    """The constraint is (person_id, session_id), not session_id alone: two
    people may legitimately generate the same client-side id, and a global
    UNIQUE would reject the second one with an IntegrityError."""
    primary = await get_primary_person_id()
    other = await seed_person("son", "Son")
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        for person_id in (primary, other):
            await db.execute(
                "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
                "exercises_json, created_at, updated_at) VALUES (?, 'shared-id', ?, 60, '[]', ?, ?)",
                (person_id, now, now, now),
            )
        await db.commit()
    finally:
        await db.close()
    assert await row_count() == 2


# --- 11-13: `start` validation ------------------------------------------------


async def test_naive_start_rejected_422(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(start="2026-09-06T08:00:00"))
    assert resp.status_code == 422


async def test_future_start_beyond_tolerance_rejected_422(client):
    future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(start=future))
    assert resp.status_code == 422


async def test_start_within_60s_future_accepted(client):
    """Boundary: ordinary clock skew is tolerated, a real future capture is not."""
    near = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(start=near))
    assert resp.status_code == 202


# --- 14-17: the rest of the model --------------------------------------------


async def test_unknown_garmin_category_rejected_422(client, fake_garmin_client):
    """Caught locally at the model layer, not discovered as a Garmin 400
    halfway through a push that has already created the activity."""
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(
            push_to_garmin=True,
            exercises=[{"name": "Burpee", "garmin_category": "BURPEE_OF_DOOM", "sets": 3, "reps": 10}],
        ),
    )
    assert resp.status_code == 422
    assert len(fake_garmin_client.created_activities) == 0


@pytest.mark.parametrize("field", ["duration_min", "sets", "reps", "seconds", "weight_kg", "rest_s"])
async def test_bool_rejected_for_numeric_fields(client, field):
    """bool subclasses int, so Pydantic's lax mode silently coerces JSON true
    to 1 -- which every bound here (sets ge=1, reps ge=1, seconds ge=1)
    happily accepts. `true` would land as a real one-rep set. This bit
    VitalForge before, on bone_mass_kg."""
    if field == "duration_min":
        payload = body(duration_min=True)
    else:
        exercise = {"name": "Bench Press", "sets": 3, "reps": 10}
        exercise[field] = True
        payload = body(exercises=[exercise])
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=payload)
    assert resp.status_code == 422


async def test_extra_field_rejected_422(client):
    """The path carries the person. A `profile` key in the body must be a
    422, not a silently ignored field -- body-based person addressing is
    exactly what require_person raises RuntimeError to prevent."""
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(profile="me"))
    assert resp.status_code == 422


async def test_empty_exercises_rejected_422(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(exercises=[]))
    assert resp.status_code == 422


async def test_51_exercises_rejected_422(client):
    exercises = [{"name": f"Move {i}", "sets": 1, "reps": 1} for i in range(51)]
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(exercises=exercises))
    assert resp.status_code == 422


async def test_50_exercises_accepted(client):
    """The other side of the bound, so a future off-by-one in `max_length`
    cannot pass by rejecting everything."""
    exercises = [{"name": f"Move {i}", "sets": 1, "reps": 1} for i in range(50)]
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(exercises=exercises))
    assert resp.status_code == 202


async def test_time_measured_exercise_accepted(client):
    """Planks, dead hangs and carries have no meaningful rep count but `reps`
    is required ge=1, so they arrive as reps=1 plus the hold in `seconds`."""
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(exercises=[{"name": "Plank", "garmin_category": "PLANK", "sets": 3, "reps": 1, "seconds": 45}]),
    )
    assert resp.status_code == 202


async def test_garmin_exercise_over_100_chars_rejected_422(client):
    """Bounded like every other free-text field: it is an opaque Garmin
    sub-category name, not user prose, and it is stored in the exercises blob
    on every row."""
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(exercises=[{
            "name": "Bench Press", "garmin_category": "BENCH_PRESS",
            "garmin_exercise": "X" * 101, "sets": 3, "reps": 10,
        }]),
    )
    assert resp.status_code == 422


async def test_garmin_exercise_at_100_chars_accepted(client):
    """The boundary, so a future off-by-one cannot pass by rejecting all."""
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(exercises=[{
            "name": "Bench Press", "garmin_category": "BENCH_PRESS",
            "garmin_exercise": "X" * 100, "sets": 3, "reps": 10,
        }]),
    )
    assert resp.status_code == 202
