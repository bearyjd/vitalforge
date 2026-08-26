"""Tests for the FIT-file local activity import feature (FIT-only first
slice -- TCX/GPX deferred). Covers the happy path, both dedup stages
(exact file-hash and near-duplicate), the concurrent-upload race (mirrors
tests/test_dedup_concurrency.py's pattern for weight_log), and the two
upload-safety checks (content-sniffed magic bytes, size cap) that must
reject gracefully rather than 500.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db
from tests.fixtures.fit_builder import (
    build_fit_file_with_non_numeric_calories,
    build_minimal_fit_file,
)


@pytest.fixture
async def client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def make_fit_bytes(**overrides) -> bytes:
    defaults = dict(
        start_time=datetime(2026, 8, 20, 7, 30, tzinfo=timezone.utc),
        sport=1,
        elapsed_seconds=1800,
        distance_m=5000,
        calories=400,
        avg_hr=140,
        max_hr=175,
        ascent_m=50,
    )
    defaults.update(overrides)
    return build_minimal_fit_file(**defaults)


async def activity_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM activities")).fetchone())[0]
    finally:
        await db.close()


async def test_valid_fit_upload_happy_path(client):
    data = make_fit_bytes()

    resp = await client.post(
        "/api/import/activity",
        files={"file": ("morning_run.fit", data, "application/octet-stream")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sport"] == "running"
    assert body["duration_seconds"] == 1800
    assert body["distance_m"] == 5000.0
    assert body["calories"] == 400
    assert body["avg_hr"] == 140
    assert body["max_hr"] == 175
    assert body["elevation_gain_m"] == 50.0
    assert body["source_format"] == "fit"
    assert body["start_time_utc"].startswith("2026-08-20T07:30:00")
    assert "duplicate" not in body
    assert await activity_count() == 1

    detail = await client.get(f"/api/activities/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["raw_summary"]["total_calories"] == 400


async def test_duplicate_file_hash_rejected_gracefully(client):
    data = make_fit_bytes()

    first = await client.post("/api/import/activity", files={"file": ("run.fit", data)})
    assert first.status_code == 200

    second = await client.post("/api/import/activity", files={"file": ("run-copy.fit", data)})

    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["duplicate_reason"] == "exact_duplicate"
    assert body["id"] == first.json()["id"]
    assert await activity_count() == 1


async def test_near_duplicate_different_bytes_same_activity_rejected(client):
    """Same sport/start_time but different file bytes (e.g. re-exported by
    the watch) -- different file_sha256, so the exact-hash stage misses it,
    but the (sport, start_time_utc) window stage should still catch it."""
    first_data = make_fit_bytes(calories=400)
    second_data = make_fit_bytes(calories=401)  # 1 byte different -> different hash
    assert first_data != second_data

    first = await client.post("/api/import/activity", files={"file": ("a.fit", first_data)})
    assert first.status_code == 200

    second = await client.post("/api/import/activity", files={"file": ("b.fit", second_data)})

    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["duplicate_reason"] == "near_duplicate"
    assert await activity_count() == 1


async def test_distinct_activities_both_stored(client):
    first_data = make_fit_bytes(start_time=datetime(2026, 8, 20, 7, 30, tzinfo=timezone.utc))
    second_data = make_fit_bytes(start_time=datetime(2026, 8, 21, 7, 30, tzinfo=timezone.utc))

    first = await client.post("/api/import/activity", files={"file": ("a.fit", first_data)})
    second = await client.post("/api/import/activity", files={"file": ("b.fit", second_data)})

    assert first.status_code == 200
    assert second.status_code == 200
    assert "duplicate" not in second.json()
    assert await activity_count() == 2


async def test_two_concurrent_identical_uploads_store_one_row(client):
    """Mirrors tests/test_dedup_concurrency.py's
    test_two_concurrent_identical_posts_store_one_row: the same atomic
    BEGIN IMMEDIATE check-then-insert pattern, applied to activities."""
    data = make_fit_bytes()

    results = await asyncio.gather(
        client.post("/api/import/activity", files={"file": ("run.fit", data)}),
        client.post("/api/import/activity", files={"file": ("run.fit", data)}),
    )

    assert all(r.status_code == 200 for r in results)
    assert await activity_count() == 1
    # Exactly one of the two responses reports the fresh insert, the other
    # reports the duplicate -- never both "fresh" (that would mean the race
    # let two inserts through) and never both "duplicate" (that would mean
    # neither actually wrote the row).
    duplicate_flags = sorted(bool(r.json().get("duplicate")) for r in results)
    assert duplicate_flags == [False, True]


async def test_two_concurrent_distinct_uploads_both_stored(client):
    first_data = make_fit_bytes(start_time=datetime(2026, 8, 20, 7, 30, tzinfo=timezone.utc))
    second_data = make_fit_bytes(start_time=datetime(2026, 8, 22, 7, 30, tzinfo=timezone.utc))

    results = await asyncio.gather(
        client.post("/api/import/activity", files={"file": ("a.fit", first_data)}),
        client.post("/api/import/activity", files={"file": ("b.fit", second_data)}),
    )

    assert all(r.status_code == 200 for r in results)
    assert await activity_count() == 2


async def test_non_fit_file_rejected(client):
    resp = await client.post(
        "/api/import/activity",
        files={"file": ("not_a_fit_file.fit", b"this is definitely not a FIT file" * 10)},
    )

    assert resp.status_code == 400
    assert await activity_count() == 0


async def test_non_fit_file_with_fit_extension_and_content_type_still_rejected(client):
    """Extension and Content-Type are attacker-controlled -- only the actual
    magic bytes are trusted."""
    resp = await client.post(
        "/api/import/activity",
        files={"file": ("totally_legit.fit", b"\x00" * 200, "application/vnd.ant.fit")},
    )

    assert resp.status_code == 400
    assert await activity_count() == 0


async def test_truncated_fit_file_rejected_not_500(client):
    """Passes the magic-byte sniff (valid header) but is corrupt/truncated
    past that -- must still be a 400, not an unhandled exception."""
    data = make_fit_bytes()
    truncated = data[:20]

    resp = await client.post("/api/import/activity", files={"file": ("run.fit", truncated)})

    assert resp.status_code == 400
    assert await activity_count() == 0


async def test_malformed_session_field_rejected_not_500(client):
    """Passes the magic-byte sniff and `fitparse`'s own parse() (structurally
    valid, correct CRC), but the session message declares `total_calories`
    with a non-numeric (string) base type. The `int()` conversion in
    `parse_fit_bytes`'s field-extraction step must be caught and turned into
    the same 400 as any other malformed upload, not surface as an
    unhandled 500."""
    data = build_fit_file_with_non_numeric_calories(
        start_time=datetime(2026, 8, 20, 7, 30, tzinfo=timezone.utc)
    )

    resp = await client.post("/api/import/activity", files={"file": ("run.fit", data)})

    assert resp.status_code == 400
    assert await activity_count() == 0


async def test_oversized_file_rejected(client, dashboard_app_module, monkeypatch):
    monkeypatch.setattr(dashboard_app_module.fit_import, "MAX_UPLOAD_BYTES", 100)
    oversized = make_fit_bytes() + b"\x00" * 500

    resp = await client.post("/api/import/activity", files={"file": ("run.fit", oversized)})

    assert resp.status_code == 413
    assert await activity_count() == 0


async def test_list_and_get_activities(client):
    data = make_fit_bytes()
    created = (await client.post("/api/import/activity", files={"file": ("run.fit", data)})).json()

    listing = await client.get("/api/activities")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    assert body["activities"][0]["id"] == created["id"]
    assert "raw_summary" not in body["activities"][0]

    detail = await client.get(f"/api/activities/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["file_sha256"] == created["file_sha256"]

    missing = await client.get("/api/activities/999999")
    assert missing.status_code == 404
