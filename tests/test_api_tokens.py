"""Per-user DB-backed API token behavior and legacy migration."""

import asyncio
import hashlib
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shared import auth as shared_auth
from shared.auth import add_auth_routes, bootstrap_migrated_token, create_session_cookie
from shared.database import get_db
from tests.conftest import seed_token, seed_user


def _build_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)
    return app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _cookies_for(username: str) -> dict[str, str]:
    user_id, session_version = await shared_auth._get_user_id_and_session_version(username)
    return {"vf_session": create_session_cookie(username, user_id, session_version)}


async def _token_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM api_tokens")).fetchone())[0]
    finally:
        await db.close()


async def test_fresh_schema_contains_api_token_and_migration_tables(initialized_db):
    db = await get_db()
    try:
        token_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(api_tokens)")).fetchall()
        }
        migration_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(auth_migrations)")).fetchall()
        }
    finally:
        await db.close()
    assert {"id", "user_id", "label", "token_hash", "created_at", "last_used_at"} <= token_columns
    assert {"name", "completed_at"} <= migration_columns


async def test_create_token_returns_raw_once_and_stores_only_hash(client):
    user_id = await seed_user("alice", password="correct-password")
    resp = await client.post(
        "/auth/tokens",
        json={"label": "  Bascule  ", "current_password": "correct-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    raw = resp.json()["token"]
    assert resp.json()["label"] == "Bascule"
    assert len(raw) > 20

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT user_id, label, token_hash FROM api_tokens")
        ).fetchone()
    finally:
        await db.close()
    assert row["user_id"] == user_id
    assert row["label"] == "Bascule"
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in tuple(row)

    listed = await client.get("/auth/tokens", cookies=await _cookies_for("alice"))
    assert listed.status_code == 200
    assert listed.json()[0]["label"] == "Bascule"
    assert "token" not in listed.json()[0]
    assert "token_hash" not in listed.json()[0]


async def test_create_token_cannot_orphan_credential_if_account_disappears(
    client, monkeypatch
):
    await seed_user("alice", password="correct-password")
    original_step_up = shared_auth._require_step_up

    async def verify_then_delete(identity, current_password):
        await original_step_up(identity, current_password)
        db = await get_db()
        try:
            await db.execute("DELETE FROM users WHERE id = ?", (identity.user_id,))
            await db.commit()
        finally:
            await db.close()

    monkeypatch.setattr(shared_auth, "_require_step_up", verify_then_delete)
    resp = await client.post(
        "/auth/tokens",
        json={"label": "Tasker", "current_password": "correct-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 401
    assert await _token_count() == 0


async def test_create_token_requires_correct_current_password(client):
    await seed_user("alice", password="correct-password")
    resp = await client.post(
        "/auth/tokens",
        json={"label": "Tasker", "current_password": "wrong-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 401
    assert await _token_count() == 0


async def test_create_token_rejects_blank_label(client):
    await seed_user("alice", password="correct-password")
    resp = await client.post(
        "/auth/tokens",
        json={"label": "   ", "current_password": "correct-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 422
    assert await _token_count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"label": 123, "current_password": "correct-password"},
        {"label": "Tasker", "current_password": "correct-password", "extra": True},
    ],
)
async def test_create_token_malformed_payload_returns_422(client, payload):
    await seed_user("alice", password="correct-password")
    resp = await client.post("/auth/tokens", json=payload, cookies=await _cookies_for("alice"))
    assert resp.status_code == 422


async def test_users_only_list_their_own_tokens(client):
    alice_id = await seed_user("alice")
    bob_id = await seed_user("bob")
    await seed_token(alice_id, label="alice-token")
    await seed_token(bob_id, label="bob-token")

    resp = await client.get("/auth/tokens", cookies=await _cookies_for("alice"))
    assert resp.status_code == 200
    assert [row["label"] for row in resp.json()] == ["alice-token"]


async def test_owner_can_revoke_token_with_step_up_password(client):
    user_id = await seed_user("alice", password="correct-password")
    token_id, _ = await seed_token(user_id)
    resp = await client.post(
        f"/auth/tokens/{token_id}/revoke",
        json={"current_password": "correct-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 200
    assert await _token_count() == 0


async def test_wrong_step_up_password_does_not_revoke_token(client):
    user_id = await seed_user("alice", password="correct-password")
    token_id, _ = await seed_token(user_id)
    resp = await client.post(
        f"/auth/tokens/{token_id}/revoke",
        json={"current_password": "wrong-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 401
    assert await _token_count() == 1


async def test_non_admin_cannot_revoke_another_users_token(client):
    await seed_user("alice", password="alice-password")
    bob_id = await seed_user("bob")
    token_id, _ = await seed_token(bob_id)
    resp = await client.post(
        f"/auth/tokens/{token_id}/revoke",
        json={"current_password": "alice-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 403
    assert await _token_count() == 1


async def test_admin_can_revoke_another_users_token(client):
    await seed_user("root", password="root-password", role="admin")
    bob_id = await seed_user("bob")
    token_id, _ = await seed_token(bob_id)
    resp = await client.post(
        f"/auth/tokens/{token_id}/revoke",
        json={"current_password": "root-password"},
        cookies=await _cookies_for("root"),
    )
    assert resp.status_code == 200
    assert await _token_count() == 0


async def test_revoke_nonexistent_token_returns_404(client):
    await seed_user("alice", password="correct-password")
    resp = await client.post(
        "/auth/tokens/999999/revoke",
        json={"current_password": "correct-password"},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 404


async def test_admin_token_list_includes_every_owner(client):
    root_id = await seed_user("root", role="admin")
    bob_id = await seed_user("bob")
    await seed_token(root_id, label="root-token")
    await seed_token(bob_id, label="bob-token")

    resp = await client.get("/auth/admin/tokens", cookies=await _cookies_for("root"))
    assert resp.status_code == 200
    assert [(row["owner"], row["label"]) for row in resp.json()] == [
        ("bob", "bob-token"),
        ("root", "root-token"),
    ]
    assert all("token_hash" not in row for row in resp.json())


async def test_non_admin_cannot_list_all_tokens(client):
    await seed_user("alice")
    resp = await client.get("/auth/admin/tokens", cookies=await _cookies_for("alice"))
    assert resp.status_code == 403


@pytest.mark.parametrize("role,expected", [("user", 403), ("admin", 200)])
async def test_bearer_token_inherits_owners_live_role(client, role, expected):
    user_id = await seed_user("token-owner", role=role)
    _, raw = await seed_token(user_id, raw_token=f"{role}-token")
    resp = await client.get(
        "/auth/admin/users/list", headers={"Authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == expected


async def test_bearer_use_updates_last_used_at(client):
    user_id = await seed_user("token-owner")
    token_id, raw = await seed_token(user_id)
    resp = await client.get("/auth/account", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    db = await get_db()
    try:
        last_used = (
            await (await db.execute("SELECT last_used_at FROM api_tokens WHERE id = ?", (token_id,))).fetchone()
        )[0]
    finally:
        await db.close()
    assert last_used is not None


async def test_deleting_user_removes_their_tokens(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob")
    await seed_token(bob_id)
    resp = await client.delete(
        f"/auth/admin/users/{bob_id}", cookies=await _cookies_for("root")
    )
    assert resp.status_code == 200
    assert await _token_count() == 0


async def test_legacy_token_migration_is_idempotent_and_hash_only(initialized_db, monkeypatch, caplog):
    admin_id = await seed_user("root", role="admin")
    legacy = "legacy-secret-token-value"
    monkeypatch.setenv("VITALFORGE_API_TOKEN", legacy)

    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        await bootstrap_migrated_token()
        await bootstrap_migrated_token()

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT user_id, label, token_hash FROM api_tokens")
        ).fetchone()
        markers = (await (await db.execute("SELECT COUNT(*) FROM auth_migrations")).fetchone())[0]
    finally:
        await db.close()
    assert await _token_count() == 1
    assert row["user_id"] == admin_id
    assert row["label"] == "migrated-from-env"
    assert row["token_hash"] == hashlib.sha256(legacy.encode()).hexdigest()
    assert markers == 1
    assert legacy not in caplog.text


async def test_legacy_token_migration_is_safe_under_concurrent_service_startup(
    initialized_db, monkeypatch
):
    await seed_user("root", role="admin")
    monkeypatch.setenv("VITALFORGE_API_TOKEN", "legacy-token")
    results = await asyncio.gather(
        bootstrap_migrated_token(), bootstrap_migrated_token(), return_exceptions=True
    )
    assert results == [None, None]
    assert await _token_count() == 1


async def test_legacy_token_migration_waits_for_an_admin(initialized_db, monkeypatch, caplog):
    monkeypatch.setenv("VITALFORGE_API_TOKEN", "legacy-token")
    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        await bootstrap_migrated_token()
    assert await _token_count() == 0
    assert "no admin account exists" in caplog.text


async def test_revoked_migrated_token_is_not_resurrected_on_restart(initialized_db, monkeypatch):
    await seed_user("root", role="admin")
    monkeypatch.setenv("VITALFORGE_API_TOKEN", "legacy-token")
    await bootstrap_migrated_token()
    db = await get_db()
    try:
        await db.execute("DELETE FROM api_tokens")
        await db.commit()
    finally:
        await db.close()

    await bootstrap_migrated_token()
    assert await _token_count() == 0
