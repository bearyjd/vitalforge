"""Adversarial probes against POST /p/{slug}/api/activity.

Everything here is a property some *other* component will lean on and be
wrong about if it does not hold:

- PRP-06's Cadence-side retry queue leans on the exact shape of the re-push
  gate, including which stored statuses are NOT retryable.
- Cadence authenticates with an API TOKEN, not a browser session cookie, so
  the isolation guarantees have to hold on that path specifically.
- The request model is duplicated verbatim in PRP-06 §4, and `extra="forbid"`
  means any drift between the two surfaces only as an end-to-end 422. The
  boundary values are pinned here so the drift surfaces in CI instead.

Companion to test_activity_api.py, which covers the happy matrix; this file
is deliberately all edges.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, PRIMARY_SLUG, grant_person, seed_person, seed_token, seed_user

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
            {"name": "Bench Press", "garmin_category": "BENCH_PRESS", "sets": 3, "reps": 10},
        ],
    }
    payload.update(overrides)
    return payload


def exercise(**overrides) -> dict:
    base = {"name": "Bench Press", "garmin_category": "BENCH_PRESS", "sets": 3, "reps": 10}
    base.update(overrides)
    return base


async def fetch_row(session_id: str = "cadence-2026-09-06-a3f9"):
    db = await get_db()
    try:
        return await (
            await db.execute("SELECT * FROM strength_sessions WHERE session_id = ?", (session_id,))
        ).fetchone()
    finally:
        await db.close()


# --- the re-push gate, which PRP-06 depends on --------------------------------


async def test_skipped_is_never_pushed_on_retry_even_when_asked(client, fake_garmin_client):
    """A session stored with push_to_garmin false NEVER becomes pushable.

    DOCUMENTED FOR PRP-06. The retryable set is ('pending', 'failed');
    'skipped' is deliberately absent. So a client that stores a session
    store-only and later decides it wants it on Garmin cannot get it there by
    re-POSTing the same session_id with push_to_garmin: true -- the repeat is
    an ordinary idempotent 200 and no Garmin call happens.

    This matters to Cadence in two directions. It is a SAFETY property for
    the youth profile: a later POST, from a stale queue entry or a
    misconfigured flag, cannot retroactively file a kid's session on the
    parent's Garmin account. And it is a CONSTRAINT on Cadence's retry queue:
    push intent must be decided on the FIRST post of a session_id, so a queued
    entry must carry the push flag it was created with rather than
    recomputing it from current settings at send time. If PRP-06 needs
    after-the-fact pushing, that is a new endpoint or an explicit status
    transition, not a re-POST -- and this test will fail loudly if the gate is
    widened to include 'skipped' without that being a deliberate decision.
    """
    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=False))
    assert first.status_code == 202
    assert first.json()["garmin_status"] == "skipped"

    second = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["garmin_status"] == "skipped"

    assert fake_garmin_client.created_activities == []
    row = await fetch_row()
    assert row["garmin_status"] == "skipped"
    assert row["garmin_activity_id"] is None


async def test_skipped_retry_with_override_does_not_stamp_garmin_target(client, son, fake_garmin_client):
    """The cross-person case of the rule above, on the row's audit column.

    A first store-only POST leaves garmin_target NULL. A second POST asking
    for both the push and the D-015 override must not push -- and must not
    back-date the override onto a row that was never filed under anyone.
    garmin_target exists to record what Garmin actually received; a value
    there for a session Garmin never saw would make the audit trail lie.
    """
    first = await client.post("/p/son/api/activity", json=body())
    assert first.status_code == 202
    assert (await fetch_row())["garmin_target"] is None

    second = await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
    )
    assert second.status_code == 200
    assert second.json()["garmin_status"] == "skipped"
    assert fake_garmin_client.created_activities == []

    row = await fetch_row()
    assert row["garmin_status"] == "skipped"
    assert row["garmin_target"] is None, "the override must not be stamped onto a session never pushed"


async def test_synced_is_never_repushed_however_many_times_it_is_posted(client, fake_garmin_client):
    """Once synced, always exactly one Garmin activity.

    Two POSTs is the minimum case and is covered elsewhere. Five is the shape
    a retry loop that has lost track of its own successes produces, and a
    duplicate activity on Garmin cannot be deleted by this service -- it is a
    permanent artefact of a transient client bug.
    """
    for _ in range(5):
        await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))

    assert len(fake_garmin_client.created_activities) == 1
    row = await fetch_row()
    assert row["garmin_status"] == "synced"


async def test_failed_pushes_again_then_stops(client, weight_app_module, fake_garmin_client, monkeypatch, activity_garmin_module):
    """The full retry arc in one test: fail, retry, succeed, then STOP.

    Split across two tests elsewhere. Together they are one property -- a
    retry gate that re-pushed on 'failed' but never closed on 'synced' would
    pass both halves separately while creating a new Garmin activity on every
    replay forever.
    """
    calls = {"n": 0}
    real = activity_garmin_module.push_activity

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("garmin is down")
        return real(**kwargs)

    monkeypatch.setattr(activity_garmin_module, "push_activity", flaky)

    for _ in range(4):
        await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))

    # One failed attempt, one successful one, then the gate closes.
    assert calls["n"] == 2
    assert len(fake_garmin_client.created_activities) == 1
    row = await fetch_row()
    assert row["garmin_status"] == "synced"
    assert row["garmin_error"] is None


async def test_pending_row_with_live_claim_is_not_pushed_again(
    client, weight_app_module, fake_garmin_client, monkeypatch
, activity_garmin_module, activity_routes_module):
    """A row stranded at 'pending' is NOT re-pushed while its claim is live.

    This test was written as a strict xfail demonstrating a real defect, and
    the fix it asked for landed -- by a different route than its reason
    predicted, so the marker is gone and the assertion stands as the
    regression guard.

    The defect was real: 'pending' is written at INSERT, before the push is
    attempted, and overwritten only by the post-commit UPDATE, so it carries
    two meanings the retry gate could not tell apart.

      1. the push has not been attempted yet, and
      2. the push WAS attempted -- possibly successfully -- and recording its
         outcome did not happen.

    Narrowing _RETRYABLE_GARMIN_STATUSES (what the marker proposed) would have
    fixed case 2 by giving up on case 1: a session whose push genuinely never
    ran could then never be retried, which is the entire client-driven retry
    story. `garmin_claimed_at` separates the two instead. It is set inside the
    same BEGIN IMMEDIATE that writes 'pending', so a live claim means "a push
    is in flight or died mid-flight" and a NULL claim means "not attempted" --
    the distinction 'pending' alone could not carry.

    That also closes the concurrent variant this docstring originally noted as
    timing-dependent; see
    test_activity_concurrency.py's slow-push regression test.

    A claim older than _GARMIN_CLAIM_TIMEOUT_SECONDS is stale and re-claimable,
    so a crash mid-push cannot strand a session unpushable forever --
    test_stale_claim_is_reclaimed covers that side.
    """
    real_record = activity_routes_module._record_activity_garmin_outcome

    async def failing_record(db, row_id, outcome):
        raise RuntimeError("disk I/O error")

    # POST 1: the Garmin push SUCCEEDS; only recording its outcome fails, so
    # the row is left at 'pending' over an activity Garmin already has.
    monkeypatch.setattr(activity_routes_module, "_record_activity_garmin_outcome", failing_record)
    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert first.status_code == 202
    assert len(fake_garmin_client.created_activities) == 1
    # 'unknown' now, not 'pending': the push succeeded and only recording it
    # failed, so the row is deliberately taken OUT of the retry set rather
    # than left looking like a push that never ran.
    assert (await fetch_row())["garmin_status"] == "unknown"

    # POST 2 is an ordinary client retry of the same session_id -- exactly
    # what PRP §5.2 tells Cadence to do, and what D-016 bounds.
    monkeypatch.setattr(activity_routes_module, "_record_activity_garmin_outcome", real_record)
    second = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert second.status_code == 200

    assert len(fake_garmin_client.created_activities) == 1, (
        "one session must never become two Garmin activities; the retry gate re-pushed "
        "a row whose 'pending' status hid an already-successful push"
    )


# --- IDOR on the token path ---------------------------------------------------


@pytest.fixture
async def son(initialized_db):
    return await seed_person("son", "Son")


async def _token_headers(username: str) -> tuple[int, dict]:
    """A seeded user plus an API-token Authorization header for them.

    Cadence authenticates with a token, not a browser cookie, so the
    isolation properties have to be proven on THIS path. Seeding any user
    also leaves open-access mode, so the grant checks are live.
    """
    user_id = await seed_user(username)
    _, raw = await seed_token(user_id, label=f"{username}-token")
    return user_id, {"Authorization": f"Bearer {raw}"}


async def seed_session(person_id: int, session_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
            "exercises_json, created_at, updated_at) VALUES (?, ?, ?, 2520, '[]', ?, ?)",
            (person_id, session_id, START, now, now),
        )
        await db.commit()
    finally:
        await db.close()


async def test_token_cannot_read_another_persons_session_by_id(client, son):
    """Bob holds a token and a grant on the son. Alice's session_id, fetched
    through the son's slug, is a 404 -- never a 403, and never the row.

    404 rather than 403 is the whole point: a 403 would confirm that the id
    names a real session belonging to someone, which is enough to enumerate
    another household member's training log by guessing ids.
    """
    _, alice_headers = await _token_headers("alice")
    bob_id, bob_headers = await _token_headers("bob")
    primary_id = await get_primary_person_id()
    await grant_person(primary_id, bob_id, access="manage")
    await grant_person(son, bob_id, access="manage")
    await seed_session(primary_id, "sess-primary")

    # Positive control: through its OWN slug the row is readable.
    mine = await client.get(f"/p/{PRIMARY_SLUG}/api/activity/sess-primary", headers=bob_headers)
    assert mine.status_code == 200

    theirs = await client.get("/p/son/api/activity/sess-primary", headers=bob_headers)
    assert theirs.status_code == 404
    assert "sess-primary" not in theirs.text or theirs.json().get("detail")

    # Alice has no grant on anyone: same 404, no distinction.
    alices = await client.get(f"/p/{PRIMARY_SLUG}/api/activity/sess-primary", headers=alice_headers)
    assert alices.status_code == 404


async def test_token_list_never_leaks_another_persons_sessions(client, son):
    """The list route scoped by token identity, not just by dependency."""
    bob_id, bob_headers = await _token_headers("bob")
    primary_id = await get_primary_person_id()
    await grant_person(primary_id, bob_id, access="manage")
    await grant_person(son, bob_id, access="manage")
    await seed_session(primary_id, "sess-primary")
    await seed_session(son, "sess-son")

    theirs = await client.get("/p/son/api/strength-sessions", headers=bob_headers)
    assert theirs.status_code == 200
    ids = [s["session_id"] for s in theirs.json()["sessions"]]
    assert ids == ["sess-son"]
    assert "sess-primary" not in theirs.text


async def test_ungranted_token_gets_404_not_403(client, son):
    """A valid token with no grant on the target is indistinguishable from an
    unknown slug. Anything else leaks household membership."""
    _, alice_headers = await _token_headers("alice")
    await seed_session(son, "sess-son")

    known = await client.get("/p/son/api/activity/sess-son", headers=alice_headers)
    unknown = await client.get("/p/nobody-here/api/activity/sess-son", headers=alice_headers)
    assert known.status_code == 404
    assert unknown.status_code == 404
    assert known.json() == unknown.json()


async def test_response_never_echoes_a_credential(client):
    """No route may put the caller's token, or any part of it, in its body.

    Cheap to assert and cheap to break: a future debug field, or an
    `extra="allow"` slip that echoes the request back, would carry the bearer
    token into logs, into Cadence's own storage, and into any error report
    that captures a response body.

    NOTE the deliberate limit of this test: it proves no route ECHOES the
    caller's credential. It does NOT prove the responses are credential-free
    in general -- `garmin_error` carries a caught exception's string verbatim
    by design (PRP §4.1), so anything garminconnect puts in an exception
    message reaches both this body and the stored row.
    """
    user_id = await seed_user("alice")
    _, raw = await seed_token(user_id, label="alice-token")
    headers = {"Authorization": f"Bearer {raw}"}
    primary_id = await get_primary_person_id()
    await grant_person(primary_id, user_id, access="manage")

    created = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(), headers=headers)
    fetched = await client.get(
        f"{PERSON_PREFIX}/api/activity/cadence-2026-09-06-a3f9", headers=headers
    )
    listed = await client.get(f"{PERSON_PREFIX}/api/strength-sessions", headers=headers)

    assert created.status_code == 202
    assert fetched.status_code == 200
    assert listed.status_code == 200
    for resp in (created, fetched, listed):
        assert raw not in resp.text
        assert "Bearer" not in resp.text
        assert "token" not in resp.text.lower()


# --- boundary values the PRP-06 model must match ------------------------------


@pytest.mark.parametrize("duration_min", [0, -1, 601])
async def test_duration_min_out_of_range_rejected_422(client, duration_min):
    """ge=1, le=600. Zero is the one that matters: a zero-minute activity is
    accepted by Garmin and shows up as a real strength session of no length,
    which is worse than a rejection because it looks like data."""
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(duration_min=duration_min))
    assert resp.status_code == 422


async def test_duration_min_boundaries_accepted(client):
    """The positive control for the range above, so it cannot pass by
    rejecting everything."""
    for i, duration_min in enumerate((1, 600)):
        resp = await client.post(
            f"{PERSON_PREFIX}/api/activity",
            json=body(duration_min=duration_min, session_id=f"boundary-{i}"),
        )
        assert resp.status_code == 202, resp.text


async def test_start_five_minutes_in_the_future_rejected_422(client):
    """The tolerance is 60 seconds, for clock skew -- not for a client that
    schedules sessions ahead. Five minutes is comfortably past it, and unlike
    the +120s case it is far enough out that a slow CI run cannot make the
    result depend on timing."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(start=future))
    assert resp.status_code == 422


@pytest.mark.parametrize("category", ["unknown", "UNKNOWN", "Bench_Press", "bench_press", ""])
async def test_unknown_garmin_category_rejected_422(client, fake_garmin_client, category):
    """Validated at the model layer, so the rejection lands BEFORE anything
    is stored or pushed -- not as a Garmin 400 halfway through a push that
    has already created the activity.

    "UNKNOWN" is called out because D-018 discusses it as a Cadence-side
    placeholder; it is genuinely not a garminconnect category, so it is a 422
    here rather than a silently-unmapped exercise. The case variants pin that
    the membership test is exact: CATEGORIES holds "BENCH_PRESS", and a
    lowercase spelling is a different string, not a near-miss to be repaired.
    """
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity", json=body(exercises=[exercise(garmin_category=category)])
    )
    assert resp.status_code == 422
    assert fake_garmin_client.created_activities == []
    assert await fetch_row() is None


async def test_time_measured_exercise_needs_reps_one_not_reps_absent(client):
    """`seconds` is accepted, but `reps` stays REQUIRED.

    PINNED DELIBERATELY, against three agreeing sources: PRP-05 §4.1's
    ActivityExerciseIn table, docs/vitalforge-contract.md §4.3, and the
    implementation. All three make `reps` required ge=1, and PRP §4.1 states
    the designed shape for time-measured work (planks, dead hangs, carries,
    the mobility prelude) is `reps=1` PLUS the hold in `seconds` -- not
    `seconds` alone.

    If `reps` should become optional, PRP §4.1 requires this model to stay
    identical to PRP-06 §4, so BOTH models and this test change together.
    Loosening it on one side only would produce a mismatch that
    extra="forbid" hides until an end-to-end run.
    """
    accepted = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(exercises=[exercise(name="Plank", garmin_category=None, sets=3, reps=1, seconds=45)]),
    )
    assert accepted.status_code == 202, accepted.text

    rejected = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json=body(
            session_id="no-reps",
            exercises=[{"name": "Plank", "garmin_category": None, "sets": 3, "seconds": 45}],
        ),
    )
    assert rejected.status_code == 422
    assert "reps" in rejected.text
