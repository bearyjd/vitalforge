"""User accounts & auth model (`.claude/PRPs/plans/user-accounts-auth-model.plan.md`):
password change, admin user CRUD, the last-admin-deletion/demotion guard, and
`bootstrap_first_admin`'s idempotency.

Uses the same throwaway-app pattern as test_auth_matrix.py/test_auth_middleware.py
so these routes are tested in isolation from either real service.
"""

import asyncio

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


async def _cookies_for(username: str) -> dict:
    user_id, session_version = await shared_auth._get_user_id_and_session_version(username)
    return {"vf_session": create_session_cookie(username, user_id, session_version)}


# --- XSS regression (fix-review finding) --------------------------------------------


async def test_admin_users_page_does_not_render_username_via_innerhtml(client):
    """Fix-review finding: the users table used to build each row with
    `row.innerHTML = `<td>${u.username}</td>...``, so a username containing
    markup would execute in any admin's browser on page load -- a username
    is untrusted input (an admin creating an account doesn't get to pick
    what a future account's owner named themselves via self-service, once
    that exists). Every cell must be built via textContent/option.value,
    matching how the Delete button was already built correctly. This is a
    static check on the served page, not a browser-executed one -- no
    fixture in this repo runs an authenticated page through a real browser
    yet, so this is the practical regression guard: verify the vulnerable
    pattern is gone and the safe one is present, in the actual HTTP
    response, not just the source constant."""
    await seed_user("root", role="admin")
    resp = await client.get("/auth/admin/users", cookies=await _cookies_for("root"))
    assert resp.status_code == 200
    html = resp.text
    assert "innerHTML = `<td>" not in html
    assert "usernameCell.textContent = u.username" in html


# --- Self-service password change -------------------------------------------------


async def test_change_own_password_requires_correct_current_password(client):
    await seed_user("alice", password="old-password")
    resp = await client.post(
        "/auth/account/password",
        json={"current_password": "wrong-password", "new_password": "new-password"},
        cookies=await _cookies_for("alice"),
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
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("alice", "old-password") is False
    assert await shared_auth.check_credentials("alice", "new-password") is True


async def test_change_own_password_revokes_existing_sessions(client):
    """MEDIUM security-review finding: a password change used to leave
    every already-issued cookie for that account valid until its own
    30-day expiry -- if the change was prompted by a suspected leak, the
    leaked cookie kept working regardless. Capturing the cookie BEFORE the
    change (via _cookies_for, which reads the account's current
    session_version at call time) and reusing it after is the regression
    check: session_version increments on the UPDATE, so the pre-change
    cookie's embedded version stops matching."""
    await seed_user("alice", password="old-password")
    old_cookie = await _cookies_for("alice")

    resp = await client.post(
        "/auth/account/password",
        json={"current_password": "old-password", "new_password": "new-password"},
        cookies=old_cookie,
    )
    assert resp.status_code == 200

    # Same cookie that authenticated the request above -- now stale.
    resp = await client.get("/auth/account", cookies=old_cookie)
    assert resp.status_code == 401

    # A freshly-issued cookie for the same account still works.
    new_cookie = await _cookies_for("alice")
    resp = await client.get("/auth/account", cookies=new_cookie)
    assert resp.status_code == 200


async def test_admin_password_reset_revokes_the_targets_existing_sessions(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", password="old-password")
    bob_old_cookie = await _cookies_for("bob")

    resp = await client.patch(
        f"/auth/admin/users/{bob_id}", json={"password": "reset-password"}, cookies=await _cookies_for("root")
    )
    assert resp.status_code == 200

    resp = await client.get("/auth/account", cookies=bob_old_cookie)
    assert resp.status_code == 401


async def test_change_own_password_empty_new_password_rejected(client):
    await seed_user("alice", password="old-password")
    resp = await client.post(
        "/auth/account/password",
        json={"current_password": "old-password", "new_password": ""},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 422
    assert await shared_auth.check_credentials("alice", "old-password") is True  # unchanged


async def test_change_own_password_requires_auth(client):
    """Fix-review finding: the route returns 401 for both "not
    authenticated" and "current password incorrect" -- asserting only the
    status code can't distinguish them, so this could have passed even if
    require_auth's own check were deleted (the request has no real
    username, so check_credentials would fail too, coincidentally also
    401). Asserting the detail message pins it to the specific check."""
    await seed_user("someone")  # turn auth on -- an empty users table means open access
    resp = await client.post(
        "/auth/account/password", json={"current_password": "x", "new_password": "y"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


# --- Admin route access ------------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected_status",
    [("user", 403), ("admin", 200)],
    ids=["user-role-blocked", "admin-role-allowed"],
)
async def test_admin_users_list_access_by_role(client, role, expected_status):
    await seed_user("someone", role=role)
    resp = await client.get("/auth/admin/users/list", cookies=await _cookies_for("someone"))
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
        cookies=await _cookies_for("root"),
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("bob", "bobs-password") is True
    assert await shared_auth.get_current_user_role("bob") == "user"


async def test_non_admin_cannot_create_user(client):
    await seed_user("someone", role="user")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "x", "role": "user"},
        cookies=await _cookies_for("someone"),
    )
    assert resp.status_code == 403


async def test_create_user_duplicate_username_rejected(client):
    await seed_user("root", role="admin")
    await seed_user("bob")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "x", "role": "user"},
        cookies=await _cookies_for("root"),
    )
    assert resp.status_code == 409


async def test_create_user_missing_fields_rejected(client):
    await seed_user("root", role="admin")
    resp = await client.post(
        "/auth/admin/users", json={"username": "", "password": "", "role": "user"}, cookies=await _cookies_for("root")
    )
    assert resp.status_code == 422


async def test_create_user_invalid_role_rejected(client):
    await seed_user("root", role="admin")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": "bob", "password": "x", "role": "superadmin"},
        cookies=await _cookies_for("root"),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"username": 123, "password": "x", "role": "user"}},
        {"content": "[1, 2, 3]", "headers": {"Content-Type": "application/json"}},
        {"content": "not json{{{", "headers": {"Content-Type": "application/json"}},
    ],
    ids=["non-string-username", "json-list-body", "malformed-json"],
)
async def test_create_user_malformed_input_returns_422_not_500(client, kwargs):
    """Fix-review finding: hand-rolled `body.get(...)` parsing crashed with
    an uncaught AttributeError/JSONDecodeError -> 500 on these three inputs
    before this route took a Pydantic body model, same as this repo's own
    documented 422-not-500 convention (see
    test_weight_api.py::test_non_finite_float_rejected_422_not_500)."""
    await seed_user("root", role="admin")
    resp = await client.post("/auth/admin/users", cookies=await _cookies_for("root"), **kwargs)
    assert resp.status_code == 422


@pytest.mark.parametrize("reserved", ["anonymous", "api-token"])
async def test_create_user_reserved_username_rejected(client, reserved):
    """Fix-review finding: get_current_user() returns these two strings as
    sentinels in the same channel as real usernames (`anonymous` when auth
    is off, `api-token` for any valid bearer request). An account actually
    named one of them would collide -- every holder of the shared bearer
    token would inherit whatever role the `api-token` account has."""
    await seed_user("root", role="admin")
    resp = await client.post(
        "/auth/admin/users",
        json={"username": reserved, "password": "x", "role": "admin"},
        cookies=await _cookies_for("root"),
    )
    assert resp.status_code == 422
    assert await shared_auth.get_current_user_role(reserved) is None


# --- Admin: edit user (role change / password reset) -------------------------------


async def test_admin_can_reset_another_users_password(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", password="old-password")
    resp = await client.patch(
        f"/auth/admin/users/{bob_id}", json={"password": "reset-password"}, cookies=await _cookies_for("root")
    )
    assert resp.status_code == 200
    assert await shared_auth.check_credentials("bob", "reset-password") is True


async def test_admin_can_promote_a_user_to_admin(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    resp = await client.patch(f"/auth/admin/users/{bob_id}", json={"role": "admin"}, cookies=await _cookies_for("root"))
    assert resp.status_code == 200
    assert await shared_auth.get_current_user_role("bob") == "admin"


async def test_admin_update_nonexistent_user_returns_404(client):
    await seed_user("root", role="admin")
    resp = await client.patch("/auth/admin/users/999999", json={"role": "admin"}, cookies=await _cookies_for("root"))
    assert resp.status_code == 404


async def test_admin_update_invalid_role_rejected(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    resp = await client.patch(
        f"/auth/admin/users/{bob_id}", json={"role": "superadmin"}, cookies=await _cookies_for("root")
    )
    assert resp.status_code == 422
    assert await shared_auth.get_current_user_role("bob") == "user"  # unchanged


# --- Last-admin guard ----------------------------------------------------------------


async def test_cannot_delete_the_last_remaining_admin(client):
    admin_id = await seed_user("root", role="admin")
    resp = await client.delete(f"/auth/admin/users/{admin_id}", cookies=await _cookies_for("root"))
    assert resp.status_code == 409
    assert await shared_auth.get_current_user_role("root") == "admin"  # unchanged


async def test_cannot_demote_the_last_remaining_admin(client):
    admin_id = await seed_user("root", role="admin")
    resp = await client.patch(f"/auth/admin/users/{admin_id}", json={"role": "user"}, cookies=await _cookies_for("root"))
    assert resp.status_code == 409
    assert await shared_auth.get_current_user_role("root") == "admin"  # unchanged


async def test_can_delete_an_admin_when_another_admin_remains(client):
    root_id = await seed_user("root", role="admin")
    await seed_user("root2", role="admin")
    resp = await client.delete(f"/auth/admin/users/{root_id}", cookies=await _cookies_for("root2"))
    assert resp.status_code == 200


async def test_concurrent_deletes_of_the_last_two_admins_cannot_both_succeed(client):
    """CRITICAL security-review finding: the last-admin guard used to do
    SELECT COUNT(*) -> decide -> DELETE with no transaction wrapping the
    two. Two concurrent requests deleting the two different last-remaining
    admins could both read admin_count=2, both pass the guard, and leave
    zero admins -- reproduced end to end (both DELETEs returned 200,
    followed by an unauthenticated request reaching real data, since an
    empty users table means auth is off). BEGIN IMMEDIATE now makes the
    count-then-write atomic per request, so the second request's
    transaction can't start until the first's has committed and it then
    sees the reduced count. Loops several times since a race is
    nondeterministic -- a single passing iteration doesn't prove the race
    is closed, but this codebase's own existing concurrency tests
    (test_dedup_concurrency.py) use the same asyncio.gather-based approach
    without needing more than that to catch a real regression reliably."""
    for i in range(10):
        # Reset to exactly zero admins before each attempt -- a surviving
        # (409'd) admin from a prior iteration would otherwise pad the
        # count above 1 for the next pair, masking the very race this
        # test exists to catch (both deletes would then legitimately
        # succeed, since other admins genuinely do remain).
        db = await get_db()
        try:
            await db.execute("DELETE FROM users WHERE role = 'admin'")
            await db.commit()
        finally:
            await db.close()

        alice_id = await seed_user(f"admin-a-{i}", role="admin")
        bob_id = await seed_user(f"admin-b-{i}", role="admin")
        results = await asyncio.gather(
            client.delete(f"/auth/admin/users/{alice_id}", cookies=await _cookies_for(f"admin-a-{i}")),
            client.delete(f"/auth/admin/users/{bob_id}", cookies=await _cookies_for(f"admin-b-{i}")),
        )
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 409], f"iteration {i}: both requests must not both succeed, got {statuses}"

        db = await get_db()
        try:
            remaining = (await (await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")).fetchone())[0]
        finally:
            await db.close()
        assert remaining == 1, f"iteration {i}: expected exactly 1 admin remaining, got {remaining}"


async def test_can_delete_a_non_admin_freely(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    resp = await client.delete(f"/auth/admin/users/{bob_id}", cookies=await _cookies_for("root"))
    assert resp.status_code == 200


async def test_delete_nonexistent_user_returns_404(client):
    await seed_user("root", role="admin")
    resp = await client.delete("/auth/admin/users/999999", cookies=await _cookies_for("root"))
    assert resp.status_code == 404


# --- bootstrap_first_admin -----------------------------------------------------------


async def test_bootstrap_first_admin_survives_concurrent_startup(initialized_db, monkeypatch):
    """Fix-review finding: neither compose file declares `depends_on`, so
    both services can start concurrently against the same SQLite file and
    both call this on a fresh (empty) DB. Both SELECT empty, both attempt
    the INSERT, one hits UNIQUE(username) -- that must be swallowed as
    "someone else already seeded it", not propagate and crash that
    service's startup."""
    monkeypatch.setattr(shared_auth, "_USER", "bootstrapped-admin")
    monkeypatch.setattr(shared_auth, "_PASS", "bootstrapped-password")

    results = await asyncio.gather(
        shared_auth.bootstrap_first_admin(),
        shared_auth.bootstrap_first_admin(),
        return_exceptions=True,
    )
    assert results == [None, None]  # neither call raised

    db = await get_db()
    try:
        count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    finally:
        await db.close()
    assert count == 1


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


@pytest.mark.parametrize("reserved", ["anonymous", "api-token"])
async def test_bootstrap_first_admin_refuses_reserved_username(initialized_db, monkeypatch, reserved):
    """Fix-review finding: admin_create_user's _RESERVED_USERNAMES guard
    didn't cover the bootstrap path -- VITALFORGE_USER=api-token would
    seed an admin account under the same name every valid bearer-token
    request resolves to, handing that role to anyone holding the shared
    token."""
    monkeypatch.setattr(shared_auth, "_USER", reserved)
    monkeypatch.setattr(shared_auth, "_PASS", "bootstrapped-password")
    await shared_auth.bootstrap_first_admin()

    db = await get_db()
    try:
        count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    finally:
        await db.close()
    assert count == 0
    assert await shared_auth.get_current_user_role(reserved) is None


# --- Live role re-check (the core property this plan exists to guarantee) ----------


async def test_deleted_user_session_cookie_no_longer_authenticates(client):
    await seed_user("root", role="admin")
    bob_id = await seed_user("bob", role="user")
    cookie = await _cookies_for("bob")

    # Cookie works before deletion.
    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 403  # correctly authenticated as bob, just not admin

    await client.delete(f"/auth/admin/users/{bob_id}", cookies=await _cookies_for("root"))

    # Same cookie, same signature -- but bob no longer exists.
    resp = await client.get("/auth/account", cookies=cookie)
    assert resp.status_code == 401


async def test_deleted_users_cookie_does_not_authenticate_as_a_later_reused_username(client):
    """HIGH security-review finding: the session cookie used to carry only
    the username, not an account id -- deleting a user and creating a
    NEW, different account with the SAME username let the old cookie
    authenticate as the new account (reproduced: an old `user`-role
    cookie resolved as the new `admin`-role account after the name was
    reused). The cookie now carries both id and username, and
    get_current_user requires both to match the current row -- a new
    account gets a new id, so the old signature no longer matches
    anything."""
    await seed_user("root", role="admin")
    await seed_user("shared_name", role="user")
    old_cookie = await _cookies_for("shared_name")

    db = await get_db()
    try:
        await db.execute("DELETE FROM users WHERE username = ?", ("shared_name",))
        await db.commit()
    finally:
        await db.close()

    # A different person, coincidentally given the same username, as admin.
    await seed_user("shared_name", role="admin")

    resp = await client.get("/auth/admin/users/list", cookies=old_cookie)
    assert resp.status_code == 401  # not 200 -- must not resolve to the new account at all


async def test_demoted_admin_session_loses_admin_access_immediately(client):
    root_id = await seed_user("root", role="admin")
    await seed_user("root2", role="admin")
    cookie = await _cookies_for("root")

    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 200

    await client.patch(f"/auth/admin/users/{root_id}", json={"role": "user"}, cookies=await _cookies_for("root2"))

    # Same cookie -- role is re-read live, not trusted from the signed payload.
    resp = await client.get("/auth/admin/users/list", cookies=cookie)
    assert resp.status_code == 403
