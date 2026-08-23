"""User accounts & auth model (`.claude/PRPs/plans/user-accounts-auth-model.plan.md`):
password change, admin user CRUD, the last-admin-deletion/demotion guard, and
`bootstrap_first_admin`'s idempotency.

Uses the same throwaway-app pattern as test_auth_matrix.py/test_auth_middleware.py
so these routes are tested in isolation from either real service.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shared import auth as shared_auth
from shared.auth import add_auth_routes, create_session_cookie
from shared.database import get_db
from tests.conftest import seed_user


def _build_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)
    return app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _cookies_for(username: str) -> dict:
    return {"vf_session": create_session_cookie(username)}


# --- Self-service password change -------------------------------------------------


async def test_change_own_password_requires_correct_current_password(client):
    await seed_user("alice", password="old-password")
    resp = await client.post(
        "/auth/account/password",
        json={"current_password": "wrong-password", "new_password": "new-password"},
        cookies=_cookies_for("alice"),
    )
    assert resp.status_code == 401

    # Unchanged: the old password still works, the "new" one doesn't.
    assert await shared_auth.check_credentials("alice", "old-password") is True
    assert await shared_auth.check_credentials("alice", "new-password") is False


async def test_change_own_password_succeeds_with_correct_current_password(client):
    await seed_user("alice", password="old-password")
    resp = await client.post(
        "/auth/account/password",
        json={"current_password": "old-password", "new_password": "new-password"},
        cookies=_cookies_for("alice"),
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("alice", "old-password") is False
    assert await shared_auth.check_credentials("alice", "new-password") is True


async def test_change_own_password_requires_auth(client):
    await seed_user("someone")  # turn auth on -- an empty users table means open access
    resp = await client.post(
        "/auth/account/password", json={"current_password": "x", "new_password": "y"}
    )
    assert resp.status_code == 401


# --- Admin route access ------------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected_status",
    [("user", 403), ("admin", 200)],
    ids=["user-role-blocked", "admin-role-allowed"],
)
async def test_admin_users_list_access_by_role(client, role, expected_status):
    await seed_user("someone", role=role)
    resp = await client.get("/auth/admin/users/list", cookies=_cookies_for("someone"))
    assert resp.status_code == expected_status


async def test_admin_users_list_requires_auth(client):
    await seed_user("root", role="admin")  # turn auth on -- an empty users table means open access
    resp = await client.get("/auth/admin/users/list")
    assert resp.status_code == 401


# --- Admin: create user -------------------------------------------------------------


async def test_admin_can_create_user(client):
    await seed_user("root", role="admin")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "bobs-password", "role": "user"},
        cookies=_cookies_for("root"),
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("bob", "bobs-password") is True
    assert await shared_auth.get_current_user_role("bob") == "user"


async def test_non_admin_cannot_create_user(client):
    await seed_user("someone", role="user")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "x", "role": "user"},
        cookies=_cookies_for("someone"),
    )
    assert resp.status_code == 403


async def test_create_user_duplicate_username_rejected(client):
    await seed_user("root", role="admin")
    await seed_user("bob")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "x", "role": "user"},
        cookies=_cookies_for("root"),
    )
    assert resp.status_code == 409


async def test_create_user_missing_fields_rejected(client):
    await seed_user("root", role="admin")
    resp = await client.post(
        "/auth/admin/users", json={"username": "", "password": "", "role": "user"}, cookies=_cookies_for("root")
    )
    assert resp.status_code == 422


# --- Admin: edit user (role change / password reset) -------------------------------


async def test_admin_can_reset_another_users_password(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", password="old-password")
    resp = await client.patch(
        f"/auth/admin/users/{bob_id}", json={"password": "reset-password"}, cookies=_cookies_for("root")
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("bob", "reset-password") is True


async def test_admin_can_promote_a_user_to_admin(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    resp = await client.patch(f"/auth/admin/users/{bob_id}", json={"role": "admin"}, cookies=_cookies_for("root"))
    assert resp.status_code == 200
    assert await shared_auth.get_current_user_role("bob") == "admin"


# --- Last-admin guard ----------------------------------------------------------------


async def test_cannot_delete_the_last_remaining_admin(client):
    admin_id = await seed_user("root", role="admin")
    resp = await client.delete(f"/auth/admin/users/{admin_id}", cookies=_cookies_for("root"))
    assert resp.status_code == 409
    assert await shared_auth.get_current_user_role("root") == "admin"  # unchanged


async def test_cannot_demote_the_last_remaining_admin(client):
    admin_id = await seed_user("root", role="admin")
    resp = await client.patch(f"/auth/admin/users/{admin_id}", json={"role": "user"}, cookies=_cookies_for("root"))
    assert resp.status_code == 409
    assert await shared_auth.get_current_user_role("root") == "admin"  # unchanged


async def test_can_delete_an_admin_when_another_admin_remains(client):
    root_id = await seed_user("root", role="admin")
    await seed_user("root2", role="admin")
    resp = await client.delete(f"/auth/admin/users/{root_id}", cookies=_cookies_for("root2"))
    assert resp.status_code == 200


async def test_can_delete_a_non_admin_freely(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    resp = await client.delete(f"/auth/admin/users/{bob_id}", cookies=_cookies_for("root"))
    assert resp.status_code == 200


async def test_delete_nonexistent_user_returns_404(client):
    await seed_user("root", role="admin")
    resp = await client.delete("/auth/admin/users/999999", cookies=_cookies_for("root"))
    assert resp.status_code == 404


# --- bootstrap_first_admin -----------------------------------------------------------


async def test_bootstrap_first_admin_seeds_from_env_vars(initialized_db, monkeypatch):
    monkeypatch.setattr(shared_auth, "_USER", "bootstrapped-admin")
    monkeypatch.setattr(shared_auth, "_PASS", "bootstrapped-password")
    await shared_auth.bootstrap_first_admin()

    assert await shared_auth.check_credentials("bootstrapped-admin", "bootstrapped-password") is True
    assert await shared_auth.get_current_user_role("bootstrapped-admin") == "admin"


async def test_bootstrap_first_admin_is_idempotent(initialized_db, monkeypatch):
    monkeypatch.setattr(shared_auth, "_USER", "bootstrapped-admin")
    monkeypatch.setattr(shared_auth, "_PASS", "bootstrapped-password")
    await shared_auth.bootstrap_first_admin()
    await shared_auth.bootstrap_first_admin()

    db = await get_db()
    try:
        count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    finally:
        await db.close()
    assert count == 1


async def test_bootstrap_first_admin_noop_when_pass_empty(initialized_db, monkeypatch):
    monkeypatch.setattr(shared_auth, "_PASS", "")
    await shared_auth.bootstrap_first_admin()

    db = await get_db()
    try:
        count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    finally:
        await db.close()
    assert count == 0


async def test_bootstrap_first_admin_noop_when_a_user_already_exists(initialized_db, monkeypatch):
    await seed_user("existing", role="user")
    monkeypatch.setattr(shared_auth, "_USER", "bootstrapped-admin")
    monkeypatch.setattr(shared_auth, "_PASS", "bootstrapped-password")
    await shared_auth.bootstrap_first_admin()

    # Bootstrap must not have run -- "existing" is still the only user, and
    # is not an admin, even though VITALFORGE_PASS was set.
    db = await get_db()
    try:
        count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    finally:
        await db.close()
    assert count == 1
    assert await shared_auth.get_current_user_role("bootstrapped-admin") is None


# --- Live role re-check (the core property this plan exists to guarantee) ----------


async def test_deleted_user_session_cookie_no_longer_authenticates(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    cookie = _cookies_for("bob")

    # Cookie works before deletion.
    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 403  # correctly authenticated as bob, just not admin

    await client.delete(f"/auth/admin/users/{bob_id}", cookies=_cookies_for("root"))

    # Same cookie, same signature -- but bob no longer exists.
    resp = await client.get("/auth/account", cookies=cookie)
    assert resp.status_code == 401


async def test_demoted_admin_session_loses_admin_access_immediately(client):
    root_id = await seed_user("root", role="admin")
    await seed_user("root2", role="admin")
    cookie = _cookies_for("root")

    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 200

    await client.patch(f"/auth/admin/users/{root_id}", json={"role": "user"}, cookies=_cookies_for("root2"))

    # Same cookie -- role is re-read live, not trusted from the signed payload.
    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 403
