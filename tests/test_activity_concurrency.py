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
