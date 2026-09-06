"""The cross-person Garmin guard and its D-015 override.

This is what protects the kid's data. The deployment holds ONE Garmin
credential, belonging to the primary person, and whatever it accepts is that
one human's data no matter which person_id the caller named. require_person
authorizes a caller FOR A TARGET PERSON; it cannot authorize them for a DATA
SOURCE, so `manage` on the son is not permission to write into the parent's
Garmin account.

D-015 makes that possible ON PURPOSE and only on purpose: the caller must ask
for it by name with `garmin_target: "credential_person"`, the activity is
titled with the target person's display name so the parent can see whose
session it was, and the override is logged at WARNING and recorded on the row.

Every test here runs in open-access mode (empty users table), so
require_person grants everything and the ONLY thing under test is the
credential-person comparison.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db
from tests.conftest import PERSON_PREFIX, seed_person

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


@pytest.fixture
async def son(initialized_db):
    """A second person, who is NOT the Garmin-credential person."""
    return await seed_person("son", "Son")


def body(**overrides) -> dict:
    payload = {
        "session_id": "cadence-2026-09-06-a3f9",
        "session_label": "Lower A",
        "start": START,
        "duration_min": 42,
        "exercises": [{"name": "Goblet Squat", "garmin_category": "SQUAT", "sets": 3, "reps": 10}],
    }
    payload.update(overrides)
    return payload


async def fetch_row(session_id: str = "cadence-2026-09-06-a3f9"):
    db = await get_db()
    try:
        return await (
            await db.execute("SELECT * FROM strength_sessions WHERE session_id = ?", (session_id,))
        ).fetchone()
    finally:
        await db.close()


async def test_cross_person_push_returns_409(client, son, fake_garmin_client):
    """409, not 404: the caller demonstrably holds `manage` on this person,
    so naming the reason leaks nothing. And NOT a silent downgrade to
    store-only -- an explicit push_to_garmin: true that quietly does nothing
    is worse than an error."""
    resp = await client.post("/p/son/api/activity", json=body(push_to_garmin=True))
    assert resp.status_code == 409
    assert "Garmin" in resp.json()["detail"]
    assert "garmin_target" in resp.json()["detail"]
    assert len(fake_garmin_client.created_activities) == 0
    # Rejected whole: nothing stored, so no row is left stuck at 'pending'
    # 409ing on every retry, and no row's status can become 'synced'.
    assert await fetch_row() is None


async def test_cross_person_push_with_override_succeeds(client, son, fake_garmin_client):
    resp = await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
    )
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_target"] == "credential_person"
    assert len(fake_garmin_client.created_activities) == 1

    row = await fetch_row()
    # The row keeps the TARGET person. Only the Garmin filing goes under the
    # credential owner, and garmin_target is what makes that auditable after
    # the fact rather than log-only.
    assert row["person_id"] == son
    assert row["garmin_target"] == "credential_person"


async def test_override_prefixes_activity_name_with_display_name(client, son, fake_garmin_client):
    """The exact string from D-015. display_name is read from persons for the
    TARGET person, never from the request body -- a body-supplied name could
    label the parent's Garmin activity as anyone at all."""
    await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
    )
    assert fake_garmin_client.created_activities[0]["activity_name"] == "Cadence (Son) — Lower A"


async def test_override_logs_a_warning(client, son, caplog):
    """Both person_ids must appear, in their own positions.

    A bare `f"person_id={son}" in caplog.text` is not enough: the format
    string contains "person_id=%s" TWICE, so that assertion passes even if
    the two ids were swapped -- which is exactly the confusion this log line
    exists to resolve (whose session went into whose account). Anchoring on
    the surrounding words pins each id to its own slot.
    """
    from shared.database import get_primary_person_id

    credential_person = await get_primary_person_id()
    assert credential_person != son, "the fixture must not make the son the credential person"

    with caplog.at_level("WARNING"):
        await client.post(
            "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
        )
    assert "D-015 override" in caplog.text
    assert f"for person_id={son} filed under" in caplog.text
    assert f"Garmin credential person_id={credential_person}" in caplog.text
    assert "display_name='Son'" in caplog.text


async def test_override_is_ignored_when_push_false(client, son, fake_garmin_client):
    """garmin_target is inert on its own: it is consulted ONLY when
    push_to_garmin is true AND the target differs from the credential person.
    A misconfigured client that sends it on every session must not thereby
    start filing sessions on Garmin."""
    resp = await client.post("/p/son/api/activity", json=body(garmin_target="credential_person"))
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "skipped"
    assert len(fake_garmin_client.created_activities) == 0
    row = await fetch_row()
    assert row is not None
    assert row["garmin_target"] is None


async def test_same_person_push_needs_no_override(client, fake_garmin_client):
    """The credential person pushing their own session is the ordinary path
    and must not require the override."""
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True))
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert "garmin_target" not in resp.json()
    assert fake_garmin_client.created_activities[0]["activity_name"] == "Cadence — Lower A"


async def test_no_session_label_defaults_to_strength(client, fake_garmin_client):
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True, session_label=None)
    )
    assert resp.status_code == 202
    assert fake_garmin_client.created_activities[0]["activity_name"] == "Cadence — Strength"


async def test_cross_person_store_only_is_the_normal_path(client, son, fake_garmin_client):
    """push_to_garmin defaults to false, which stores with
    garmin_status='skipped' and is the normal path for the kid profile."""
    resp = await client.post("/p/son/api/activity", json=body())
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "skipped"
    assert len(fake_garmin_client.created_activities) == 0


async def test_garmin_target_rejects_unknown_value(client, son):
    """The field is a Literal, so "yes"/"true"/a typo is a 422 rather than a
    quietly-not-an-override that 409s with a confusing message."""
    resp = await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="parent")
    )
    assert resp.status_code == 422


async def test_override_not_echoed_when_no_push_happens(client, son, fake_garmin_client, caplog):
    """A re-POST of a 'skipped' row with the override pushes nothing --
    'skipped' is not retryable. Echoing garmin_target there would tell the
    client its session had been filed under the credential owner's account
    when nothing was filed at all, and a D-015 WARNING for a push that never
    happened trains the reader to ignore the line that matters."""
    await client.post("/p/son/api/activity", json=body())
    assert (await fetch_row())["garmin_status"] == "skipped"

    with caplog.at_level("WARNING"):
        resp = await client.post(
            "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
        )

    assert resp.status_code == 200
    assert "garmin_target" not in resp.json()
    assert "D-015 override" not in caplog.text
    assert fake_garmin_client.created_activities == []


async def test_override_is_echoed_on_a_later_read_of_an_overridden_row(client, son, fake_garmin_client):
    """The converse: a row that really WAS filed under the credential owner
    keeps saying so on every later dedup response, even though that request
    pushed nothing itself."""
    await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
    )
    resp = await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, garmin_target="credential_person")
    )
    assert resp.status_code == 200
    assert resp.json()["garmin_target"] == "credential_person"
    assert len(fake_garmin_client.created_activities) == 1
