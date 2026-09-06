"""The UTC-to-local-wall-clock conversion for create_manual_activity.

The single highest-value guard in this feature. VitalForge stores UTC ISO
strings everywhere, but create_manual_activity wants a LOCAL WALL-CLOCK string
carrying NO offset, plus the IANA zone name alongside. Passing a UTC string
with the local zone name, or a string that carries an offset, silently
misfiles the activity by the offset amount -- nothing errors, nothing logs,
and nobody notices for weeks. It is the same class of bug push_weight's
offset-less strftime already caused once.

TZ is read per call rather than captured at import precisely so these tests
can set it: the app module is imported once per session via importlib, so a
module-level constant would freeze whatever the environment held during the
first test that imported it.
"""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db
from tests.conftest import PERSON_PREFIX

# 08:00 UTC on a date when Europe/Paris is CEST (UTC+2), so the expected
# wall clock is 10:00 and a passed-through UTC string would read 08:00.
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


async def test_start_converted_to_local_wall_clock(client, fake_garmin_client, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202

    call = fake_garmin_client.created_activities[0]
    assert call["start_datetime"] == "2026-09-06T10:00:00.000"
    assert call["time_zone"] == "Europe/Paris"


async def test_start_string_carries_no_offset(client, fake_garmin_client, monkeypatch):
    """An offset in the string is the failure mode Garmin accepts silently
    and files wrong."""
    monkeypatch.setenv("TZ", "America/New_York")
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    start_datetime = fake_garmin_client.created_activities[0]["start_datetime"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000", start_datetime)
    assert "+" not in start_datetime
    assert start_datetime == "2026-09-06T04:00:00.000"


async def test_tz_unset_falls_back_to_utc_and_logs(client, fake_garmin_client, monkeypatch, caplog):
    """Never guess the host zone. Fall back to UTC and SAY SO -- a silent
    fallback makes a misconfigured deployment invisible."""
    monkeypatch.delenv("TZ", raising=False)
    with caplog.at_level("WARNING"):
        await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    call = fake_garmin_client.created_activities[0]
    assert call["time_zone"] == "UTC"
    assert call["start_datetime"] == "2026-09-06T08:00:00.000"
    assert "TZ is unset" in caplog.text


async def test_empty_tz_is_treated_as_unset(client, fake_garmin_client, monkeypatch):
    """TZ="" is what an unset variable looks like in a compose file that
    passes it through unconditionally. ZoneInfo("") raises, which would turn
    a configuration gap into garmin_status='failed' on every session."""
    monkeypatch.setenv("TZ", "")
    resp = await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    assert resp.status_code == 202
    assert resp.json()["garmin_status"] == "synced"
    assert fake_garmin_client.created_activities[0]["time_zone"] == "UTC"


async def test_stored_start_time_is_utc(client, monkeypatch):
    """Storage stays UTC regardless of TZ. The local wall clock exists only
    for the duration of the Garmin call."""
    monkeypatch.setenv("TZ", "Europe/Paris")
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT start_time_utc FROM strength_sessions")
        ).fetchone()
    finally:
        await db.close()
    assert row["start_time_utc"] == "2026-09-06T08:00:00+00:00"


async def test_non_utc_offset_is_normalized_before_storage(client, monkeypatch, fake_garmin_client):
    """`start` need only carry SOME offset, not specifically +00:00. A
    client-local "-04:00" must be normalized to UTC on the way in, or the
    stored string sorts wrongly against every other row in the `since` filter
    and the Garmin conversion starts from the wrong instant."""
    monkeypatch.setenv("TZ", "Europe/Paris")
    await client.post(
        f"{PERSON_PREFIX}/api/activity", json={**BODY, "start": "2026-09-06T04:00:00-04:00"}
    )

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT start_time_utc FROM strength_sessions")
        ).fetchone()
    finally:
        await db.close()
    assert row["start_time_utc"] == "2026-09-06T08:00:00+00:00"
    assert fake_garmin_client.created_activities[0]["start_datetime"] == "2026-09-06T10:00:00.000"


async def test_activity_type_key_is_strength_training(client, fake_garmin_client):
    """Pinned so JD's probe-3 answer (get_activity_types() against a live
    account) has one obvious place to land if Garmin names it something
    else."""
    await client.post(f"{PERSON_PREFIX}/api/activity", json=BODY)
    call = fake_garmin_client.created_activities[0]
    assert call["type_key"] == "strength_training"
    assert call["distance_km"] == 0.0
    assert call["duration_min"] == 42
