"""Authorization and redaction contract for shared Garmin lifecycle routes."""

import ast
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from shared import auth, garmin_registry, garmin_routes
from shared.auth import create_session_cookie
from shared.auth_routes import add_auth_routes
from shared.database import get_db
from tests.conftest import grant_person, primary_person_id, seed_token, seed_user
from tests.live_server import LiveServer


def _build_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)
    garmin_routes.add_garmin_routes(app)
    return app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        yield ac


@pytest.fixture
async def insecure_client(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _manager_cookie(username: str = "alice") -> tuple[int, dict[str, str]]:
    user_id = await seed_user(username, password="local-password")
    person_id = await primary_person_id()
    await grant_person(person_id, user_id, "manage")
    return user_id, {"vf_session": create_session_cookie(username, user_id, 1)}


async def test_status_is_cookie_only_manage_and_redacts_link_metadata(client):
    user_id, cookies = await _manager_cookie()
    person_id = await primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO garmin_links "
            "(person_id, state, garmin_email, generation, linked_at, linked_by, updated_at, last_auth_ok) "
            "VALUES (?, 'linked', 'alice@example.test', 7, ?, ?, ?, ?)",
            (person_id, now, user_id, now, now),
        )
        await db.commit()
    finally:
        await db.close()

    response = await client.get("/p/primary/api/garmin/status", cookies=cookies)
    assert response.status_code == 200
    assert response.json() == {
        "linked": True,
        "last_auth_ok": now,
        "last_auth_error": None,
        "last_auth_error_at": None,
    }
    assert "alice@example.test" not in response.text
    assert "generation" not in response.text
    assert "linked_by" not in response.text


async def test_status_rejects_bearer_anonymous_and_view_only_callers_without_enumeration(client):
    user_id = await seed_user("manager")
    person_id = await primary_person_id()
    await grant_person(person_id, user_id, "manage")
    _, raw_token = await seed_token(user_id, raw_token="manager-token")

    bearer = await client.get(
        "/p/primary/api/garmin/status", headers={"Authorization": f"Bearer {raw_token}"}
    )
    assert bearer.status_code == 404

    viewer_id = await seed_user("viewer")
    await grant_person(person_id, viewer_id, "view")
    viewer = await client.get(
        "/p/primary/api/garmin/status",
        cookies={"vf_session": create_session_cookie("viewer", viewer_id, 1)},
    )
    assert viewer.status_code == 404


async def test_status_returns_404_in_anonymous_development_mode(client):
    response = await client.get("/p/primary/api/garmin/status")
    assert response.status_code == 404
    assert response.json() == {"detail": "Person not found"}


@pytest.mark.parametrize("path", [
    "/p/primary/api/garmin/link",
    "/p/primary/api/garmin/relink",
    "/p/primary/api/garmin/unlink",
])
async def test_credential_changes_reject_plain_http(insecure_client, path):
    _, cookies = await _manager_cookie()
    payload = (
        {"email": "person@example.test", "password": "garmin-password-secret", "current_password": "local-password"}
        if path.endswith(("link", "relink")) and not path.endswith("unlink")
        else {"current_password": "local-password"}
    )

    response = await insecure_client.post(path, cookies=cookies, json=payload)

    assert response.status_code == 400
    assert response.json() == {"detail": "Garmin credential changes require HTTPS"}
    assert "garmin-password-secret" not in response.text
    assert "local-password" not in response.text


async def test_credential_changes_reject_spoofed_forwarded_https(insecure_client):
    _, cookies = await _manager_cookie()

    response = await insecure_client.post(
        "/p/primary/api/garmin/link",
        cookies=cookies,
        headers={"X-Forwarded-Proto": "https"},
        json={"email": "person@example.test", "password": "garmin-password-secret", "current_password": "local-password"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Garmin credential changes require HTTPS"}


async def test_configured_proxy_https_allows_credential_change(insecure_client, monkeypatch):
    user_id, cookies = await _manager_cookie()
    person_id = await primary_person_id()
    calls = []
    monkeypatch.setenv("VITALFORGE_TRUSTED_PROXY_IPS", "127.0.0.1")

    async def step_up(identity, password):
        calls.append((identity.user_id, password))

    async def link(*args):
        calls.append(args)
        return garmin_registry.GarminLink(person_id, 1, "linked")

    async def status(_person_id):
        return {"linked": True, "last_auth_ok": None, "last_auth_error": None, "last_auth_error_at": None}

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, "link", link)
    monkeypatch.setattr(garmin_routes, "_status_for_person", status)

    response = await insecure_client.post(
        "/p/primary/api/garmin/link",
        cookies=cookies,
        headers={"X-Forwarded-Proto": "https"},
        json={"email": "person@example.test", "password": "garmin-password-secret", "current_password": "local-password"},
    )

    assert response.status_code == 200
    assert calls == [
        (user_id, "local-password"),
        (person_id, user_id, 1, "person@example.test", "garmin-password-secret"),
    ]


def test_real_uvicorn_preserves_route_level_proxy_trust_boundary(monkeypatch):
    """A loopback peer cannot spoof HTTPS before the credential gate runs."""
    app = FastAPI()

    @app.post("/credential", dependencies=[Depends(garmin_routes.require_secure_garmin_credential_transport)])
    async def credential_change():
        return {"ok": True}

    server = LiveServer(app)
    server.start()
    try:
        spoofed = httpx.post(
            f"{server.base_url}/credential",
            headers={"X-Forwarded-Proto": "https"},
            timeout=2,
        )
        assert spoofed.status_code == 400

        monkeypatch.setenv("VITALFORGE_TRUSTED_PROXY_IPS", "127.0.0.1")
        trusted = httpx.post(
            f"{server.base_url}/credential",
            headers={"X-Forwarded-Proto": "https"},
            timeout=2,
        )
        assert trusted.status_code == 200
    finally:
        server.stop()


async def test_explicit_development_override_allows_plain_http(insecure_client, monkeypatch):
    user_id, cookies = await _manager_cookie()
    person_id = await primary_person_id()
    calls = []
    monkeypatch.setenv("VITALFORGE_ALLOW_INSECURE_GARMIN_LINKS", "1")

    async def step_up(identity, password):
        calls.append((identity.user_id, password))

    async def link(*args):
        calls.append(args)
        return garmin_registry.GarminLink(person_id, 1, "linked")

    async def status(_person_id):
        return {"linked": True, "last_auth_ok": None, "last_auth_error": None, "last_auth_error_at": None}

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, "link", link)
    monkeypatch.setattr(garmin_routes, "_status_for_person", status)

    response = await insecure_client.post(
        "/p/primary/api/garmin/link",
        cookies=cookies,
        json={"email": "person@example.test", "password": "garmin-password-secret", "current_password": "local-password"},
    )

    assert response.status_code == 200
    assert calls == [
        (user_id, "local-password"),
        (person_id, user_id, 1, "person@example.test", "garmin-password-secret"),
    ]


@pytest.mark.parametrize(
    ("path", "operation_name"),
    [
        ("/p/primary/api/garmin/link", "link"),
        ("/p/primary/api/garmin/relink", "relink"),
    ],
)
async def test_link_routes_step_up_and_keep_credentials_out_of_response(
    client, monkeypatch, path, operation_name
):
    user_id, cookies = await _manager_cookie()
    person_id = await primary_person_id()
    calls = []

    async def step_up(identity, password):
        calls.append(("step_up", identity.user_id, password))

    async def operation(*args):
        calls.append((operation_name, *args))
        return garmin_registry.GarminLink(person_id, 1, "linked")

    async def redacted_status(person):
        assert person == person_id
        return {"linked": True, "last_auth_ok": "safe-time", "last_auth_error": None, "last_auth_error_at": None}

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, operation_name, operation)
    monkeypatch.setattr(garmin_routes, "_status_for_person", redacted_status)
    response = await client.post(
        path,
        cookies=cookies,
        json={
            "email": "person@example.test",
            "password": "garmin-password-secret",
            "current_password": "local-password",
        },
    )

    assert response.status_code == 200
    assert response.json()["linked"] is True
    assert calls == [
        ("step_up", user_id, "local-password"),
        (operation_name, person_id, user_id, 1, "person@example.test", "garmin-password-secret"),
    ]
    assert "person@example.test" not in response.text
    assert "garmin-password-secret" not in response.text
    assert "local-password" not in response.text


async def test_unlink_step_up_passes_exact_cookie_identity_to_registry(client, monkeypatch):
    user_id, cookies = await _manager_cookie()
    person_id = await primary_person_id()
    calls = []

    async def step_up(identity, password):
        calls.append(("step_up", identity.user_id, password))

    async def unlink(*args):
        calls.append(("unlink", *args))
        return True

    async def redacted_status(person):
        assert person == person_id
        return {"linked": False, "last_auth_ok": None, "last_auth_error": None, "last_auth_error_at": None}

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, "unlink", unlink)
    monkeypatch.setattr(garmin_routes, "_status_for_person", redacted_status)
    response = await client.post(
        "/p/primary/api/garmin/unlink",
        cookies=cookies,
        json={"current_password": "local-password"},
    )

    assert response.status_code == 200
    assert calls == [("step_up", user_id, "local-password"), ("unlink", person_id, user_id, 1)]


async def test_registry_exception_text_cannot_echo_credentials(client, monkeypatch):
    _, cookies = await _manager_cookie()

    async def step_up(identity, password):
        pass

    async def leak_if_rendered(*args):
        raise garmin_registry.GarminRegistryError("person@example.test garmin-password-secret")

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, "link", leak_if_rendered)
    response = await client.post(
        "/p/primary/api/garmin/link",
        cookies=cookies,
        json={
            "email": "person@example.test",
            "password": "garmin-password-secret",
            "current_password": "local-password",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Garmin operation failed"}
    assert "person@example.test" not in response.text
    assert "garmin-password-secret" not in response.text


@pytest.mark.parametrize(
    ("payload", "secret_sentinel"),
    [
        (
            {
                "email": "person@example.test",
                "password": ["garmin-password-list-sentinel"],
                "current_password": "local-password",
            },
            "garmin-password-list-sentinel",
        ),
        (
            {
                "email": "person@example.test",
                "password": "garmin-password-secret",
                "current_password": "local-password",
                "unexpected": "extra-field-sentinel",
            },
            "extra-field-sentinel",
        ),
        (
            {
                "email": ["email-sentinel@example.test"],
                "password": "garmin-password-secret",
                "current_password": "local-password",
            },
            "email-sentinel@example.test",
        ),
    ],
)
async def test_credential_validation_never_reflects_rejected_secret_input(client, payload, secret_sentinel):
    _, cookies = await _manager_cookie()
    response = await client.post("/p/primary/api/garmin/link", cookies=cookies, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid Garmin link request"}
    assert secret_sentinel not in response.text


async def test_missing_target_from_registry_is_a_non_enumerating_404(client, monkeypatch):
    _, cookies = await _manager_cookie()

    async def step_up(identity, password):
        pass

    async def target_deleted(*args):
        raise garmin_registry.GarminNotLinked(1)

    monkeypatch.setattr(auth, "_require_step_up", step_up)
    monkeypatch.setattr(garmin_registry, "link", target_deleted)
    response = await client.post(
        "/p/primary/api/garmin/link",
        cookies=cookies,
        json={
            "email": "person@example.test",
            "password": "garmin-password-secret",
            "current_password": "local-password",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Person not found"}


def test_both_apps_register_the_shared_garmin_route_surface():
    """Both service factories install the shared surface on their app object.

    Importing the full dashboard app here would require the optional FIT
    parser, which this focused auth-route suite intentionally does not need.
    Structural inspection still pins the registration call and the app object
    passed to it in both services.
    """
    root = Path(__file__).resolve().parent.parent
    for relative in ("vitalforge_weight/app.py", "vitalforge_dashboard/app.py"):
        tree = ast.parse((root / relative).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "add_garmin_routes"
        ]
        assert any(
            len(call.args) == 1 and isinstance(call.args[0], ast.Name) and call.args[0].id == "app"
            for call in calls
        ), f"{relative} does not register Garmin lifecycle routes on app"


def test_service_launchers_disable_uvicorn_implicit_proxy_headers():
    """Credential-route proxy trust must not be bypassed before FastAPI runs.

    Uvicorn enables proxy-header rewriting by default. If a launcher leaves
    that default in place, a request from an address Uvicorn happens to trust
    can arrive at ``require_secure_garmin_credential_transport`` already
    rewritten as HTTPS, bypassing its explicit
    ``VITALFORGE_TRUSTED_PROXY_IPS`` check.
    """
    root = Path(__file__).resolve().parent.parent
    for relative in ("vitalforge_weight/Dockerfile", "vitalforge_dashboard/Dockerfile"):
        assert "--no-proxy-headers" in (root / relative).read_text(), relative
