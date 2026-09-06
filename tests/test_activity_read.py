"""The two read routes: status polling and the session list.

`GET /p/{slug}/api/activity/{session_id}` is what a client polls after a
202 to learn whether its session reached Garmin. `GET
/p/{slug}/api/strength-sessions` is the list, named that way because
/api/activities is already the dashboard's FIT-import route over a different
table, and because `?person=` is exactly the query-string person addressing
require_person raises RuntimeError to prevent.

Cross-person isolation for both lives in tests/test_activity_auth.py.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import PERSON_PREFIX

START = "2026-09-06T08:00:00+00:00"

BODY = {
    "session_id": "cadence-2026-09-06-a3f9",
    "session_label": "Lower A",
    "start": START,
    "duration_min": 42,
    "exercises": [
        {"name": "Bench Press", "garmin_category": "BENCH_PRESS", "sets": 3, "reps": 10, "weight_kg": 40.0},
        {"name": "Plank", "garmin_category": "PLANK", "sets": 3, "reps": 1, "seconds": 45},
    ],
    "notes": "felt strong",
    "source": "cadence",
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


async def test_get_activity_by_session_id(client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "push_to_garmin": True})
    resp = await client.get(f"{PERSON_PREFIX}/api/activity/{BODY['session_id']}")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["session_id"] == BODY["session_id"]
    assert payload["session_label"] == "Lower A"
    assert payload["start_time_utc"] == START
    assert payload["duration_min"] == 42
    assert payload["notes"] == "felt strong"
    assert payload["source"] == "cadence"
    assert payload["garmin_status"] == "synced"
    assert payload["garmin_activity_id"] == "19283746501"
    assert payload["garmin_sets_status"] == "not_attempted"
    assert payload["garmin_error"] is None

    # exercises come back parsed out of the JSON blob, not as a string.
    assert isinstance(payload["exercises"], list)
    assert len(payload["exercises"]) == 2
    assert payload["exercises"][0]["name"] == "Bench Press"
    assert payload["exercises"][0]["weight_kg"] == 40.0
    assert payload["exercises"][1]["seconds"] == 45


async def test_get_unknown_session_id_404(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/activity/never-stored")
    assert resp.status_code == 404


async def test_list_strength_sessions_shape(client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json={**BODY, "session_id": "cadence-2026-09-05-b7c1", "start": "2026-09-05T08:00:00+00:00"},
    )

    resp = await client.get(f"{PERSON_PREFIX}/api/strength-sessions")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) == {"count", "sessions"}
    assert payload["count"] == 2
    # Newest first.
    assert [s["session_id"] for s in payload["sessions"]] == [
        "cadence-2026-09-06-a3f9",
        "cadence-2026-09-05-b7c1",
    ]


@pytest.mark.parametrize("limit", [0, 201, -1])
async def test_list_rejects_out_of_range_limit(client, limit):
    resp = await client.get(f"{PERSON_PREFIX}/api/strength-sessions", params={"limit": limit})
    assert resp.status_code == 422


async def test_list_limit_is_applied(client):
    for i in range(3):
        await client.post(
            f"{PERSON_PREFIX}/api/activity",
            json={**BODY, "session_id": f"sess-{i}", "start": f"2026-09-0{i + 1}T08:00:00+00:00"},
        )
    resp = await client.get(f"{PERSON_PREFIX}/api/strength-sessions", params={"limit": 2})
    assert resp.json()["count"] == 2


async def test_list_since_filters_by_start_time(client):
    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "session_id": "old", "start": "2026-08-01T08:00:00+00:00"})
    await client.post(f"{PERSON_PREFIX}/api/activity", json={**BODY, "session_id": "new", "start": START})

    resp = await client.get(f"{PERSON_PREFIX}/api/strength-sessions", params={"since": "2026-09-01"})
    assert resp.json()["count"] == 1
    assert resp.json()["sessions"][0]["session_id"] == "new"


async def test_list_empty_is_not_an_error(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/strength-sessions")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "sessions": []}
