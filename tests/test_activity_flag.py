"""VITALFORGE_GARMIN_EXERCISE_SETS -- the default-off enhancement path.

The exerciseSets request payload's JSON field names are UNVERIFIED: a grep for
`repetitionCount`, `setType` and `exerciseSets` across the whole installed
garminconnect 0.3.11 tree returns only the two method definitions. It is also
unknown whether Garmin accepts exercise sets on a MANUALLY created activity at
all. Shipping that on by default would 400 every session, so it ships off, and
these tests pin both the default and the parse.

The parse specifically: `bool(os.environ.get("VITALFORGE_GARMIN_EXERCISE_SETS"))`
is True for the literal string "0", which is exactly the value .env.example
ships. That one-character mistake would silently enable the unverified path in
every deployment that dutifully set the flag to off.
"""

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


async def test_sets_not_attempted_by_default(client, fake_garmin_client, monkeypatch):
    """The shipped path is create_manual_activity ONLY, and that is a
    complete working feature on its own."""
    monkeypatch.delenv("VITALFORGE_GARMIN_EXERCISE_SETS", raising=False)
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_sets_status"] == "not_attempted"
    assert len(fake_garmin_client.created_activities) == 1
    assert fake_garmin_client.pushed_exercise_sets == []


@pytest.mark.parametrize("value", ["0", "", "false", "off", "no", "FALSE", " 0 "])
async def test_flag_string_zero_is_false(client, fake_garmin_client, monkeypatch, value):
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", value)
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_sets_status"] == "not_attempted"
    assert fake_garmin_client.pushed_exercise_sets == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " on "])
async def test_flag_truthy_strings_enable(client, fake_garmin_client, monkeypatch, value):
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", value)
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_sets_status"] == "synced"
    assert len(fake_garmin_client.pushed_exercise_sets) == 1


async def test_exercise_without_category_is_skipped_in_sets(client, fake_garmin_client, monkeypatch):
    """No category means no set entries for that exercise. Guessing one would
    file the wrong movement under a real Garmin exercise -- and Cadence sends
    a bare `null` precisely when it does not know the variant (D-018), so this
    is the common case, not an edge one. The activity itself is still
    created, which is the part that matters."""
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json={
            **BODY,
            "exercises": [
                {"name": "Bench Press", "garmin_category": "BENCH_PRESS", "sets": 2, "reps": 10},
                {"name": "Farmer Carry", "garmin_category": None, "sets": 3, "reps": 1, "seconds": 30},
            ],
        },
    )
    assert resp.status_code == 202

    entries = fake_garmin_client.pushed_exercise_sets[0]["payload"]["exerciseSets"]
    # Two sets from the first exercise, nothing from the second.
    assert len(entries) == 2
    categories = {entry["exercises"][0]["category"] for entry in entries}
    assert categories == {"BENCH_PRESS"}


async def test_all_categories_missing_sends_nothing(client, fake_garmin_client, monkeypatch):
    """An empty payload is not worth a request, and must not be recorded as
    a failure either -- nothing failed."""
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/activity",
        json={**BODY, "exercises": [{"name": "Mystery Move", "sets": 3, "reps": 10}]},
    )
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert resp.json()["garmin_sets_status"] == "not_attempted"
    assert fake_garmin_client.pushed_exercise_sets == []


async def test_sets_payload_shape(client, fake_garmin_client, monkeypatch):
    """Pins the BELIEVED shape in one place so JD's probe-1 result (one
    get_activity_exercise_sets call against a real strength activity) has an
    obvious landing site. One entry per set; weight in grams; duration in
    seconds; sub-category passed through as `name`, which Garmin accepts as
    null under a known parent."""
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    monkeypatch.setenv("TZ", "UTC")
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    pushed = fake_garmin_client.pushed_exercise_sets[0]
    assert pushed["activity_id"] == "19283746501"
    entries = pushed["payload"]["exerciseSets"]
    assert len(entries) == 3  # sets: 3
    assert entries[0] == {
        "setType": "ACTIVE",
        "startTime": "2026-09-06T08:00:00.0",
        "repetitionCount": 10,
        "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
        "weight": 40000.0,
    }


async def test_sets_status_persisted_to_the_row(client, monkeypatch):
    monkeypatch.setenv("VITALFORGE_GARMIN_EXERCISE_SETS", "1")
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_sets_status FROM strength_sessions")
        ).fetchone()
    finally:
        await db.close()
    assert row["garmin_sets_status"] == "synced"
