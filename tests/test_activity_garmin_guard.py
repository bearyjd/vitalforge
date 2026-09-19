"""Strength-session Garmin routing is always scoped to the path person.

Phase 3 gives every person an independent Garmin link. There is no longer a
credential-owner override: a request either uses the addressed person's link,
or stays in VitalForge with a bounded ``link_required`` outcome.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from shared import garmin_registry
from shared.database import get_db
from tests.conftest import PERSON_PREFIX, seed_person

START = "2026-09-06T08:00:00+00:00"

pytestmark = pytest.mark.usefixtures("no_real_garmin_client")


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def son(initialized_db):
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


async def test_unlinked_push_is_stored_with_bounded_link_required(
    client, son, fake_garmin_client, monkeypatch
):
    """An unlinked person's explicit push never reaches another person's Garmin."""
    requested_person_ids: list[int] = []

    async def unlinked(person_id, operation, *, max_wait_seconds=0.0):
        requested_person_ids.append(person_id)
        raise garmin_registry.GarminNotLinked(person_id)

    monkeypatch.setattr(garmin_registry, "call", unlinked)

    resp = await client.post("/p/son/api/activity", json=body(push_to_garmin=True))

    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "failed"
    assert resp.json()["garmin_error"] == "link_required"
    assert requested_person_ids == [son]
    assert fake_garmin_client.created_activities == []

    row = await fetch_row()
    assert row["person_id"] == son
    assert row["garmin_status"] == "failed"
    assert row["garmin_error"] == "link_required"


async def test_linked_person_pushes_only_to_their_own_registry_client(client, son, fake_garmin_client):
    resp = await client.post("/p/son/api/activity", json=body(push_to_garmin=True))

    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert fake_garmin_client.registry_calls == [(son, 1)]
    assert fake_garmin_client.created_activities[0]["activity_name"] == "Cadence — Lower A [6-a3f9]"

    row = await fetch_row()
    assert row["person_id"] == son
    assert all(not key.endswith("_target") for key in row.keys())
    assert all(not key.endswith("_target") for key in resp.json())


async def test_terminalized_legacy_global_row_never_calls_the_new_person_link(
    client, son, fake_garmin_client
):
    """The migration's bounded marker blocks both retry and reconciliation."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
            "duration_seconds, exercises_json, garmin_status, garmin_error, created_at, updated_at) "
            "VALUES (?, ?, 'Lower A', ?, 2520, '[]', 'unknown', 'legacy_target_retired', ?, ?)",
            (son, "cadence-legacy-global", START, START, START),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.post(
        "/p/son/api/activity", json=body(session_id="cadence-legacy-global", push_to_garmin=True)
    )

    assert resp.status_code == 200
    assert resp.json()["garmin_status"] == "unknown"
    assert resp.json()["garmin_error"] == "legacy_target_retired"
    assert fake_garmin_client.registry_calls == []
    assert fake_garmin_client.created_activities == []


async def test_unknown_provider_target_field_is_rejected(client, son, fake_garmin_client):
    resp = await client.post(
        "/p/son/api/activity", json=body(push_to_garmin=True, legacy_provider_target="another_person")
    )

    assert resp.status_code == 422
    assert fake_garmin_client.created_activities == []
    assert await fetch_row() is None


async def test_store_only_is_available_without_a_garmin_link(client, son, fake_garmin_client):
    resp = await client.post("/p/son/api/activity", json=body())

    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "skipped"
    assert fake_garmin_client.created_activities == []


async def test_no_session_label_defaults_to_strength(client, fake_garmin_client):
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity", json=body(push_to_garmin=True, session_label=None)
    )

    assert resp.status_code == 202
    assert fake_garmin_client.created_activities[0]["activity_name"] == "Cadence — Strength [6-a3f9]"
