"""Tests for the vitalforge-weight service's HTTP API.

No Docker, no network, no real Garmin account — `weight_app_module` (see
conftest.py) fully fakes Garmin and points the DB at a tmp_path SQLite file.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import PERSON_PREFIX


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "vitalforge-weight"}


async def test_post_weight_lbs_converts_and_syncs(client, fake_garmin_client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["success"] is True
    assert body["weight_lbs"] == 180.0
    assert body["weight_kg"] == pytest.approx(180.0 / 2.20462, abs=0.01)
    assert body["synced_to_garmin"] is True
    assert "garmin_error" not in body

    # Confirm the fake Garmin client actually recorded the push.
    assert len(fake_garmin_client.pushed_weights) == 1


async def test_post_weight_kg_converts(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 81.5, "unit": "kg"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["weight_kg"] == 81.5
    assert body["weight_lbs"] == pytest.approx(81.5 * 2.20462, abs=0.01)


async def test_invalid_unit_still_returns_400_not_422(client):
    """Pins the one legacy 400 (a retained quirk, not a compatibility
    requirement -- see docs/prp/00-design.md SS3.1) against the 422 migration
    B2 applies to everything else this endpoint validates."""
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 150.0, "unit": "stone"})
    assert resp.status_code == 400


async def test_post_weight_garmin_failure_still_saves_locally(client, weight_app_module, monkeypatch):
    def failing_push(weight_grams, timestamp=None):
        raise RuntimeError("synthetic Garmin outage")

    monkeypatch.setattr(weight_app_module, "push_weight", failing_push)

    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 170.0, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced_to_garmin"] is False
    assert "garmin_error" in body

    # The entry should still show up locally even though Garmin sync failed.
    recent = await client.get(f"{PERSON_PREFIX}/api/weight/recent")
    assert recent.status_code == 200
    assert len(recent.json()) == 1
    assert recent.json()[0]["synced_to_garmin"] is False


async def test_get_recent_weights_orders_newest_first(client):
    for w in (150.0, 151.0, 152.0):
        await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": w, "unit": "lbs"})

    resp = await client.get(f"{PERSON_PREFIX}/api/weight/recent")
    assert resp.status_code == 200
    weights = [row["weight_lbs"] for row in resp.json()]
    assert weights == [152.0, 151.0, 150.0]


async def test_get_weight_trend_returns_entries(client):
    await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 160.0, "unit": "lbs"})
    resp = await client.get(f"{PERSON_PREFIX}/api/weight/trend")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["weight_lbs"] == 160.0


async def test_delete_weight_success(client):
    await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 140.0, "unit": "lbs"})
    recent = await client.get(f"{PERSON_PREFIX}/api/weight/recent")
    entry_id = recent.json()[0]["id"]

    del_resp = await client.delete(f"{PERSON_PREFIX}/api/weight/{entry_id}")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"success": True, "deleted_id": entry_id}

    recent_after = await client.get(f"{PERSON_PREFIX}/api/weight/recent")
    assert recent_after.json() == []


async def test_delete_weight_missing_returns_404(client):
    resp = await client.delete(f"{PERSON_PREFIX}/api/weight/999999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# B2: body-composition intake (docs/prp/01-plan.md SSB2)
# ---------------------------------------------------------------------------

COMPOSITION_PAYLOAD = {
    "weight": 180.0,
    "unit": "lbs",
    "body_fat_pct": 18.4,
    "body_water_pct": 55.2,
    "muscle_pct": 40.1,
    "bone_mass_kg": 3.2,
    "bmi": 25.3,
    "bmr": 1620.0,
    "amr": 2400.0,
    "source": "bascule",
}


async def test_composition_fields_accepted_and_echoed(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json=COMPOSITION_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["body_fat_pct"] == 18.4
    assert body["body_water_pct"] == 55.2
    assert body["muscle_pct"] == 40.1
    assert body["bone_mass_kg"] == 3.2
    assert body["bmi"] == 25.3
    assert body["bmr"] == 1620.0
    assert body["amr"] == 2400.0
    assert body["source"] == "bascule"


async def test_weight_only_payload_still_succeeds(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    for field in ("body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "bmi", "bmr", "amr", "source"):
        assert field not in body


async def test_unknown_field_rejected_422(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "bodyFat": 20})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["type"] == "extra_forbidden"
    assert detail[0]["loc"] == ["body", "bodyFat"]


@pytest.mark.parametrize("value", [2.9, 75.1])
async def test_body_fat_below_floor_or_above_ceiling_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "body_fat_pct": value})
    assert resp.status_code == 422


async def test_body_fat_fraction_rejected_422(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "body_fat_pct": 0.20})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [29.9, 80.1])
async def test_body_water_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "body_water_pct": value})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [9.9, 90.1])
async def test_muscle_pct_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "muscle_pct": value})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [0.4, 10.1])
async def test_bone_mass_kg_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "bone_mass_kg": value})
    assert resp.status_code == 422


async def test_bone_mass_in_grams_rejected_422(client):
    """3200 (grams) is the unit-error case the 0.5-10.0 kg bound exists for."""
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "bone_mass_kg": 3200})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [9.9, 100.1])
async def test_bmi_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "bmi": value})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [499.9, 5000.1])
async def test_bmr_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "bmr": value})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", [499.9, 10000.1])
async def test_amr_bounds_rejected_422(client, value):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "amr": value})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "field", ["weight", "body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "bmi", "bmr", "amr"],
)
async def test_boolean_value_rejected_not_silently_coerced(client, field):
    """Pydantic v2 treats bool as an int subtype, so a bare `float` field
    would otherwise silently coerce JSON true/false to 1.0/0.0.
    bone_mass_kg's 0.5-10.0 kg bound does not exclude 1.0, so
    `bone_mass_kg: true` would reach the DB and the Garmin FIT payload as a
    measured 1kg bone mass without an explicit guard (Phase 4 adversarial
    review finding). Asserts on the specific error, not just the status
    code, so this can't pass by coincidence via a field's own range bound."""
    payload = {"weight": 180.0, "unit": "lbs", field: True}
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json=payload)
    assert resp.status_code == 422
    assert "boolean" in resp.text


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field", ["weight", "body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "bmi", "bmr", "amr"],
)
async def test_non_finite_float_rejected_422_not_500(client, field, bad_value):
    """`json.dumps` accepts bare NaN/Infinity by default (a non-standard
    extension), so a bridge/scale that forwards a failed-reading NaN reaches
    Pydantic's ge/le validators, which correctly reject it -- but FastAPI's
    default RequestValidationError handler then tries to JSON-encode the
    rejected value itself (`exc.errors()`'s `input` field) and crashes with
    a 500 text/plain response instead of the documented 422, silently
    reclassifying a terminal, don't-retry error (00-design.md SS4.5 rule 2)
    into the retryable-with-backoff bucket (rule 3) -- and reintroduces the
    500/text-plain shape on /api/weight that Track A eliminated (Phase 4
    adversarial review finding).

    Sent as a raw body via `content=`, not httpx's `json=` convenience
    param -- httpx's own request-side JSON encoder rejects non-finite
    floats before a request is even sent, but stdlib `json.dumps` (what a
    real bridge/scale client would use) emits bare NaN/Infinity by default,
    so this is what actually reaches the server over the wire."""
    payload = {"weight": 180.0, "unit": "lbs", field: bad_value}
    body = json.dumps(payload)
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("weight,unit", [(1200.0, "kg"), (1.0, "kg")])
async def test_weight_above_500kg_or_below_2kg_rejected_422(client, weight, unit):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": weight, "unit": unit})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body"]
    assert "2 and 500 kg" in detail[0]["msg"]


async def test_weight_bound_applies_after_unit_conversion(client):
    """1200 lbs is ~544 kg -- the bound is on derived kg, not the raw field."""
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 1200.0, "unit": "lbs"})
    assert resp.status_code == 422


async def test_pwa_toast_flattens_array_detail():
    """F8c -- the template's error path must flatten a 422 detail array
    instead of rendering it as [object Object]."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parent.parent / "vitalforge-weight" / "templates" / "index.html"
    ).read_text()
    assert "Array.isArray(data.detail)" in template


async def test_source_literal_rejects_unknown_value_422(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs", "source": "basucle"})
    assert resp.status_code == 422


async def test_source_optional_defaults_to_null(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs"})
    assert resp.status_code == 200
    assert "source" not in resp.json()

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (await db.execute("SELECT source FROM weight_log ORDER BY id DESC LIMIT 1")).fetchone()
    finally:
        await db.close()
    assert row["source"] is None


async def test_composition_persisted_to_weight_log(client):
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json=COMPOSITION_PAYLOAD)
    assert resp.status_code == 200

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, bmi, bmr, amr, source "
                "FROM weight_log ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    finally:
        await db.close()
    assert row["body_fat_pct"] == 18.4
    assert row["body_water_pct"] == 55.2
    assert row["muscle_pct"] == 40.1
    assert row["bone_mass_kg"] == 3.2
    assert row["bmi"] == 25.3
    assert row["bmr"] == 1620.0
    assert row["amr"] == 2400.0
    assert row["source"] == "bascule"


async def test_composition_pushed_to_garmin_includes_bmi_bmr_amr(client, fake_garmin_client):
    """bmi/basal_met/active_met pass straight through to Garmin, unlike
    muscle_pct (which push_weight itself converts from a percentage to a
    mass) -- pinning the exact kwarg names/values that reach
    add_body_composition, not just that the request succeeds."""
    resp = await client.post(f"{PERSON_PREFIX}/api/weight", json=COMPOSITION_PAYLOAD)
    assert resp.status_code == 200

    pushed = fake_garmin_client.pushed_weights[-1]
    assert pushed["bmi"] == 25.3
    assert pushed["basal_met"] == 1620.0
    assert pushed["active_met"] == 2400.0


@pytest.mark.asyncio
async def test_posted_weight_always_carries_a_person_id(weight_app_module):
    from httpx import ASGITransport, AsyncClient

    from shared.database import get_db

    async with AsyncClient(transport=ASGITransport(app=weight_app_module.app), base_url="http://test") as client:
        response = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 180.0, "unit": "lbs"})
        assert response.status_code == 200

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()
