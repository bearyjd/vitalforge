"""Simple cookie-based session auth for VitalForge services."""

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict

from shared.database import get_db

logger = logging.getLogger(__name__)

_SECRET = os.environ.get("VITALFORGE_SECRET", "default-dev-secret")
_USER = os.environ.get("VITALFORGE_USER", "admin")
_PASS = os.environ.get("VITALFORGE_PASS", "")
_API_TOKEN = os.environ.get("VITALFORGE_API_TOKEN", "").strip()
_COOKIE_NAME = "vf_session"
_MAX_AGE = 30 * 24 * 3600  # 30 days

# get_current_user returns these two sentinel strings in the same channel
# as real usernames ("anonymous" when auth is off, "api-token" for a valid
# bearer request). A real account created with either name would collide:
# get_current_user_role("api-token") would return that account's role, so
# every holder of the shared bearer token would be authorized as whatever
# role the "api-token" account has (fix-review finding, reproduced). Reject
# both at account-creation time -- Phase B's per-user-api-tokens plan
# removes the "api-token" sentinel entirely by resolving bearer auth to the
# token's real owning username, but that hasn't merged yet.
_RESERVED_USERNAMES = {"anonymous", "api-token"}

# scrypt cost parameters for password hashing. n=2**14 (OWASP's minimum
# recommendation for interactive/low-throughput logins) rather than a
# stronger setting: this is a personal app with infrequent logins, not a
# high-throughput auth service, and n must stay a fixed constant -- changing
# it after users exist invalidates every stored hash (see
# _verify_password's docstring).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

_serializer = URLSafeTimedSerializer(_SECRET)


def _warn_if_misconfigured():
    if _API_TOKEN and not _PASS:
        logger.warning(
            "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty — "
            "auth is DISABLED and the token is inert. Set VITALFORGE_PASS to enable auth."
        )


_warn_if_misconfigured()


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"{salt.hex()}${derived.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    """Never raises on a malformed stored_hash (e.g. a corrupted row) --
    returns False instead, same "fail closed, don't crash the request"
    principle as the rest of this module. n/r/p are fixed module constants,
    not read from stored_hash: changing them after users exist invalidates
    every existing password, since verification re-derives with the
    CURRENT constants against the OLD salt, not with whatever cost
    parameters were used at creation time."""
    try:
        salt_hex, derived_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return hmac.compare_digest(candidate.hex(), derived_hex)


async def _is_auth_configured() -> bool:
    db = await get_db()
    try:
        row = await (await db.execute("SELECT 1 FROM users LIMIT 1")).fetchone()
        return row is not None
    finally:
        await db.close()


def create_session_cookie(username: str, user_id: int) -> str:
    return _serializer.dumps({"user": username, "uid": user_id, "t": int(time.time())})


def validate_session(cookie: str) -> tuple[str, int] | None:
    """Returns (username, user_id) from the signed payload, or None if the
    signature is invalid/expired. get_current_user checks both against the
    users table -- a valid signature alone only proves this server issued
    the cookie at some point, not that it still names the same account. A
    deleted user's username can be reused by a later, different account;
    without the id, the old cookie would authenticate as the new account
    (security-review finding, reproduced)."""
    try:
        data = _serializer.loads(cookie, max_age=_MAX_AGE)
        username = data.get("user")
        user_id = data.get("uid")
        if username is None or user_id is None:
            return None
        return username, user_id
    except (BadSignature, SignatureExpired):
        return None


async def _get_user_id(username: str) -> int | None:
    db = await get_db()
    try:
        row = await (await db.execute("SELECT id FROM users WHERE username = ?", (username,))).fetchone()
    finally:
        await db.close()
    return row["id"] if row is not None else None


async def get_current_user(request: Request) -> str | None:
    if not await _is_auth_configured():
        return "anonymous"
    if _bearer_token_valid(request):
        return "api-token"
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    session = validate_session(cookie)
    if session is None:
        return None
    username, user_id = session
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM users WHERE id = ? AND username = ?", (user_id, username))
        ).fetchone()
    finally:
        await db.close()
    return username if row is not None else None


async def require_auth(request: Request) -> str:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def get_current_user_role(username: str) -> str | None:
    """Separate from get_current_user (which only confirms a session's
    owner still exists) so route handlers that need the role for an
    authorization decision (e.g. /auth/admin/* -- admin-only) fetch it
    explicitly, rather than every request paying for a role lookup it
    doesn't need."""
    db = await get_db()
    try:
        row = await (await db.execute("SELECT role FROM users WHERE username = ?", (username,))).fetchone()
    finally:
        await db.close()
    return row["role"] if row is not None else None


async def _require_admin(request: Request) -> str:
    """require_auth() plus the admin-only role check every /auth/admin/*
    route needs -- was five copies of the same three lines (fix-review
    finding)."""
    user = await require_auth(request)
    if await get_current_user_role(user) != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


async def check_credentials(username: str, password: str) -> bool:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        ).fetchone()
    finally:
        await db.close()
    if row is None:
        return False
    return _verify_password(password, row["password_hash"])


async def bootstrap_first_admin():
    """If no users exist yet, seed one admin from VITALFORGE_USER/
    VITALFORGE_PASS -- a zero-touch upgrade path so an existing
    deployment's login keeps working exactly as before, just backed by a
    real (hashed) user record instead of the env-var pair. Does nothing if
    any user already exists, or if VITALFORGE_PASS is empty (matches
    today's "empty VITALFORGE_PASS = auth disabled" dev convenience -- an
    empty users table IS that state now). Called from each service's own
    lifespan, after init_db() -- both services start against the same
    SQLite file with no `depends_on` ordering between them, so both can
    reach the empty-table check before either commits its INSERT. The
    UNIQUE(username) constraint is the actual race guard: the loser's
    IntegrityError is caught and treated as "someone else already seeded
    it", not a real failure (fix-review finding, reproduced: both
    processes calling this concurrently against a fresh DB, one raises)."""
    if not _PASS:
        return
    if _USER in _RESERVED_USERNAMES:
        # admin_create_user rejects these; the bootstrap path bypassed that
        # guard entirely (fix-review finding) -- VITALFORGE_USER=api-token
        # would seed an admin account under the same name get_current_user
        # returns for every valid bearer-token request, handing that role
        # to anyone holding the shared token. _USER is operator-set, not
        # attacker-controlled, but refuse it here too rather than silently
        # creating the same collision through a different door.
        logger.error(
            "VITALFORGE_USER=%r is a reserved name and cannot be used to seed the "
            "first admin account. Set VITALFORGE_USER to something else and restart.",
            _USER,
        )
        return
    db = await get_db()
    try:
        row = await (await db.execute("SELECT 1 FROM users LIMIT 1")).fetchone()
        if row is not None:
            return
        try:
            await db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (_USER, _hash_password(_PASS), datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            return
        logger.warning(
            "Seeded admin user %r from VITALFORGE_USER/VITALFORGE_PASS -- these env "
            "vars are no longer read for ongoing auth after this, only for this "
            "one-time bootstrap. Manage the account from /auth/account or "
            "/auth/admin/users from now on.",
            _USER,
        )
    finally:
        await db.close()


def _bearer_token_valid(request: Request) -> bool:
    """Constant-time check of the `Authorization: Bearer <token>` header.

    Two independent empty-value guards (no configured token, no presented
    value) because `hmac.compare_digest("", "")` is `True`. Compares bytes,
    not str, so a non-ASCII token returns `False` instead of raising
    `TypeError` (the same bug `check_credentials` had).
    """
    if not _API_TOKEN:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    value = value.strip()
    if not value:
        return False
    return hmac.compare_digest(value.encode("utf-8"), _API_TOKEN.encode("utf-8"))


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            width: 320px;
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; text-align: center; }
        input {
            width: 100%;
            padding: 0.7rem;
            margin-bottom: 0.8rem;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1a1a2e;
            color: #e0e0e0;
            font-size: 0.95rem;
        }
        input:focus { outline: none; border-color: #5c6bc0; }
        button {
            width: 100%;
            padding: 0.7rem;
            background: #5c6bc0;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 0.95rem;
            cursor: pointer;
        }
        button:hover { background: #7c4dff; }
        .error { color: #ef5350; font-size: 0.85rem; margin-bottom: 0.8rem; text-align: center; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>VitalForge</h1>
        <div class="error" id="error"></div>
        <form onsubmit="return doLogin(event)">
            <input type="text" id="user" placeholder="Username" autocomplete="username" required>
            <input type="password" id="pass" placeholder="Password" autocomplete="current-password" required>
            <button type="submit">Sign In</button>
        </form>
    </div>
    <script>
        async function doLogin(e) {
            e.preventDefault();
            const res = await fetch("/auth/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({username: document.getElementById("user").value, password: document.getElementById("pass").value})
            });
            if (res.ok) {
                window.location.href = "/";
            } else {
                document.getElementById("error").textContent = "Invalid credentials";
            }
            return false;
        }
    </script>
</body>
</html>"""


ACCOUNT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Account</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            width: 360px;
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; text-align: center; }
        input {
            width: 100%;
            padding: 0.7rem;
            margin-bottom: 0.8rem;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1a1a2e;
            color: #e0e0e0;
            font-size: 0.95rem;
        }
        input:focus { outline: none; border-color: #5c6bc0; }
        button {
            width: 100%;
            padding: 0.7rem;
            background: #5c6bc0;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 0.95rem;
            cursor: pointer;
        }
        button:hover { background: #7c4dff; }
        .error { color: #ef5350; font-size: 0.85rem; margin-bottom: 0.8rem; text-align: center; }
        .success { color: #66bb6a; font-size: 0.85rem; margin-bottom: 0.8rem; text-align: center; }
        a { color: #5c6bc0; text-decoration: none; display: block; text-align: center; margin-top: 1rem; font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Your Account</h1>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <form onsubmit="return changePassword(event)">
            <input type="password" id="current" placeholder="Current password" autocomplete="current-password" required>
            <input type="password" id="new" placeholder="New password" autocomplete="new-password" required>
            <button type="submit">Change Password</button>
        </form>
        <a href="/">Back</a>
    </div>
    <script>
        async function changePassword(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const res = await fetch("/auth/account/password", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    current_password: document.getElementById("current").value,
                    new_password: document.getElementById("new").value
                })
            });
            if (res.ok) {
                document.getElementById("success").textContent = "Password changed.";
                document.getElementById("current").value = "";
                document.getElementById("new").value = "";
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to change password.";
            }
            return false;
        }
    </script>
</body>
</html>"""

ADMIN_USERS_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Users</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            min-height: 100vh;
            padding: 2rem;
        }
        .box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            max-width: 640px;
            margin: 0 auto;
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; }
        h2 { font-size: 1rem; color: #c0c0e0; margin: 1.5rem 0 0.8rem; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
        th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #2a2a4a; font-size: 0.9rem; }
        input, select {
            width: 100%;
            padding: 0.7rem;
            margin-bottom: 0.8rem;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1a1a2e;
            color: #e0e0e0;
            font-size: 0.95rem;
        }
        button {
            padding: 0.5rem 1rem;
            background: #5c6bc0;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
        }
        button:hover { background: #7c4dff; }
        button.danger { background: #ef5350; }
        button.danger:hover { background: #e53935; }
        .error { color: #ef5350; font-size: 0.85rem; margin-bottom: 0.8rem; }
        .success { color: #66bb6a; font-size: 0.85rem; margin-bottom: 0.8rem; }
        a { color: #5c6bc0; text-decoration: none; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Manage Users</h1>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <table id="users-table">
            <thead><tr><th>Username</th><th>Role</th><th>Created</th><th></th></tr></thead>
            <tbody id="users-body"></tbody>
        </table>
        <h2>Add User</h2>
        <form onsubmit="return createUser(event)">
            <input type="text" id="new-username" placeholder="Username" required>
            <input type="password" id="new-password" placeholder="Password" required>
            <select id="new-role">
                <option value="user">user</option>
                <option value="admin">admin</option>
            </select>
            <button type="submit">Create</button>
        </form>
        <p style="margin-top:1rem"><a href="/">Back</a></p>
    </div>
    <script>
        // Every cell built from server data uses textContent/option.value, never
        // innerHTML -- a username is untrusted input as far as this page is
        // concerned, and innerHTML would execute markup in it.
        async function loadUsers() {
            const res = await fetch("/auth/admin/users/list");
            const users = await res.json();
            const body = document.getElementById("users-body");
            body.innerHTML = "";
            for (const u of users) {
                const row = document.createElement("tr");

                const usernameCell = document.createElement("td");
                usernameCell.textContent = u.username;
                row.appendChild(usernameCell);

                const roleCell = document.createElement("td");
                const roleSelect = document.createElement("select");
                for (const r of ["user", "admin"]) {
                    const opt = document.createElement("option");
                    opt.value = r;
                    opt.textContent = r;
                    if (r === u.role) opt.selected = true;
                    roleSelect.appendChild(opt);
                }
                roleSelect.onchange = () => updateRole(u.id, roleSelect.value);
                roleCell.appendChild(roleSelect);
                row.appendChild(roleCell);

                const createdCell = document.createElement("td");
                createdCell.textContent = u.created_at;
                row.appendChild(createdCell);

                const actionsCell = document.createElement("td");
                const resetBtn = document.createElement("button");
                resetBtn.textContent = "Reset Password";
                resetBtn.onclick = () => resetPassword(u.id);
                actionsCell.appendChild(resetBtn);

                const delBtn = document.createElement("button");
                delBtn.className = "danger";
                delBtn.textContent = "Delete";
                delBtn.onclick = () => deleteUser(u.id);
                actionsCell.appendChild(delBtn);

                row.appendChild(actionsCell);
                body.appendChild(row);
            }
        }

        async function createUser(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const res = await fetch("/auth/admin/users", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    username: document.getElementById("new-username").value,
                    password: document.getElementById("new-password").value,
                    role: document.getElementById("new-role").value
                })
            });
            if (res.ok) {
                document.getElementById("success").textContent = "User created.";
                document.getElementById("new-username").value = "";
                document.getElementById("new-password").value = "";
                loadUsers();
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to create user.";
            }
            return false;
        }

        async function updateRole(id, role) {
            document.getElementById("error").textContent = "";
            const res = await fetch(`/auth/admin/users/${id}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({role: role})
            });
            if (!res.ok) {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to update role.";
            }
            loadUsers();  // re-render either way, so a rejected change reverts the dropdown
        }

        async function resetPassword(id) {
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const newPassword = prompt("New password for this user:");
            if (!newPassword) return;
            const res = await fetch(`/auth/admin/users/${id}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({password: newPassword})
            });
            if (res.ok) {
                document.getElementById("success").textContent = "Password reset.";
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to reset password.";
            }
        }

        async function deleteUser(id) {
            document.getElementById("error").textContent = "";
            const res = await fetch(`/auth/admin/users/${id}`, { method: "DELETE" });
            if (res.ok) {
                loadUsers();
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to delete user.";
            }
        }

        loadUsers();
    </script>
</body>
</html>"""


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


class CreateUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: Literal["admin", "user"] = "user"


class UpdateUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user"] | None = None
    password: str | None = None


def add_auth_routes(app):
    """Add login/logout routes to a FastAPI app."""

    @app.get("/auth/login")
    async def login_page(request: Request):
        if await get_current_user(request):
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(LOGIN_PAGE_HTML)

    @app.post("/auth/login")
    async def login(request: Request):
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if not await check_credentials(username, password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user_id = await _get_user_id(username)
        if user_id is None:
            # Narrow TOCTOU: the account was deleted between the
            # check_credentials() call above and this one. Fail the login
            # rather than issue a cookie with no matching account -- it
            # would just be permanently invalid anyway (get_current_user's
            # id+username check could never match), but failing loudly here
            # is clearer than a cookie that silently never authenticates.
            raise HTTPException(status_code=401, detail="Invalid credentials")
        cookie = create_session_cookie(username, user_id)
        response = JSONResponse({"success": True})
        response.set_cookie(_COOKIE_NAME, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax")
        return response

    @app.get("/auth/logout")
    async def logout():
        response = RedirectResponse("/auth/login", status_code=302)
        response.delete_cookie(_COOKIE_NAME)
        return response

    @app.get("/auth/account")
    async def account_page(request: Request):
        await require_auth(request)
        return HTMLResponse(ACCOUNT_PAGE_HTML)

    @app.post("/auth/account/password")
    async def change_own_password(request: Request, data: PasswordChangeIn):
        user = await require_auth(request)
        if not await check_credentials(user, data.current_password):
            raise HTTPException(status_code=401, detail="Current password incorrect")
        if not data.new_password:
            raise HTTPException(status_code=422, detail="New password required")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (_hash_password(data.new_password), user),
            )
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.get("/auth/admin/users")
    async def admin_users_page(request: Request):
        await _require_admin(request)
        return HTMLResponse(ADMIN_USERS_PAGE_HTML)

    @app.get("/auth/admin/users/list")
    async def admin_list_users(request: Request):
        await _require_admin(request)
        db = await get_db()
        try:
            rows = await (
                await db.execute("SELECT id, username, role, created_at FROM users ORDER BY username")
            ).fetchall()
        finally:
            await db.close()
        return [dict(row) for row in rows]

    @app.post("/auth/admin/users")
    async def admin_create_user(request: Request, data: CreateUserIn):
        await _require_admin(request)
        username = data.username.strip()
        if not username or not data.password:
            raise HTTPException(status_code=422, detail="username and password are required")
        if username in _RESERVED_USERNAMES:
            raise HTTPException(status_code=422, detail=f"'{username}' is a reserved username")
        db = await get_db()
        try:
            try:
                await db.execute(
                    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    (username, _hash_password(data.password), data.role, datetime.now(timezone.utc).isoformat()),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                raise HTTPException(status_code=409, detail="Username already exists")
        finally:
            await db.close()
        return {"success": True}

    @app.patch("/auth/admin/users/{user_id}")
    async def admin_update_user(request: Request, user_id: int, data: UpdateUserIn):
        await _require_admin(request)
        new_role = data.role
        new_password = data.password
        db = await get_db()
        try:
            # BEGIN IMMEDIATE makes the count-then-write atomic: two
            # concurrent demotes of the two different last-two admins
            # could otherwise both read admin_count=2 before either
            # commits, both pass the guard, and leave zero admins
            # (security-review finding, reproduced end to end). The second
            # request's BEGIN IMMEDIATE blocks until the first commits, so
            # it then sees the already-reduced count. Same pattern as
            # vitalforge-weight/app.py's dedup transaction.
            await db.execute("BEGIN IMMEDIATE")
            target = await (await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))).fetchone()
            if target is None:
                await db.rollback()
                raise HTTPException(status_code=404, detail="User not found")
            if new_role is not None and target["role"] == "admin" and new_role != "admin":
                admin_count = (
                    await (await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")).fetchone()
                )[0]
                if admin_count <= 1:
                    await db.rollback()
                    raise HTTPException(status_code=409, detail="Cannot demote the last remaining admin")
            updates = {}
            if new_role is not None:
                updates["role"] = new_role
            if new_password:
                updates["password_hash"] = _hash_password(new_password)
            if updates:
                set_clause = ", ".join(f"{field} = ?" for field in updates)
                await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", (*updates.values(), user_id))
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.delete("/auth/admin/users/{user_id}")
    async def admin_delete_user(request: Request, user_id: int):
        await _require_admin(request)
        db = await get_db()
        try:
            # See admin_update_user's comment -- same TOCTOU race, same fix.
            await db.execute("BEGIN IMMEDIATE")
            target = await (await db.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))).fetchone()
            if target is None:
                await db.rollback()
                raise HTTPException(status_code=404, detail="User not found")
            if target["role"] == "admin":
                admin_count = (
                    await (await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")).fetchone()
                )[0]
                if admin_count <= 1:
                    await db.rollback()
                    raise HTTPException(status_code=409, detail="Cannot delete the last remaining admin")
            await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Skip auth for login routes, health check, static files, and service worker
        path = request.url.path
        if path.startswith("/auth/") or path == "/health" or path.startswith("/static/"):
            return await call_next(request)

        if not await _is_auth_configured():
            return await call_next(request)

        user = await get_current_user(request)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return RedirectResponse("/auth/login", status_code=302)

        return await call_next(request)
