"""Middleware-level behavior for A2: the 500->401 fix (D1), the WWW-Authenticate
header, exemptions, and the /auth/login-with-a-bearer-token edge case (F7).

Uses the same throwaway app as test_auth_matrix.py for isolated assertions,
plus one real-service smoke test each for weight and dashboard (the
`shared/` blast-radius check CLAUDE.md calls for).
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient, Headers

from shared import auth as shared_auth
from shared.auth import add_auth_routes, create_session_cookie


def _build_matrix_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)

    @app.get("/api/thing")
    async def api_thing():
        return {"ok": True}

    @app.get("/page")
    async def page():
        return HTMLResponse("<html></html>")

    @app.get("/static/somefile")
    async def static_file():
        return {"static": True}

    return app


@pytest.fixture
async def matrix_client():
    transport = ASGITransport(app=_build_matrix_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def configured_auth(monkeypatch):
    """VITALFORGE_PASS set, no token -- auth is on, plain cookie auth."""
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "")


async def test_api_path_returns_401_json_not_500(configured_auth, matrix_client):
    resp = await matrix_client.get("/api/thing")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not authenticated"}


async def test_401_includes_www_authenticate_bearer(configured_auth, matrix_client):
    resp = await matrix_client.get("/api/thing")
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_401_body_does_not_echo_credentials(configured_auth, matrix_client):
    resp = await matrix_client.get(
        "/api/thing",
        headers={"Authorization": "Bearer some-presented-token"},
        cookies={"vf_session": "some-presented-cookie"},
    )
    assert resp.status_code == 401
    body_text = resp.text
    assert "some-presented-token" not in body_text
    assert "some-presented-cookie" not in body_text
    assert "some-presented-token" not in str(resp.headers)
    assert "some-presented-cookie" not in str(resp.headers)


async def test_html_path_redirects_to_login_not_401(configured_auth, matrix_client):
    resp = await matrix_client.get("/page", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login"


async def test_valid_cookie_still_works_with_token_enabled(monkeypatch, matrix_client):
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "correct-token")
    cookie = create_session_cookie("testuser")
    resp = await matrix_client.get("/api/thing", cookies={"vf_session": cookie})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "pass_value,token_value",
    [
        ("correct-pass", "correct-token"),
        ("correct-pass", ""),
        ("", "correct-token"),
        ("", ""),
    ],
)
async def test_health_exempt_in_all_four_configs(monkeypatch, matrix_client, pass_value, token_value):
    monkeypatch.setattr(shared_auth, "_PASS", pass_value)
    monkeypatch.setattr(shared_auth, "_API_TOKEN", token_value)
    # No /health stub on this app; add_auth_routes only special-cases the path,
    # it doesn't require a route to exist there. Assert it's not blocked by auth
    # (a 404 from no-route-registered is fine; 401/302 would mean auth ran).
    resp = await matrix_client.get("/health")
    assert resp.status_code not in (401, 302)


async def test_auth_and_static_paths_exempt_from_enforcement(configured_auth, matrix_client):
    login_resp = await matrix_client.get("/auth/login")
    assert login_resp.status_code == 200

    static_resp = await matrix_client.get("/static/somefile")
    assert static_resp.status_code == 200


async def test_auth_login_with_valid_bearer_redirects_to_root(monkeypatch, matrix_client):
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "correct-token")
    resp = await matrix_client.get(
        "/auth/login",
        headers={"Authorization": "Bearer correct-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


async def test_bearer_first_authorization_header_wins(monkeypatch, matrix_client):
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "correct-token")
    headers = Headers([("authorization", "Bearer JUNK"), ("authorization", "Bearer correct-token")])
    resp = await matrix_client.get("/api/thing", headers=headers)
    assert resp.status_code == 401


async def test_weight_service_api_401_shape(weight_app_module, monkeypatch):
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "")
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/weight/recent")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


async def test_dashboard_service_api_401_shape(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "")
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sync/status")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}
