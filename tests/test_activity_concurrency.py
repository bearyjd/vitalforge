"""Concurrent double-POST of one session_id.

The gap this closes is the one Cadence's offline replay queue makes real: a
phone that comes back online can fire the same queued session twice, close
enough together that both requests are in flight at once. Every other test in
test_activity_api.py issues one POST at a time, which is exactly what let
VitalForge's original (non-atomic) weight dedup ship broken -- both requests
saw "no duplicate" and both wrote.

`asyncio.gather` is used directly, as in tests/test_dedup_concurrency.py: both
requests here are expected to SUCCEED (one waits out the other's short
BEGIN IMMEDIATE transaction, neither errors), so the interpreter-cleanup hang
documented in test_migration.py -- specific to a *failing* aiosqlite operation
-- does not apply.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

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


async def row_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM strength_sessions")).fetchone())[0]
    finally:
        await db.close()


async def test_two_concurrent_identical_posts_insert_once_push_once(client, fake_garmin_client):
    first, second = await asyncio.gather(
        client.post(f"{PERSON_PREFIX}/api/activity", json=BODY),
        client.post(f"{PERSON_PREFIX}/api/activity", json=BODY),
    )

    assert await row_count() == 1
    assert len(fake_garmin_client.created_activities) == 1
    # One created, one deduplicated -- in whichever order they were served.
    assert sorted([first.status_code, second.status_code]) == [200, 202]
    deduped = first if first.status_code == 200 else second
    created = second if first.status_code == 200 else first
    assert deduped.json()["deduplicated"] is True
    assert deduped.json()["id"] == created.json()["id"]


async def test_five_concurrent_identical_posts_insert_once_push_once(client, fake_garmin_client):
    """Five at once, not two: a phone coming back online replays its whole
    queue, and a retry loop that fires on every reconnect can stack several
    copies of one session before any of them lands.

    Two concurrent requests can pass by luck -- the second may simply arrive
    after the first has already committed. Five make the BEGIN IMMEDIATE
    queue genuinely deep (four requests waiting on one writer), which is
    where a serialization bug stops being intermittent. It also exercises the
    30s busy_timeout in shared/database.py:121: too short a timeout would
    surface here as `database is locked` -- a 500 over a session the user
    really did complete -- rather than as a patient 200.

    Exactly one insert, exactly one Garmin activity, and exactly one 202
    among the five, however they interleave.

    The push count here caught the defect garmin_claimed_at now fixes: at
    five it duplicated in roughly 1 run in 13, where two never did. It is
    kept as a broad guard rather than the primary one -- a frequency like
    that is a coin toss, not a regression test, so the deterministic
    guarantees live in test_slow_push_is_not_duplicated_by_a_concurrent_post
    and test_live_claim_blocks_a_sequential_retry. This one is the
    end-to-end shape: five queued replays, one session, one activity.
    """
    results = await asyncio.gather(
        *[client.post(f"{PERSON_PREFIX}/api/activity", json=BODY) for _ in range(5)]
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 200, 200, 200, 202], f"expected one create and four dedups, got {statuses}"
    assert await row_count() == 1
    assert len(fake_garmin_client.created_activities) == 1

    # Every response names the SAME row. A client that reconciles by `id`
    # must not be handed two different ones for one session.
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1
    assert all(r.json()["deduplicated"] is True for r in results if r.status_code == 200)


async def test_two_concurrent_distinct_sessions_both_stored(client, fake_garmin_client):
    """The companion property: serialization must not collapse two genuinely
    different sessions into one."""
    other = {**BODY, "session_id": "cadence-2026-09-06-b7c1"}
    results = await asyncio.gather(
        client.post(f"{PERSON_PREFIX}/api/activity", json=BODY),
        client.post(f"{PERSON_PREFIX}/api/activity", json=other),
    )
    assert all(r.status_code == 202 for r in results)
    assert await row_count() == 2
    assert len(fake_garmin_client.created_activities) == 2


async def test_second_request_does_not_duplicate_a_push_already_in_flight(
    client, weight_app_module, fake_garmin_client, monkeypatch
, activity_garmin_module):
    """The reviewer's probe, and the reason garmin_claimed_at exists.

    The two gather-based tests above pass even against the broken gate,
    because the fake push returns without ever yielding -- so the first
    request records its outcome before the second one's transaction is
    granted. That is an artefact of an instant fake, not a property of the
    code. The real push is a synchronous HTTP call taking hundreds of
    milliseconds, and the window between the first request's COMMIT and its
    push returning is wide open.

    Driven from a second thread with its own event loop, the way
    test_dedup_concurrency.py drives its concurrent writer: the first
    request's push blocks, and the second request runs the REAL route
    against the same database in that window. That is also literally the
    two-worker deployment PRP risk 12 warns about.

    In that window the row reads garmin_status='pending' -- which before the
    fix the retry gate judged retryable, filing a SECOND activity for one
    session. This service has no delete path, so that duplicate is permanent
    and cleaned up by hand or not at all. garmin_claimed_at is written inside
    the same BEGIN IMMEDIATE that writes 'pending', so the second request
    sees the claim at the instant it can see the row at all.
    """
    push_started = threading.Event()
    real = activity_garmin_module.push_activity

    def slow_push(**kwargs):
        push_started.set()
        time.sleep(1.0)
        return real(**kwargs)

    monkeypatch.setattr(activity_garmin_module, "push_activity", slow_push)

    second: dict = {}

    def second_request():
        assert push_started.wait(timeout=5), "the first request never started its push"

        async def _go():
            transport = ASGITransport(app=weight_app_module.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                return await ac.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

        second["resp"] = asyncio.run(_go())

    # daemon: if the first request raises, push_started is never set and
    # a non-daemon thread would keep the interpreter alive past the test.
    thread = threading.Thread(target=second_request, daemon=True)
    thread.start()
    first = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    thread.join(timeout=10)

    assert first.status_code == 202
    assert second["resp"].status_code == 200
    assert await row_count() == 1
    assert len(fake_garmin_client.created_activities) == 1, (
        "a second request pushed a session that was already mid-push; one session "
        "became two permanent Garmin activities"
    )
    # The blocked request reports 'pending', not a stale status: a push really
    # is in flight, and this request did not make one.
    assert second["resp"].json()["deduplicated"] is True
    assert second["resp"].json()["garmin_status"] == "pending"


async def test_stale_claim_is_reclaimed(client, weight_app_module, fake_garmin_client, monkeypatch, garmin_claim_module):
    """A claim outlives the process that took it only until it ages out.

    Without an expiry, a worker killed mid-push would leave the row claimed
    forever and the session could never reach Garmin -- trading a duplicate
    for a permanent silent loss, which is worse. A claim older than
    _GARMIN_CLAIM_TIMEOUT_SECONDS is treated as evidence of a dead claimant
    and may be taken again.
    """
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=garmin_claim_module._GARMIN_CLAIM_TIMEOUT_SECONDS + 60)
    ).isoformat()

    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": False})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE strength_sessions SET garmin_status = 'pending', garmin_claimed_at = ?",
            (stale,),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "synced"
    assert len(fake_garmin_client.created_activities) == 1


async def test_live_claim_blocks_a_sequential_retry(client, fake_garmin_client):
    """The same gate, without any concurrency: a fresh claim from moments ago
    is respected by the very next POST. Pins the decision on the claim rather
    than on request timing."""
    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": False})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE strength_sessions SET garmin_status = 'failed', garmin_error = 'earlier failure', "
            "garmin_claimed_at = ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "pending"
    # The stale 'failed' error is not echoed -- a push is in flight.
    assert "garmin_error" not in resp.json()
    assert fake_garmin_client.created_activities == []


async def test_claim_is_cleared_once_the_outcome_is_recorded(client):
    """A completed push must release the claim, or the next legitimate retry
    of a failed session would be blocked for ten minutes."""
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_status, garmin_claimed_at FROM strength_sessions")
        ).fetchone()
    finally:
        await db.close()
    assert row["garmin_status"] == "synced"
    assert row["garmin_claimed_at"] is None


async def test_no_claim_is_taken_when_push_is_not_requested(client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": False})
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_claimed_at FROM strength_sessions")
        ).fetchone()
    finally:
        await db.close()
    assert row["garmin_claimed_at"] is None
