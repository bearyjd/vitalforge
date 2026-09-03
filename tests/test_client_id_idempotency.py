"""A6 (Bascule docs/prp/00-design.md SS4.4): client_id + captured_at
idempotency for POST /api/weight.

Two halves, tested separately:
  1. client_id gives an exact-identity match regardless of how far apart the
     two requests' *receipt* times are -- the property a same-session retry
     or a future replay needs.
  2. captured_at anchors the timestamp+weight dedup window on the client's
     true capture time instead of this request's receipt time, so a replay
     POSTed long after the original weigh-in can still line up with a
     pre-existing row that was itself stored near its own true capture time.

Same harness as test_dedup.py: no Docker, no network, `weight_app_module`
fakes Garmin and points the DB at a tmp_path SQLite file.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def seed_row(
    weight_grams: int,
    seconds_ago: float = 0,
    *,
    client_id: str | None = None,
    body_fat_pct=None,
    synced_to_garmin=1,
) -> tuple[int, str]:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    weight_kg = weight_grams / 1000.0
    weight_lbs = weight_kg * 2.20462
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, "
            "synced_to_garmin, body_fat_pct, client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, round(weight_lbs, 2), round(weight_kg, 2), weight_grams, ts, synced_to_garmin, body_fat_pct, client_id),
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


# --- client_id: exact-identity match, independent of the time window -------


async def test_client_id_match_short_circuits_time_window(client):
    """A row 600s old (far outside the 60s window) still dedups on a matching
    client_id -- the primary lookup runs before the window query at all."""
    row_id, _ = await seed_row(84096, seconds_ago=600, client_id="reading-1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id
    assert await row_count() == 1


async def test_client_id_match_ignores_weight_tolerance_but_flags_the_disagreement(client):
    """Identity trumps the +-50g tolerance for MATCHING -- a client_id match
    is the same reading by definition, not a fuzzy candidate, so it's still
    treated as one row. But unlike the window path (whose own +-50g bound
    makes a small difference deliberately silent), any weight disagreement
    under a client_id match is surfaced as a conflict: a colliding client_id
    across two genuinely different readings should never look identical to a
    clean match (Devil's-advocate review, Round 1)."""
    row_id, _ = await seed_row(84096, seconds_ago=5, client_id="reading-1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": (84096 + 5000) / 1000, "unit": "kg", "client_id": "reading-1"},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id
    assert await row_count() == 1
    assert body["conflict"] is True
    assert "weight" in body["conflict_fields"]
    # the stored weight is authoritative and never silently overwritten
    assert body["weight_kg"] == pytest.approx(84.1, abs=0.01)


async def test_client_id_match_with_identical_weight_raises_no_conflict(client):
    """The weight-conflict check must not fire on genuine idempotent
    replay -- a same client_id, same weight resubmission is exactly the
    no-op the whole feature exists to support."""
    await seed_row(84096, seconds_ago=5, client_id="reading-1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 84096 / 1000, "unit": "kg", "client_id": "reading-1"},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert "conflict" not in body


async def test_client_id_conflict_within_window_is_flagged_not_overwritten(client):
    """A different client_id landing inside the timestamp+weight window is a
    genuine collision between two distinct readings, not enrichment -- the
    existing row's identity must not be silently overwritten (that would
    make a later replay of the ORIGINAL reading match the wrong row)."""
    row_id, _ = await seed_row(84096, seconds_ago=5, client_id="reading-1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-2"},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["conflict"] is True
    assert "client_id" in body["conflict_fields"]

    db = await get_db()
    try:
        row = await (await db.execute("SELECT client_id FROM weight_log WHERE id = ?", (row_id,))).fetchone()
    finally:
        await db.close()
    assert row["client_id"] == "reading-1"


async def test_client_id_backfills_onto_a_row_matched_by_window(client):
    """A legacy row (no client_id) matched via the timestamp+weight window
    gets the incoming client_id backfilled -- so a *later* replay of this
    same reading can match it directly, without needing the window at all."""
    row_id, _ = await seed_row(84096, seconds_ago=5, client_id=None)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-1"},
    )
    assert resp.json()["deduplicated"] is True

    db = await get_db()
    try:
        row = await (await db.execute("SELECT client_id FROM weight_log WHERE id = ?", (row_id,))).fetchone()
    finally:
        await db.close()
    assert row["client_id"] == "reading-1"


async def test_client_id_repeat_post_same_weight_stays_one_row(client, fake_garmin_client):
    """The core idempotency property: POSTing the identical reading twice,
    client_id included both times, never creates a second row."""
    resp1 = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-1"},
    )
    assert resp1.json()["success"] is True
    resp2 = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-1"},
    )
    assert resp2.json()["deduplicated"] is True
    assert await row_count() == 1
    assert len(fake_garmin_client.pushed_weights) == 1


async def test_client_id_replay_enriches_via_client_id_outside_window(client, fake_garmin_client):
    """A field-filling replay (WP-22's whole purpose) lands well outside the
    60s window and must still match by client_id, then enrich and re-push."""
    row_id, _ = await seed_row(84096, seconds_ago=3600, client_id="reading-1")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "client_id": "reading-1", "body_fat_pct": 18.4},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["enriched"] is True
    assert body["id"] == row_id
    assert len(fake_garmin_client.pushed_weights) == 1


# --- captured_at: anchors the window for rows with no client_id yet --------


async def test_captured_at_anchors_window_for_a_delayed_replay(client):
    """The other half of A6: a legacy row (no client_id, e.g. pre-fix) was
    stored near its own true capture time. A replay arriving long after that
    (receipt time far outside the window) still matches it, because the
    window is evaluated against the replay's captured_at, not its receipt
    time."""
    original_capture = datetime.now(timezone.utc) - timedelta(days=90)
    row_id, _ = await seed_row(84096, seconds_ago=(datetime.now(timezone.utc) - original_capture).total_seconds())

    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={
            "weight": 185.4,
            "unit": "lbs",
            "client_id": "reading-1",
            "captured_at": original_capture.isoformat(),
        },
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id
    assert await row_count() == 1


async def test_captured_at_outside_window_of_stored_row_creates_new_row(client):
    """The residual gap, made explicit: captured_at only helps when it's
    close to what the existing row's own timestamp actually is. A captured_at
    that itself misses the window (e.g. the client's own clock is wrong, or
    the original row's stored timestamp isn't a good proxy for its true
    capture time) does not force a match -- it inserts as new, same as today."""
    await seed_row(84096, seconds_ago=90 * 86400)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={
            "weight": 185.4,
            "unit": "lbs",
            "client_id": "reading-1",
            "captured_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        },
    )
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_captured_at_absent_behaves_exactly_like_today(client):
    """No captured_at (pwa/tasker/legacy bascule) means the window still
    anchors on receipt time, unchanged."""
    await seed_row(84096, seconds_ago=600)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_captured_at_is_stored_as_the_row_timestamp(client):
    captured_at = datetime.now(timezone.utc) - timedelta(days=5)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": captured_at.isoformat()},
    )
    body = resp.json()
    assert datetime.fromisoformat(body["timestamp"]) == captured_at


async def test_captured_at_is_used_as_the_garmin_push_timestamp_on_fresh_insert(client, fake_garmin_client):
    """Not just the stored row -- the timestamp actually forwarded to Garmin
    must be the true capture time too, not receipt time. This is the first
    code path able to push a backdated timestamp to Garmin at all (Devil's-
    advocate review, Round 2) -- unverified against real Garmin Connect
    behavior for a large backdate, but this pins what THIS app sends."""
    captured_at = datetime.now(timezone.utc) - timedelta(days=90)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4, "captured_at": captured_at.isoformat()},
    )
    assert resp.json()["synced_to_garmin"] is True
    pushed_ts = fake_garmin_client.pushed_weights[-1]["timestamp"]
    assert pushed_ts == captured_at


async def test_captured_at_without_client_id_still_anchors_the_window(client):
    """captured_at doesn't require client_id -- a client resubmitting a
    known-old reading with only a timestamp (e.g. a one-time migration from
    another data source) still benefits from anchoring the dedup window on
    the true capture time instead of receipt time."""
    original_capture = datetime.now(timezone.utc) - timedelta(days=30)
    row_id, _ = await seed_row(84096, seconds_ago=(datetime.now(timezone.utc) - original_capture).total_seconds())

    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": original_capture.isoformat()},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id


async def test_captured_at_naive_datetime_rejected(client):
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": "2026-05-01T12:00:00"},
    )
    assert resp.status_code == 422


async def test_captured_at_non_utc_offset_is_stored_normalized_to_utc(client):
    """A valid non-UTC offset is accepted (WeightIn only requires SOME
    offset, not specifically +00:00) but must be normalized before storage --
    every pre-existing row's TEXT timestamp is always +00:00, and the
    sargable dedup prefilter is a plain string comparison that only works
    when every row shares one offset convention (codex review finding)."""
    captured_at = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": captured_at.isoformat()},
    )
    body = resp.json()
    stored = datetime.fromisoformat(body["timestamp"])
    assert stored == captured_at  # same instant
    assert body["timestamp"].endswith("+00:00")  # but normalized on the wire


async def test_captured_at_non_utc_offset_still_dedup_matches_a_utc_stored_row(client):
    """The failure mode this guards against, made concrete: a row is stored
    (as every row is) with a +00:00 timestamp. A replay expresses the exact
    same instant in a different offset. Without normalizing captured_at
    before computing the prefilter, this reproduces the exact scenario the
    codex review named: the TEXT prefilter string-compares "...T10:00:00-05:00"
    against a "...T15:00:00+00:00" row and can exclude a real match before
    the authoritative julianday() bounds ever see it, inserting a duplicate."""
    row_id, ts = await seed_row(84096, seconds_ago=0)  # stored +00:00, like every real row
    stored_instant = datetime.fromisoformat(ts)

    # The identical instant, expressed in a different offset.
    replay_captured_at = stored_instant.astimezone(timezone(timedelta(hours=-5)))
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": replay_captured_at.isoformat()},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id
    assert await row_count() == 1


async def test_captured_at_far_future_rejected(client):
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": future.isoformat()},
    )
    assert resp.status_code == 422


async def test_captured_at_small_clock_skew_into_the_future_accepted(client):
    """Within the tolerance -- ordinary clock skew, not a real future
    weigh-in -- must not 422."""
    slightly_ahead = datetime.now(timezone.utc) + timedelta(seconds=10)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "captured_at": slightly_ahead.isoformat()},
    )
    assert resp.status_code == 200


# --- unique index -----------------------------------------------------------


async def test_unique_index_rejects_duplicate_client_id_for_same_person(initialized_db):
    """DB-level guarantee, independent of the request-path BEGIN IMMEDIATE
    serialization: a direct second INSERT with the same (person_id,
    client_id) must fail, not silently succeed."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, "
            "synced_to_garmin, client_id) VALUES (?, 185.4, 84.1, 84096, ?, 0, 'reading-1')",
            (person_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, "
                "synced_to_garmin, client_id) VALUES (?, 186.0, 84.4, 84400, ?, 0, 'reading-1')",
                (person_id, datetime.now(timezone.utc).isoformat()),
            )
    finally:
        await db.close()


async def test_unique_index_allows_multiple_null_client_ids(initialized_db):
    """NULL client_id (every legacy row, every non-replay client) must not
    collide with itself under the partial unique index."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        for _ in range(3):
            await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, "
                "synced_to_garmin, client_id) VALUES (?, 185.4, 84.1, 84096, ?, 0, NULL)",
                (person_id, datetime.now(timezone.utc).isoformat()),
            )
        await db.commit()
    finally:
        await db.close()
    assert await row_count() == 3
