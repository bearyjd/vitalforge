"""B4: dedup for POST /api/weight (docs/prp/00-design.md SS3.7). Two bridges
posting the same weigh-in within +-50g/60s collapse into one row and one
Garmin push; a real second weigh-in outside that window does not.

Time is not mocked: tests seed a prior row with a controlled `timestamp` via
direct SQL, then POST "now". The window is evaluated against the request's
own receipt time, so controlling only the stored row is sufficient.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def seed_row(
    weight_grams: int,
    seconds_ago: float = 0,
    *,
    body_fat_pct=None,
    body_water_pct=None,
    muscle_pct=None,
    bone_mass_kg=None,
    source=None,
    synced_to_garmin=1,
) -> tuple[int, str]:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    weight_kg = weight_grams / 1000.0
    weight_lbs = weight_kg * 2.20462
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
            "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                round(weight_lbs, 2),
                round(weight_kg, 2),
                weight_grams,
                ts,
                synced_to_garmin,
                body_fat_pct,
                body_water_pct,
                muscle_pct,
                bone_mass_kg,
                source,
            ),
        )
        await db.commit()
        return cursor.lastrowid, ts
    finally:
        await db.close()


async def row_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM weight_log")).fetchone())[0]
    finally:
        await db.close()


async def test_duplicate_within_window_returns_deduplicated_true(client):
    row_id, ts = await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id


async def test_duplicate_not_pushed_to_garmin_twice(client, fake_garmin_client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    assert len(fake_garmin_client.pushed_weights) == 0


async def test_dedup_response_returns_original_row_id_and_timestamp(client):
    row_id, ts = await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert body["id"] == row_id
    assert body["timestamp"] == ts


async def test_dedup_tolerance_49g_collapses(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": (84096 + 49) / 1000, "unit": "kg"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_tolerance_exactly_50g_collapses(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": (84096 + 50) / 1000, "unit": "kg"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_tolerance_51g_creates_second_row(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post("/api/weight", json={"weight": (84096 + 51) / 1000, "unit": "kg"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_dedup_ignores_source(client):
    row_id, _ = await seed_row(84096, seconds_ago=5, source="tasker")
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id


async def test_weighins_600s_apart_both_stored(client):
    await seed_row(84096, seconds_ago=600)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert body["success"] is True
    assert await row_count() == 2


async def test_dedup_window_58s_collapses(client):
    """A few seconds of margin inside the 60s edge, since real wall-clock
    time elapses between seeding and the POST landing."""
    await seed_row(84096, seconds_ago=58)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_window_62s_does_not_collapse(client):
    await seed_row(84096, seconds_ago=62)
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_future_dated_row_does_not_swallow_real_weighin(client):
    """Regression for the missing-upper-bound bug: a row timestamped in the
    future (e.g. from clock skew) must not match every later request and
    silently discard real weigh-ins forever."""
    await seed_row(84096, seconds_ago=-3600)  # one hour in the future
    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_enrichment_updates_null_columns_and_repushes(client, fake_garmin_client):
    row_id, ts = await seed_row(84096, seconds_ago=5, synced_to_garmin=1)
    resp = await client.post(
        "/api/weight",
        json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4, "bone_mass_kg": 3.2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["enriched"] is True
    assert body["id"] == row_id
    assert len(fake_garmin_client.pushed_weights) == 1


async def test_enrichment_uses_original_timestamp(client, fake_garmin_client):
    """weight_app_module's push_weight double records the raw datetime it's
    called with (the route<->push_weight boundary), not Garmin's wire-format
    string -- that string-formatting is push_weight's own job, already
    covered by test_garmin_mapping.py."""
    row_id, ts = await seed_row(84096, seconds_ago=5)
    await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4})
    pushed_ts = fake_garmin_client.pushed_weights[-1]["timestamp"]
    assert pushed_ts == datetime.fromisoformat(ts)


async def test_enrichment_push_failure_sets_synced_to_garmin_zero(client, weight_app_module, monkeypatch):
    row_id, ts = await seed_row(84096, seconds_ago=5, synced_to_garmin=1)

    def failing_push(weight_grams, timestamp=None, **kwargs):
        raise RuntimeError("synthetic Garmin outage")

    monkeypatch.setattr(weight_app_module, "push_weight", failing_push)

    resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4})
    body = resp.json()
    assert body["enriched"] is True
    assert body["synced_to_garmin"] is False
    assert "garmin_error" in body


async def test_conflicting_value_does_not_overwrite_and_flags_conflict(client, fake_garmin_client, caplog):
    row_id, ts = await seed_row(84096, seconds_ago=5, body_fat_pct=18.4)
    with caplog.at_level(logging.WARNING):
        resp = await client.post("/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 20.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["conflict"] is True
    assert len(fake_garmin_client.pushed_weights) == 0
    assert "body_fat_pct" in caplog.text

    db = await get_db()
    try:
        row = await (await db.execute("SELECT body_fat_pct FROM weight_log WHERE id = ?", (row_id,))).fetchone()
    finally:
        await db.close()
    assert row["body_fat_pct"] == 18.4


async def test_enrichment_with_partial_conflict_updates_only_null_columns(client, fake_garmin_client):
    """A request can enrich some fields and conflict on others in the same
    POST -- each field is classified independently, not the request as a
    whole. The stored value wins on the conflicting field; the null field
    gets enriched; the re-push carries the final (post-merge) row."""
    row_id, ts = await seed_row(84096, seconds_ago=5, body_fat_pct=18.4, bone_mass_kg=None)
    resp = await client.post(
        "/api/weight",
        json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 20.0, "bone_mass_kg": 3.2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["enriched"] is True
    assert body["conflict"] is True

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT body_fat_pct, bone_mass_kg FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["body_fat_pct"] == 18.4  # stored value wins, not overwritten
    assert row["bone_mass_kg"] == 3.2  # null column enriched

    pushed = fake_garmin_client.pushed_weights[-1]
    assert pushed["percent_fat"] == 18.4  # the stored (not incoming) value
    assert pushed["bone_mass_kg"] == 3.2
