"""Routes that take a row id as well as a slug need BOTH checks.

`require_person` authorizes the SLUG. It says nothing about a row id in the
same path. `DELETE /p/{slug}/api/weight/{weight_id}` and
`GET /p/{slug}/api/activities/{activity_id}` each carry a second identifier
that the dependency never sees, so the query itself must also constrain on
person_id -- otherwise a caller with a legitimate grant on their OWN person
can name any other person's row and have it served or deleted.

This file exists because mutation testing found the gap. With the whole suite
green at 556 passing, deleting the `AND person_id = ?` predicate from the
weight DELETE changed nothing: no test ever pointed one person's credentials
at another person's row id. The suite was green and the guard was decorative.

That is the exact shape of this phase's failure mode -- correct-looking with
one person, silently wrong with two -- so the tests are written the only way
that catches it: two persons, and the assertion is on the OTHER person's data.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from shared.auth import create_session_cookie
from tests.conftest import PERSON_PREFIX, grant_person, primary_person_id, seed_person, seed_user


@pytest.fixture
async def weight_client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def dashboard_client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _authorized_as(username: str, person_id: int, access: str = "manage"):
    user_id = await seed_user(username)
    await grant_person(person_id, user_id, access=access)
    return {"vf_session": create_session_cookie(username, user_id, 1)}


async def _insert_weight(person_id: int, grams: int, timestamp: str) -> int:
    from shared.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, grams / 453.592, grams / 1000, grams, timestamp),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def _weight_row_exists(row_id: int) -> bool:
    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
        return row is not None
    finally:
        await db.close()


@pytest.fixture
async def two_persons(initialized_db):
    """The primary person plus a second one. One person cannot express this
    bug at all, which is why it went unnoticed."""
    mine = await primary_person_id()
    theirs = await seed_person("bryn")
    return mine, theirs


async def test_delete_cannot_reach_another_persons_weight_row(weight_client, two_persons):
    """The mutation that survived. A caller with `manage` on their own person
    names someone else's weight row id under their OWN slug -- the dependency
    authorizes them for that slug and never sees the row id, so only the
    query's person_id predicate stops the delete."""
    mine, theirs = two_persons
    cookies = await _authorized_as("mallory", mine, access="manage")

    victim_row = await _insert_weight(theirs, 81650, "2026-03-01T12:00:00+00:00")

    resp = await weight_client.delete(f"{PERSON_PREFIX}/api/weight/{victim_row}", cookies=cookies)

    assert resp.status_code == 404, (
        f"deleting another person's weight row returned {resp.status_code}; the row id is not "
        "covered by require_person, so the query must constrain on person_id too"
    )
    assert await _weight_row_exists(victim_row), (
        "another person's weight row was DELETED via a slug the caller legitimately holds"
    )


async def test_delete_still_works_on_your_own_row(weight_client, two_persons):
    """The negative test above is worthless without this: a route that 404s
    everything would pass it while being completely broken."""
    mine, _ = two_persons
    cookies = await _authorized_as("owner", mine, access="manage")

    my_row = await _insert_weight(mine, 79000, "2026-03-02T12:00:00+00:00")

    resp = await weight_client.delete(f"{PERSON_PREFIX}/api/weight/{my_row}", cookies=cookies)
    assert resp.status_code == 200, f"could not delete my own row: {resp.status_code}"
    assert not await _weight_row_exists(my_row)


async def test_activity_detail_cannot_reach_another_persons_activity(dashboard_client, two_persons):
    """Same shape on the dashboard: /activities/{id} takes a row id the
    dependency never inspects."""
    mine, theirs = two_persons
    cookies = await _authorized_as("mallory2", mine, access="view")

    from shared.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO activities (person_id, start_time_utc, sport, source_format, "
            "file_sha256, imported_at) VALUES (?, '2026-03-01T00:00:00Z', 'running', 'fit', "
            "'their-hash', '2026-03-01T00:00:00Z')",
            (theirs,),
        )
        await db.commit()
        victim_activity = cursor.lastrowid
    finally:
        await db.close()

    resp = await dashboard_client.get(
        f"{PERSON_PREFIX}/api/activities/{victim_activity}", cookies=cookies
    )
    assert resp.status_code == 404, (
        f"another person's activity was served with {resp.status_code}; the activity id is "
        "not covered by require_person"
    )
    assert "their-hash" not in resp.text
