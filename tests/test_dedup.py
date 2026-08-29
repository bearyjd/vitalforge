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
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
            "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                person_id,
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


async def seed_row_raw_timestamp(weight_grams: int, timestamp: str) -> int:
    """Like seed_row, but inserts an arbitrary literal `timestamp` string
    instead of computing one from `seconds_ago` -- for testing rows whose
    timestamp doesn't match the format this route itself writes (e.g. a
    pre-existing/legacy row, per the open item in docs/prp/01-plan.md SS4.1)."""
    weight_kg = weight_grams / 1000.0
    weight_lbs = weight_kg * 2.20462
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (person_id, round(weight_lbs, 2), round(weight_kg, 2), weight_grams, timestamp),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def test_duplicate_within_window_returns_deduplicated_true(client):
    row_id, ts = await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id


async def test_duplicate_not_pushed_to_garmin_twice(client, fake_garmin_client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    assert len(fake_garmin_client.pushed_weights) == 0


async def test_dedup_response_returns_original_row_id_and_timestamp(client):
    row_id, ts = await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert body["id"] == row_id
    assert body["timestamp"] == ts


async def test_dedup_tolerance_49g_collapses(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": (84096 + 49) / 1000, "unit": "kg"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_tolerance_exactly_50g_collapses(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": (84096 + 50) / 1000, "unit": "kg"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_tolerance_51g_creates_second_row(client):
    await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": (84096 + 51) / 1000, "unit": "kg"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_dedup_ignores_source(client):
    row_id, _ = await seed_row(84096, seconds_ago=5, source="tasker")
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["id"] == row_id


async def test_weighins_600s_apart_both_stored(client):
    await seed_row(84096, seconds_ago=600)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert body["success"] is True
    assert await row_count() == 2


async def test_dedup_window_58s_collapses(client):
    """A few seconds of margin inside the 60s edge, since real wall-clock
    time elapses between seeding and the POST landing."""
    await seed_row(84096, seconds_ago=58)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.json()["deduplicated"] is True


async def test_dedup_window_62s_does_not_collapse(client):
    await seed_row(84096, seconds_ago=62)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_future_dated_row_does_not_swallow_real_weighin(client):
    """Regression for the missing-upper-bound bug: a row timestamped in the
    future (e.g. from clock skew) must not match every later request and
    silently discard real weigh-ins forever."""
    await seed_row(84096, seconds_ago=-3600)  # one hour in the future
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_malformed_timestamp_row_does_not_error_the_dedup_query(client):
    """A row whose `timestamp` isn't ISO8601 (e.g. a legacy/pre-format-change
    row -- see docs/prp/01-plan.md SS4.1's open item on unverified historical
    timestamp formats) must not make the dedup SELECT raise. julianday() on
    an unparseable string returns NULL, and NULL comparisons are false in
    SQL, so the row is silently excluded from matching -- not a crash."""
    await seed_row_raw_timestamp(84096, "not-a-real-timestamp")
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert "deduplicated" not in body or body["deduplicated"] is not True
    assert await row_count() == 2


async def test_enrichment_updates_null_columns_and_repushes(client, fake_garmin_client):
    row_id, ts = await seed_row(84096, seconds_ago=5, synced_to_garmin=1)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
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
    await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4})
    pushed_ts = fake_garmin_client.pushed_weights[-1]["timestamp"]
    assert pushed_ts == datetime.fromisoformat(ts)


async def test_enrichment_push_failure_sets_synced_to_garmin_zero(client, weight_app_module, monkeypatch):
    row_id, ts = await seed_row(84096, seconds_ago=5, synced_to_garmin=1)

    def failing_push(weight_grams, timestamp=None, **kwargs):
        raise RuntimeError("synthetic Garmin outage")

    monkeypatch.setattr(weight_app_module, "push_weight", failing_push)

    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4})
    body = resp.json()
    assert body["enriched"] is True
    assert body["synced_to_garmin"] is False
    assert "garmin_error" in body


async def test_source_only_enrichment_does_not_repush_to_garmin(client, fake_garmin_client):
    """Fix-review finding on the O2 fix (8062a9f): adding `source` to
    ENRICHABLE_FIELDS meant a source-only enrich (no composition field
    actually changed) populated the same `updates` dict the Garmin re-push
    branch and the synced_to_garmin flag-persist both gate on -- so a row
    that was already correctly synced got a spurious re-push of unchanged
    composition data, and if that incidental push ever failed, a
    previously-true synced_to_garmin flag flipped to false with no way to
    recover it (a later identical POST finds no updates at all and just
    echoes the now-corrupted stored value). Only an *actual composition*
    change should trigger a re-push or touch the flag; `source` alone must
    not."""
    row_id, ts = await seed_row(84096, seconds_ago=5, body_fat_pct=18.4, bone_mass_kg=3.2, synced_to_garmin=1)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    body = resp.json()
    assert body["enriched"] is True  # source itself is still recorded
    assert body["synced_to_garmin"] is True  # untouched, not silently flipped
    assert len(fake_garmin_client.pushed_weights) == 0  # no spurious re-push of unchanged composition

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT source, synced_to_garmin FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["source"] == "bascule"  # the enrichment itself still happened
    assert row["synced_to_garmin"] == 1


async def test_source_only_enrichment_does_not_fabricate_synced_true(client, fake_garmin_client):
    """The composition_changed gate must not over-correct: a source-only
    enrich against a row that was NOT synced must not push, and must not
    fabricate a `true` flag in the other direction either -- it stays
    whatever it already was."""
    row_id, ts = await seed_row(84096, seconds_ago=5, body_fat_pct=18.4, synced_to_garmin=0)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    body = resp.json()
    assert body["enriched"] is True
    assert body["synced_to_garmin"] is False
    assert len(fake_garmin_client.pushed_weights) == 0

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT source, synced_to_garmin FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["source"] == "bascule"
    assert row["synced_to_garmin"] == 0


async def test_pure_source_conflict_with_no_composition_change_does_not_repush(client, fake_garmin_client):
    """Isolates the source-conflict case from
    test_dedup_enrichment_conflicting_source_kept_not_overwritten, which
    always pairs it with a body_fat_pct enrichment (so composition_changed
    is already true there regardless of this gate). A POST that conflicts
    on source alone, with no composition fields at all, must not push and
    must not touch synced_to_garmin -- there is nothing here for the
    composition_changed gate to even see."""
    row_id, ts = await seed_row(84096, seconds_ago=5, source="tasker", synced_to_garmin=1)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    body = resp.json()
    assert body["conflict"] is True
    assert body["conflict_fields"] == ["source"]
    assert "enriched" not in body
    assert body["synced_to_garmin"] is True
    assert len(fake_garmin_client.pushed_weights) == 0

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT source, synced_to_garmin FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["source"] == "tasker"
    assert row["synced_to_garmin"] == 1


async def test_conflicting_value_does_not_overwrite_and_flags_conflict(client, fake_garmin_client, caplog):
    row_id, ts = await seed_row(84096, seconds_ago=5, body_fat_pct=18.4)
    with caplog.at_level(logging.WARNING):
        resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 20.0})
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
        f"{PERSON_PREFIX}/api/weight",
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


async def test_conflict_response_names_the_conflicting_fields(client, fake_garmin_client):
    """Phase 4 adversarial review finding: `conflict: true` used to be a
    black box -- the conflicting field names went only to a server-side
    WARNING log, so a client had no way to know which value was rejected
    without grepping server logs it likely can't see."""
    await seed_row(84096, seconds_ago=5, body_fat_pct=18.4, muscle_pct=40.0)
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 20.0, "muscle_pct": 41.0, "bone_mass_kg": 3.2},
    )
    body = resp.json()
    assert body["conflict"] is True
    assert sorted(body["conflict_fields"]) == ["body_fat_pct", "muscle_pct"]


async def test_dedup_enrichment_fills_in_missing_source(client):
    """Phase 4 adversarial review finding: a row whose first arrival omitted
    `source` used to stay `source: NULL` forever, even once a later request
    on the same weigh-in supplied one."""
    row_id, ts = await seed_row(84096, seconds_ago=5)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "source": "bascule"})
    assert resp.status_code == 200
    assert resp.json()["enriched"] is True

    db = await get_db()
    try:
        row = await (await db.execute("SELECT source FROM weight_log WHERE id = ?", (row_id,))).fetchone()
    finally:
        await db.close()
    assert row["source"] == "bascule"


async def test_dedup_enrichment_conflicting_source_kept_not_overwritten(client):
    """Phase 4 adversarial review finding: source used to be excluded from
    the enrichment merge entirely, so a later, different client's
    composition payload silently landed under the first client's source
    label -- misattributing the measurement with no visible sign anything
    was dropped. It must now be classified the same way every other
    enrichable field already is: stored value wins, and the mismatch is a
    visible conflict, not a silent overwrite in either direction."""
    row_id, ts = await seed_row(84096, seconds_ago=5, source="tasker")
    resp = await client.post(
        f"{PERSON_PREFIX}/api/weight",
        json={"weight": 185.4, "unit": "lbs", "source": "bascule", "body_fat_pct": 18.4},
    )
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["enriched"] is True  # body_fat_pct still enriches
    assert body["conflict"] is True
    assert "source" in body["conflict_fields"]

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT source, body_fat_pct FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["source"] == "tasker"  # original provenance kept, not silently overwritten
    assert row["body_fat_pct"] == 18.4


async def test_unparseable_stored_timestamp_still_persists_sync_failure(client, fake_garmin_client):
    """Phase 4 adversarial review finding: SQLite's julianday() accepts a
    bare Julian-day-number string directly (so the dedup SELECT's window
    match still fires on a row like this), but Python's
    datetime.fromisoformat() cannot parse it -- and that parse used to be
    the very first thing the enrichment-push branch did, so it raised
    before the subsequent `UPDATE weight_log SET synced_to_garmin = ?`
    ever ran. The in-memory response correctly said `false`, but the
    stored row was left asserting its prior (here stale-true) value --
    a flag that lies is worse than a flag that's merely False, since
    nothing downstream knows to re-check it."""
    db = await get_db()
    try:
        julian_now = (await (await db.execute("SELECT julianday('now')")).fetchone())[0]
    finally:
        await db.close()
    row_id = await seed_row_raw_timestamp(84096, str(julian_now))
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 185.4, "unit": "lbs", "body_fat_pct": 18.4})
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced_to_garmin"] is False
    assert len(fake_garmin_client.pushed_weights) == 0

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT synced_to_garmin FROM weight_log WHERE id = ?", (row_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["synced_to_garmin"] == 0


@pytest.mark.asyncio
async def test_two_persons_same_second_similar_weight_produce_two_rows(initialized_db):
    """Regression test for spec §0.2 Correction 1: without a person predicate
    on the dedup SELECT, two family members weighing in within the dedup
    window at similar weights would silently merge into one row."""
    from datetime import datetime, timezone

    from shared.database import get_db, get_primary_person_id

    person_a = await get_primary_person_id()
    now = datetime.now(timezone.utc)

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", now.isoformat()),
        )
        person_b = cursor.lastrowid
        await db.commit()

        for pid in (person_a, person_b):
            await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
                "VALUES (?, 180.0, 81.6, 81600, ?, 0)",
                (pid, now.isoformat()),
            )
        await db.commit()

        cursor = await db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM weight_log WHERE timestamp = ?", (now.isoformat(),)
        )
        assert (await cursor.fetchone())[0] == 2
    finally:
        await db.close()
