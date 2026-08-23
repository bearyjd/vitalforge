"""Phase 4 end-to-end scenario tests (docs/prp/vitalforge-agent-prompt.md
Phase 4 SS1) -- chains that cross layers in one test.

Unit-level auth, dedup, Garmin-mapping, and weight-validation behavior
already have exhaustive dedicated coverage in test_auth_matrix.py,
test_auth_middleware.py, test_weight_api.py, test_dedup.py,
test_dashboard_api.py, and test_garmin_mapping.py -- this file does not
re-assert any of that.
"""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared import auth as shared_auth
from shared.auth import create_session_cookie
from tests.conftest import import_service_module, seed_user

FULL_PAYLOAD = {
    "weight": 180.0,
    "unit": "lbs",
    "body_fat_pct": 18.4,
    "body_water_pct": 55.2,
    "muscle_pct": 40.1,
    "bone_mass_kg": 3.2,
    "source": "bascule",
}

TOKEN = "secret-token"


async def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_auth, "_API_TOKEN", TOKEN)
    await seed_user("testuser")


async def test_token_client_full_flow(weight_app_module, monkeypatch):
    await _configure_auth(monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        post_resp = await ac.post("/api/weight", json=FULL_PAYLOAD, headers=headers)
        assert post_resp.status_code == 200
        post_body = post_resp.json()
        assert post_body["success"] is True
        assert post_body["body_fat_pct"] == 18.4
        assert post_body["source"] == "bascule"

        recent_resp = await ac.get("/api/weight/recent", headers=headers)
    assert recent_resp.status_code == 200
    recent = recent_resp.json()
    assert len(recent) == 1
    assert recent[0]["weight_lbs"] == 180.0


async def test_cookie_client_regression_flow(weight_app_module, monkeypatch):
    """A2 regression: cookie auth stays unaffected once a bearer token is
    configured. Cookies are set at client construction, not per-request --
    httpx deprecates (and eventually hard-errors on) per-request `cookies=`
    persisting into the client jar, so relying on that would be pinned to
    ambiguous, disappearing behavior rather than modeling a real session."""
    await _configure_auth(monkeypatch)
    cookies = {"vf_session": create_session_cookie("testuser")}
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as ac:
        post_resp = await ac.post("/api/weight", json=FULL_PAYLOAD)
        assert post_resp.status_code == 200
        assert post_resp.json()["success"] is True

        recent_resp = await ac.get("/api/weight/recent")
    assert recent_resp.status_code == 200
    recent = recent_resp.json()
    assert len(recent) == 1
    assert recent[0]["weight_lbs"] == 180.0


async def test_mixed_clients_interleaved_no_auth_leakage(weight_app_module, monkeypatch):
    """Each request's outcome must depend only on its own credentials. Uses
    three genuinely distinct AsyncClient instances (own connection, own
    cookie jar) against the same app/DB, rather than one shared client with
    per-request `cookies=` overrides -- the latter only "proves" no leakage
    because httpx doesn't persist per-request cookies into the jar (a
    deprecated, ambiguous behavior slated to change), not because VitalForge
    itself keeps no cross-client state."""
    await _configure_auth(monkeypatch)
    token_headers = {"Authorization": f"Bearer {TOKEN}"}
    cookies = {"vf_session": create_session_cookie("testuser")}
    transport = ASGITransport(app=weight_app_module.app)

    async with (
        AsyncClient(transport=transport, base_url="http://test", headers=token_headers) as token_client,
        AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as cookie_client,
        AsyncClient(transport=transport, base_url="http://test") as anon_client,
    ):
        token_post = await token_client.post("/api/weight", json={"weight": 170.0, "unit": "lbs"})
        assert token_post.status_code == 200

        cookie_get = await cookie_client.get("/api/weight/recent")
        assert cookie_get.status_code == 200
        assert len(cookie_get.json()) == 1

        anon_get = await anon_client.get("/api/weight/recent")
        assert anon_get.status_code == 401

        token_get = await token_client.get("/api/weight/recent")
        assert token_get.status_code == 200
        assert len(token_get.json()) == 1  # unaffected by the intervening 401 on a distinct client

        cookie_post = await cookie_client.post("/api/weight", json={"weight": 171.0, "unit": "lbs"})
        assert cookie_post.status_code == 200

        final_get = await token_client.get("/api/weight/recent")
    assert final_get.status_code == 200
    assert len(final_get.json()) == 2


def _weigh_ins_echoing_push(pushed: dict) -> dict:
    """Build a `get_weigh_ins`-shaped response FROM what the weight service
    actually pushed to (fake) Garmin, instead of returning the static
    `weigh_ins.json` fixture. Without this, the dashboard half of the chain
    test below reads fixture-sourced values with no relationship to the POST
    above it -- passing regardless of what the weight service actually
    computed, which defeats the point of an end-to-end chain test (Phase 4
    review finding)."""
    ts: datetime = pushed["timestamp"]
    bone_mass_kg = pushed.get("bone_mass_kg")
    muscle_mass_kg = pushed.get("muscle_mass_kg")
    return {
        "dailyWeightSummaries": [
            {
                "summaryDate": ts.strftime("%Y-%m-%d"),
                "latestWeight": {
                    "weight": pushed["weight_grams"],
                    "bodyFat": pushed.get("percent_fat"),
                    "bodyWater": pushed.get("percent_hydration"),
                    "boneMass": round(bone_mass_kg * 1000) if bone_mass_kg is not None else None,
                    "muscleMass": round(muscle_mass_kg * 1000) if muscle_mass_kg is not None else None,
                },
            }
        ]
    }


async def test_full_composition_chain_and_duplicate_collapse(
    weight_app_module, dashboard_app_module, fake_garmin_client, monkeypatch
):
    """Auth disabled (default VITALFORGE_PASS unset) -- isolates data-flow
    from the auth scenarios above. weight_app_module and dashboard_app_module
    both resolve to the same tmp_db_path/initialized_db/fake_garmin_client
    here (function-scoped fixtures), so they share one DB.

    weight=80kg (not 100kg): at 100kg, weight_kg * muscle_pct/100 is
    numerically the identity map on the percent value, so a 100kg fixture
    can't distinguish "converted to a mass" from "passed through raw" (Phase
    4 review finding, also fixed in test_garmin_mapping.py)."""
    payload = {
        "weight": 80.0,
        "unit": "kg",
        "body_fat_pct": 18.4,
        "body_water_pct": 55.2,
        "muscle_pct": 40.0,
        "bone_mass_kg": 3.2,
    }
    weight_transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=weight_transport, base_url="http://test") as wc:
        first = await wc.post("/api/weight", json=payload)
        assert first.status_code == 200
        assert first.json()["synced_to_garmin"] is True

        pushed = fake_garmin_client.pushed_weights[-1]
        assert pushed["percent_fat"] == 18.4
        assert pushed["percent_hydration"] == 55.2
        assert pushed["muscle_mass_kg"] == pytest.approx(32.0)  # 80kg * 40% -- distinguishable from a raw 40.0
        assert pushed["bone_mass_kg"] == 3.2

        first_id = (await wc.get("/api/weight/recent")).json()[0]["id"]

        # B4 dedup, folded in here rather than re-testing test_dedup.py's
        # suite: an identical POST inside the window must collapse, not
        # double-push.
        second = await wc.post("/api/weight", json=payload)
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["deduplicated"] is True
        assert second_body["id"] == first_id
        assert len(fake_garmin_client.pushed_weights) == 1

        recent = await wc.get("/api/weight/recent")
    assert len(recent.json()) == 1

    # Dashboard sync pulls from the same fake Garmin client. Echo back what
    # was actually pushed above (see _weigh_ins_echoing_push) so the chain is
    # real end to end, rather than the static fixture (which is pinned at a
    # fixed 2020-06-01 by test_sync.py and carries unrelated values).
    sync = import_service_module("vitalforge-dashboard.sync")
    sync_date = pushed["timestamp"].astimezone(timezone.utc).date().isoformat()
    monkeypatch.setattr(
        fake_garmin_client, "get_weigh_ins", lambda start, end: _weigh_ins_echoing_push(pushed)
    )
    await sync.sync_weight_history(sync_date, sync_date)

    dashboard_transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=dashboard_transport, base_url="http://test") as dc:
        bone_resp = await dc.get("/api/metrics/bone_mass")
        muscle_resp = await dc.get("/api/metrics/muscle_mass")

    assert bone_resp.status_code == 200
    bone_body = bone_resp.json()
    assert bone_body["count"] == 1
    assert bone_body["data"][0]["value"] == 3200  # 3.2kg pushed above, echoed back and synced

    assert muscle_resp.status_code == 200
    muscle_body = muscle_resp.json()
    assert muscle_body["count"] == 1
    assert muscle_body["data"][0]["value"] == 32000  # 32.0kg derived above, echoed back and synced


# Scenario 6 (out-of-range body fat -> 422) is already exhaustively covered,
# with no cross-layer element, by test_weight_api.py's
# test_body_fat_below_floor_or_above_ceiling_rejected_422 and its siblings.
