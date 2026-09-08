"""The access-control matrix for the three activity routes.

Modelled on tests/test_require_person.py and tests/test_idor_by_row_id.py, and
run against the REAL weight app rather than a synthetic one, so a route
mounted with the wrong dependency (or on a path with no {slug}) is caught here
rather than in review.

The three properties that matter, all negative:

1. No credential is 401 with `WWW-Authenticate: Bearer`, not a redirect --
   these are API paths.
2. An unknown slug and a missing grant return the SAME 404. A 403 would
   confirm the person exists, which leaks household membership.
3. A session_id is only ever readable through its own person's slug.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from tests.conftest import PRIMARY_SLUG, grant_person, seed_person, seed_user

START = "2026-09-06T08:00:00+00:00"

BODY = {
    "session_id": "cadence-2026-09-06-a3f9",
    "start": START,
    "duration_min": 42,
    "exercises": [{"name": "Bench Press", "sets": 3, "reps": 10}],
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


async def _as(username: str, role: str = "user") -> tuple[int, dict]:
    """A seeded user plus the session cookie that authenticates them.

    Seeding ANY user also leaves open-access mode: with a non-empty users
    table `_get_current_identity` stops returning the anonymous sentinel, so
    these tests exercise the real grant checks rather than the
    everything-is-open path a fresh volume runs in.
    """
    user_id = await seed_user(username, role=role)
    return user_id, {"vf_session": create_session_cookie(username, user_id, 1)}


async def seed_session(person_id: int, session_id: str) -> None:
    from datetime import datetime, timezone

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


async def test_unauthenticated_returns_401(client):
    """With a populated users table, no credential is a 401 carrying the
    bearer challenge -- not the 302-to-login a non-API path would get."""
    await seed_user("someone")
    resp = await client.post(f"/p/{PRIMARY_SLUG}/api/activity", json=BODY)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_unknown_slug_returns_404(client):
    _, cookies = await _as("alice")
    resp = await client.post("/p/nobody-here/api/activity", json=BODY, cookies=cookies)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Person not found"


async def test_no_grant_returns_404(client):
    """Identical body to the unknown-slug case above, deliberately: "no such
    person" and "not yours" must be indistinguishable."""
    _, cookies = await _as("alice")
    await seed_person("son", "Son")
    resp = await client.post("/p/son/api/activity", json=BODY, cookies=cookies)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Person not found"


async def test_view_grant_cannot_post(client):
    """POST requires `manage`. A view-only grant is a 404, never a 403 -- the
    same answer as no grant at all."""
    user_id, cookies = await _as("alice")
    person_id = await seed_person("son", "Son")
    await grant_person(person_id, user_id, access="view")
    resp = await client.post("/p/son/api/activity", json=BODY, cookies=cookies)
    assert resp.status_code == 404


async def test_manage_grant_can_post(client):
    """The positive control, so the three negatives above cannot pass merely
    because every POST 404s."""
    user_id, cookies = await _as("alice")
    person_id = await seed_person("son", "Son")
    await grant_person(person_id, user_id, access="manage")
    resp = await client.post("/p/son/api/activity", json=BODY, cookies=cookies)
    assert resp.status_code == 202


async def test_view_grant_can_read(client):
    user_id, cookies = await _as("alice")
    person_id = await seed_person("son", "Son")
    await grant_person(person_id, user_id, access="view")
    await seed_session(person_id, "sess-son")
    resp = await client.get("/p/son/api/activity/sess-son", cookies=cookies)
    assert resp.status_code == 200


async def test_cross_person_read_isolated(client):
    """Person A's session_id fetched under person B's slug is a 404, even
    though the caller legitimately holds a grant on B. The dependency
    authorizes the caller for B; the person_id in the WHERE clause is what
    stops A's row coming back through B's slug."""
    user_id, cookies = await _as("alice")
    primary_id = await get_primary_person_id()
    son_id = await seed_person("son", "Son")
    await grant_person(primary_id, user_id, access="manage")
    await grant_person(son_id, user_id, access="manage")
    await seed_session(primary_id, "sess-primary")

    mine = await client.get(f"/p/{PRIMARY_SLUG}/api/activity/sess-primary", cookies=cookies)
    assert mine.status_code == 200

    theirs = await client.get("/p/son/api/activity/sess-primary", cookies=cookies)
    assert theirs.status_code == 404


async def test_cross_person_list_isolated(client):
    """The same property on the list route: it must not leak rows across
    persons just because the caller can reach both."""
    user_id, cookies = await _as("alice")
    primary_id = await get_primary_person_id()
    son_id = await seed_person("son", "Son")
    await grant_person(primary_id, user_id, access="manage")
    await grant_person(son_id, user_id, access="manage")
    await seed_session(primary_id, "sess-primary")

    resp = await client.get("/p/son/api/strength-sessions", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "sessions": []}
