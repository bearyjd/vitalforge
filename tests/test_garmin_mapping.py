"""B3: mapping body-composition fields onto shared.garmin_client.push_weight's
new keyword-only parameters (docs/prp/00-design.md SS3.4). No Docker, no
network, no real Garmin account.
"""

import logging
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared import garmin_client


@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def test_real_client_signature_accepts_our_kwargs():
    """F5, the highest-value test in this package: verifies something we do
    not control. A **kwargs fake accepts every name, so without this an
    upstream garminconnect rename leaves every other mapping test here green
    while production silently stops recording body fat."""
    import inspect

    import garminconnect

    params = set(inspect.signature(garminconnect.Garmin.add_body_composition).parameters)
    assert params >= {"percent_fat", "percent_hydration", "bone_mass", "muscle_mass"}


def test_fake_client_captures_composition_kwargs(fake_garmin_client):
    garmin_client.push_weight(81600, percent_fat=18.4)
    assert fake_garmin_client.pushed_weights[-1]["percent_fat"] == 18.4


def test_body_fat_maps_to_percent_fat(fake_garmin_client):
    garmin_client.push_weight(81600, percent_fat=18.4)
    assert fake_garmin_client.pushed_weights[-1]["percent_fat"] == 18.4


def test_body_water_maps_to_percent_hydration(fake_garmin_client):
    garmin_client.push_weight(81600, percent_hydration=55.2)
    assert fake_garmin_client.pushed_weights[-1]["percent_hydration"] == 55.2


def test_bone_mass_kg_passes_through_unconverted(fake_garmin_client):
    garmin_client.push_weight(81600, bone_mass_kg=3.2)
    assert fake_garmin_client.pushed_weights[-1]["bone_mass"] == 3.2


def test_omitted_composition_passed_as_none(fake_garmin_client):
    garmin_client.push_weight(81600)
    pushed = fake_garmin_client.pushed_weights[-1]
    assert pushed["percent_fat"] is None
    assert pushed["percent_hydration"] is None
    assert pushed["muscle_mass"] is None
    assert pushed["bone_mass"] is None


def test_push_weight_positional_call_still_works(fake_garmin_client):
    garmin_client.push_weight(81600, datetime.now(timezone.utc))
    assert len(fake_garmin_client.pushed_weights) == 1


def test_no_composition_values_in_log_output(fake_garmin_client, caplog):
    with caplog.at_level(logging.INFO, logger=garmin_client.__name__):
        garmin_client.push_weight(81600, percent_fat=18.4, percent_hydration=55.2, bone_mass_kg=3.2)
    assert "18.4" not in caplog.text
    assert "55.2" not in caplog.text
    assert "3.2" not in caplog.text


async def test_muscle_pct_derives_muscle_mass_kg(client, weight_app_module, fake_garmin_client, monkeypatch):
    # weight_app_module fakes push_weight wholesale by default (for tests that
    # don't care about Garmin-side mapping). Restore the real function here so
    # the route's muscle_mass_kg derivation reaches push_weight's own kwarg
    # mapping instead of being bypassed by the double.
    monkeypatch.setattr(weight_app_module, "push_weight", garmin_client.push_weight)

    resp = await client.post("/api/weight", json={"weight": 100.0, "unit": "kg", "muscle_pct": 40.0})
    assert resp.status_code == 200
    assert fake_garmin_client.pushed_weights[-1]["muscle_mass"] == pytest.approx(40.0)


async def test_weight_only_push_sends_no_composition_values(client, fake_garmin_client):
    resp = await client.post("/api/weight", json={"weight": 180.0, "unit": "lbs"})
    assert resp.status_code == 200
    pushed = fake_garmin_client.pushed_weights[-1]
    assert pushed.get("percent_fat") is None
    assert pushed.get("percent_hydration") is None
    assert pushed.get("muscle_mass") is None
    assert pushed.get("bone_mass") is None


async def test_garmin_failure_still_stores_composition_locally(client, weight_app_module, monkeypatch):
    def failing_push(weight_grams, timestamp=None, **kwargs):
        raise RuntimeError("synthetic Garmin outage")

    monkeypatch.setattr(weight_app_module, "push_weight", failing_push)

    resp = await client.post(
        "/api/weight",
        json={"weight": 180.0, "unit": "lbs", "body_fat_pct": 18.4, "bone_mass_kg": 3.2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced_to_garmin"] is False
    assert body["body_fat_pct"] == 18.4
    assert body["bone_mass_kg"] == 3.2
