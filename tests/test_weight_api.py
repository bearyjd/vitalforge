"""Tests for the vitalforge-weight service's HTTP API.

No Docker, no network, no real Garmin account — `weight_app_module` (see
conftest.py) fully fakes Garmin and points the DB at a tmp_path SQLite file.
"""

import pytest
from httpx import ASGITransport, AsyncClient


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
    resp = await client.post("/api/weight", json={"weight": 180.0, "unit": "lbs"})
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
    resp = await client.post("/api/weight", json={"weight": 81.5, "unit": "kg"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["weight_kg"] == 81.5
    assert body["weight_lbs"] == pytest.approx(81.5 * 2.20462, abs=0.01)


async def test_post_weight_invalid_unit_rejected(client):
    resp = await client.post("/api/weight", json={"weight": 150.0, "unit": "stone"})
    assert resp.status_code == 400


async def test_post_weight_garmin_failure_still_saves_locally(client, weight_app_module, monkeypatch):
    def failing_push(weight_grams, timestamp=None):
        raise RuntimeError("synthetic Garmin outage")

    monkeypatch.setattr(weight_app_module, "push_weight", failing_push)

    resp = await client.post("/api/weight", json={"weight": 170.0, "unit": "lbs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced_to_garmin"] is False
    assert "garmin_error" in body

    # The entry should still show up locally even though Garmin sync failed.
    recent = await client.get("/api/weight/recent")
    assert recent.status_code == 200
    assert len(recent.json()) == 1
    assert recent.json()[0]["synced_to_garmin"] is False


async def test_get_recent_weights_orders_newest_first(client):
    for w in (150.0, 151.0, 152.0):
        await client.post("/api/weight", json={"weight": w, "unit": "lbs"})

    resp = await client.get("/api/weight/recent")
    assert resp.status_code == 200
    weights = [row["weight_lbs"] for row in resp.json()]
    assert weights == [152.0, 151.0, 150.0]


async def test_get_weight_trend_returns_entries(client):
    await client.post("/api/weight", json={"weight": 160.0, "unit": "lbs"})
    resp = await client.get("/api/weight/trend")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["weight_lbs"] == 160.0


async def test_delete_weight_success(client):
    await client.post("/api/weight", json={"weight": 140.0, "unit": "lbs"})
    recent = await client.get("/api/weight/recent")
    entry_id = recent.json()[0]["id"]

    del_resp = await client.delete(f"/api/weight/{entry_id}")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"success": True, "deleted_id": entry_id}

    recent_after = await client.get("/api/weight/recent")
    assert recent_after.json() == []


async def test_delete_weight_missing_returns_404(client):
    resp = await client.delete("/api/weight/999999")
    assert resp.status_code == 404
