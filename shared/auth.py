"""Simple cookie-based session auth for VitalForge services."""

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Literal, NamedTuple

import aiosqlite
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict

from shared.database import get_db

logger = logging.getLogger(__name__)

_INSECURE_SECRET_PLACEHOLDERS = {
    "default-dev-secret",
    "change-this-to-a-random-string",
    "your-random-secret-here",
}


def _resolve_secret(configured: str) -> str:
    """Never sign sessions with the public default secret.

    Generate a process-local replacement instead, while warning operators
    about the session and cross-service consequences of leaving the real
    setting unconfigured.
    """
    if configured.strip() and configured.strip() not in _INSECURE_SECRET_PLACEHOLDERS:
        return configured
    generated = secrets.token_urlsafe(32)
    logger.warning(
        "VITALFORGE_SECRET is unset, blank, or still a known placeholder -- "
        "generated a random secret for THIS PROCESS instead of using the "
        "public default. Every existing session cookie is now invalid, "
        "this will happen again on every restart, and (if you run both "
        "services) they will each generate a DIFFERENT secret, breaking "
        "single sign-on between them, until you set VITALFORGE_SECRET in "
        ".env -- see README's Environment Variables section."
    )
    return generated


_SECRET = _resolve_secret(os.environ.get("VITALFORGE_SECRET", ""))
_USER = os.environ.get("VITALFORGE_USER", "admin")
_PASS = os.environ.get("VITALFORGE_PASS", "")
_COOKIE_NAME = "vf_session"
_MAX_AGE = 30 * 24 * 3600  # 30 days
_LEGACY_TOKEN_MIGRATION = "legacy-api-token-v1"

# "anonymous" remains the open-access sentinel. "api-token" was the old
# shared-token sentinel; keep both reserved so upgrades cannot create an
# ambiguous real account with a formerly special identity.
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


class _Identity(NamedTuple):
    username: str
    user_id: int | None
    session_version: int | None
    role: str | None


def _request_is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded == "https" or request.url.scheme == "https"


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"{salt.hex()}${derived.hex()}"


# check_credentials() verifies against this when the username doesn't
# exist, so an unknown username still pays the same scrypt cost a real
# check would -- see check_credentials' own docstring for why.
_DUMMY_PASSWORD_HASH = _hash_password("dummy-password-for-timing-parity-only-not-a-real-credential")


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


def create_session_cookie(username: str, user_id: int, session_version: int) -> str:
    return _serializer.dumps(
        {"user": username, "uid": user_id, "sv": session_version, "t": int(time.time())}
    )


def validate_session(cookie: str) -> tuple[str, int, int] | None:
    """Returns (username, user_id, session_version) from the signed
    payload, or None if the signature is invalid/expired. get_current_user
    checks all three against the users table -- a valid signature alone
    only proves this server issued the cookie at some point, not that it
    still names the same account (a deleted user's username can be reused
    by a later, different account -- caught by user_id) or that the
    account's password hasn't changed since (caught by session_version,
    incremented on every password change so older cookies stop validating
    immediately instead of staying valid until their 30-day expiry --
    security-review finding)."""
    try:
        data = _serializer.loads(cookie, max_age=_MAX_AGE)
        username = data.get("user")
        user_id = data.get("uid")
        session_version = data.get("sv")
        if username is None or user_id is None or session_version is None:
            return None
        return username, user_id, session_version
    except (BadSignature, SignatureExpired):
        return None


async def _get_user_id_and_session_version(username: str) -> tuple[int, int] | None:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT id, session_version FROM users WHERE username = ?", (username,))
        ).fetchone()
    finally:
        await db.close()
    return (row["id"], row["session_version"]) if row is not None else None


async def _get_current_identity(request: Request) -> _Identity | None:
    """Return one account-bound identity and live role check.

    Cookie identity and role are selected together using every value bound
    into the signed session. Keeping this as one query prevents a deleted
    username from being recreated between an identity lookup and a later
    role lookup, which could otherwise lend the new account's role to the
    old account's cookie.
    """
    if not await _is_auth_configured():
        return _Identity("anonymous", None, None, None)
    bearer_identity = await _resolve_bearer_token(request)
    if bearer_identity is not None:
        return bearer_identity
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    session = validate_session(cookie)
    if session is None:
        return None
    username, user_id, session_version = session
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT role FROM users WHERE id = ? AND username = ? AND session_version = ?",
                (user_id, username, session_version),
            )
        ).fetchone()
    finally:
        await db.close()
    return _Identity(username, user_id, session_version, row["role"]) if row is not None else None


async def get_current_user(request: Request) -> str | None:
    identity = await _get_current_identity(request)
    return identity.username if identity is not None else None


async def require_auth(request: Request) -> str:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def get_current_user_role(username: str) -> str | None:
    """Return a user's current role for management/display callers.

    Authorization paths use _get_current_identity instead so cookie-bound
    identity and role cannot be separated by username-reuse races.
    """
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
    identity = await _get_current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return identity.username


# Public alias for callers outside this module (e.g. dashboard goal
# ownership checks) that need the full account-bound identity, including
# live role, without reaching into this module's private names.
Identity = _Identity


async def require_account_identity(request: Request) -> Identity:
    """Resolve the caller to a full account-bound identity (401 if there
    isn't one), including live role for authorization decisions that need
    it (e.g. "owner or admin can act on this resource").
    get_current_user_role() is explicitly documented as unsafe for that -- a
    username-reuse race can lend a new account's role to an old account's
    cookie -- so this reuses _get_current_identity's single cookie/token-bound
    query instead, the same safe path every other identity-requiring route in
    this module uses. Also the implementation behind the private
    `_require_account_identity` name below, kept as an alias for existing
    internal call sites (one implementation, not two copies of the same
    four lines -- see _require_admin's docstring for why that matters here)."""
    identity = await _get_current_identity(request)
    if identity is None or identity.user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return identity


_require_account_identity = require_account_identity


async def _require_step_up(identity: _Identity, current_password: str):
    verified = await _authenticate_credentials(identity.username, current_password)
    if verified is None or verified[0] != identity.user_id:
        raise HTTPException(status_code=401, detail="Current password incorrect")


async def _authenticate_credentials(username: str, password: str) -> tuple[int, int] | None:
    """Verify credentials and return identity from the exact row verified.

    Fetching the password hash, account id, and session version together
    prevents username deletion/recreation between password verification and
    session issuance. If that row is later deleted or changed, the returned
    id/version can only produce a cookie that fails closed.
    """
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT id, password_hash, session_version FROM users WHERE username = ?",
                (username,),
            )
        ).fetchone()
    finally:
        await db.close()
    stored_hash = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
    verified = _verify_password(password, stored_hash)
    if row is None or not verified:
        return None
    return row["id"], row["session_version"]


async def check_credentials(username: str, password: str) -> bool:
    """Always runs one scrypt verification, real or dummy -- an unknown
    username used to return False immediately, skipping the ~29ms scrypt
    cost a real check pays. That gap is a measurable, exploitable
    username-enumeration oracle (security-review finding): an attacker can
    tell a valid username from an invalid one by response time alone,
    without ever guessing a password."""
    return await _authenticate_credentials(username, password) is not None


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
    db = await get_db()
    try:
        row = await (await db.execute("SELECT 1 FROM users LIMIT 1")).fetchone()
        if row is not None:
            return
        if not _PASS:
            return
        if _USER in _RESERVED_USERNAMES:
            # admin_create_user rejects these; the bootstrap path bypassed
            # that guard entirely (fix-review finding) --
            # VITALFORGE_USER=api-token would seed an admin account under
            # the same name get_current_user returns for every valid bearer
            # request, handing that role to anyone holding the shared token.
            logger.error(
                "VITALFORGE_USER=%r is a reserved name and cannot be used to seed the "
                "first admin account. Set VITALFORGE_USER to something else and restart.",
                _USER,
            )
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


async def bootstrap_migrated_token():
    """Migrate the legacy env token exactly once onto the first admin.

    The durable marker is committed atomically with the token. Both services
    execute this during concurrent startup, so BEGIN IMMEDIATE serializes the
    marker check and write. Keeping the marker after token revocation prevents
    the legacy credential from being resurrected on a later restart.
    """
    legacy_token = os.environ.get("VITALFORGE_API_TOKEN", "").strip()
    if not legacy_token:
        return
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        migrated = await (
            await db.execute("SELECT 1 FROM auth_migrations WHERE name = ?", (_LEGACY_TOKEN_MIGRATION,))
        ).fetchone()
        if migrated is not None:
            await db.rollback()
            return
        admin = await (
            await db.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        ).fetchone()
        if admin is None:
            await db.rollback()
            logger.warning(
                "VITALFORGE_API_TOKEN is set but no admin account exists to own its "
                "DB-backed migration. Set VITALFORGE_USER/VITALFORGE_PASS and restart."
            )
            return
        token_hash = hashlib.sha256(legacy_token.encode("utf-8")).hexdigest()
        await db.execute(
            "INSERT OR IGNORE INTO api_tokens (user_id, label, token_hash, created_at) "
            "VALUES (?, 'migrated-from-env', ?, ?)",
            (admin["id"], token_hash, datetime.now(timezone.utc).isoformat()),
        )
        await db.execute(
            "INSERT INTO auth_migrations (name, completed_at) VALUES (?, ?)",
            (_LEGACY_TOKEN_MIGRATION, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        logger.warning(
            "Migrated VITALFORGE_API_TOKEN into a DB-backed token owned by the first "
            "admin account. The env var is no longer used for ongoing authentication; "
            "manage tokens from /auth/account or /auth/admin/users."
        )
    finally:
        await db.close()


async def _resolve_bearer_token(request: Request) -> _Identity | None:
    """Resolve a bearer value to its account-bound owning identity."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    if not value:
        return None
    token_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT api_tokens.id AS token_id, users.id AS user_id, users.username, "
                "users.session_version, users.role FROM api_tokens "
                "JOIN users ON users.id = api_tokens.user_id WHERE api_tokens.token_hash = ?",
                (token_hash,),
            )
        ).fetchone()
        if row is None:
            return None
        try:
            await db.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["token_id"]),
            )
            await db.commit()
        except Exception as e:
            logger.warning("Failed to update last_used_at for token id %s: %s", row["token_id"], e)
        return _Identity(row["username"], row["user_id"], row["session_version"], row["role"])
    finally:
        await db.close()


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
            width: min(620px, calc(100vw - 2rem));
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; text-align: center; }
        h2 { font-size: 1rem; color: #c0c0e0; margin: 1.8rem 0 0.8rem; }
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
        table { width: 100%; border-collapse: collapse; margin-top: 0.8rem; }
        th, td { text-align: left; padding: 0.45rem; border-bottom: 1px solid #2a2a4a; font-size: 0.82rem; }
        td button { width: auto; padding: 0.4rem 0.7rem; background: #ef5350; }
        .token-reveal { display: none; margin: 0.8rem 0; padding: 0.8rem; background: #1a1a2e; border-radius: 6px; }
        .token-reveal code { display: block; overflow-wrap: anywhere; margin: 0.5rem 0; color: #80cbc4; }
        .token-reveal button { width: auto; }
        .hint { color: #aaa; font-size: 0.8rem; margin-bottom: 0.8rem; }
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
        <h2>API Tokens</h2>
        <p class="hint">Create a named token for Tasker, Bascule, or another unattended client.</p>
        <div class="token-reveal" id="token-reveal">
            <strong>Copy this token now. It will not be shown again.</strong>
            <code id="raw-token"></code>
            <button type="button" onclick="copyToken()">Copy token</button>
        </div>
        <form onsubmit="return createToken(event)">
            <input type="text" id="token-label" placeholder="Label (for example, Bascule)" required>
            <input type="password" id="token-password" placeholder="Current password" autocomplete="current-password" required>
            <button type="submit">Create Token</button>
        </form>
        <table>
            <thead><tr><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead>
            <tbody id="tokens-body"></tbody>
        </table>
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

        async function loadTokens() {
            const res = await fetch("/auth/tokens");
            if (!res.ok) return;
            const tokens = await res.json();
            const body = document.getElementById("tokens-body");
            body.textContent = "";
            for (const token of tokens) {
                const row = document.createElement("tr");
                for (const value of [token.label, token.created_at, token.last_used_at || "never"]) {
                    const cell = document.createElement("td");
                    cell.textContent = value;
                    row.appendChild(cell);
                }
                const action = document.createElement("td");
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = "Revoke";
                button.onclick = () => revokeToken(token.id);
                action.appendChild(button);
                row.appendChild(action);
                body.appendChild(row);
            }
        }

        async function createToken(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            const res = await fetch("/auth/tokens", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    label: document.getElementById("token-label").value,
                    current_password: document.getElementById("token-password").value
                })
            });
            const responseBody = await res.json();
            if (res.ok) {
                document.getElementById("raw-token").textContent = responseBody.token;
                document.getElementById("token-reveal").style.display = "block";
                document.getElementById("token-label").value = "";
                document.getElementById("token-password").value = "";
                loadTokens();
            } else {
                document.getElementById("error").textContent = responseBody.detail || "Failed to create token.";
            }
            return false;
        }

        async function copyToken() {
            await navigator.clipboard.writeText(document.getElementById("raw-token").textContent);
        }

        async function revokeToken(id) {
            const password = prompt("Enter your current password to revoke this token:");
            if (!password) return;
            const res = await fetch(`/auth/tokens/${id}/revoke`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({current_password: password})
            });
            if (res.ok) {
                loadTokens();
            } else {
                const responseBody = await res.json();
                document.getElementById("error").textContent = responseBody.detail || "Failed to revoke token.";
            }
        }

        loadTokens();
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
        <h2>All API Tokens</h2>
        <table>
            <thead><tr><th>Owner</th><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead>
            <tbody id="admin-tokens-body"></tbody>
        </table>
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
            body.textContent = "";
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
                loadAllTokens();
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to delete user.";
            }
        }

        async function loadAllTokens() {
            const res = await fetch("/auth/admin/tokens");
            if (!res.ok) return;
            const tokens = await res.json();
            const body = document.getElementById("admin-tokens-body");
            body.textContent = "";
            for (const token of tokens) {
                const row = document.createElement("tr");
                for (const value of [token.owner, token.label, token.created_at, token.last_used_at || "never"]) {
                    const cell = document.createElement("td");
                    cell.textContent = value;
                    row.appendChild(cell);
                }
                const action = document.createElement("td");
                const button = document.createElement("button");
                button.type = "button";
                button.className = "danger";
                button.textContent = "Revoke";
                button.onclick = () => revokeManagedToken(token.id);
                action.appendChild(button);
                row.appendChild(action);
                body.appendChild(row);
            }
        }

        async function revokeManagedToken(id) {
            const password = prompt("Enter your current password to revoke this token:");
            if (!password) return;
            const res = await fetch(`/auth/tokens/${id}/revoke`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({current_password: password})
            });
            if (res.ok) {
                loadAllTokens();
            } else {
                const responseBody = await res.json();
                document.getElementById("error").textContent = responseBody.detail || "Failed to revoke token.";
            }
        }

        loadUsers();
        loadAllTokens();
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


class CreateTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    current_password: str


class RevokeTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str


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
        id_and_version = await _authenticate_credentials(username, password)
        if id_and_version is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user_id, session_version = id_and_version
        cookie = create_session_cookie(username, user_id, session_version)
        response = JSONResponse({"success": True})
        response.set_cookie(
            _COOKIE_NAME,
            cookie,
            max_age=_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_request_is_https(request),
        )
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
        identity = await _get_current_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if identity.user_id is None:
            raise HTTPException(status_code=401, detail="Current password incorrect")
        verified_identity = await _authenticate_credentials(identity.username, data.current_password)
        if verified_identity is None or verified_identity[0] != identity.user_id:
            raise HTTPException(status_code=401, detail="Current password incorrect")
        if not data.new_password:
            raise HTTPException(status_code=422, detail="New password required")
        db = await get_db()
        try:
            # session_version bump invalidates every previously-issued
            # cookie for this account, including whatever session made
            # this very request -- appropriate for a password change
            # (security-review finding: this used to leave old sessions
            # valid until their 30-day expiry regardless).
            await db.execute(
                "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                (_hash_password(data.new_password), identity.user_id),
            )
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.get("/auth/tokens")
    async def list_own_tokens(request: Request):
        identity = await _require_account_identity(request)
        db = await get_db()
        try:
            rows = await (
                await db.execute(
                    "SELECT id, label, created_at, last_used_at FROM api_tokens "
                    "WHERE user_id = ? ORDER BY created_at, id",
                    (identity.user_id,),
                )
            ).fetchall()
        finally:
            await db.close()
        return [dict(row) for row in rows]

    @app.post("/auth/tokens")
    async def create_own_token(request: Request, data: CreateTokenIn):
        identity = await _require_account_identity(request)
        await _require_step_up(identity, data.current_password)
        label = data.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="Label required")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        db = await get_db()
        try:
            # The project does not enable SQLite foreign keys. Serialize
            # against account deletion and insert only while the exact
            # authenticated account version still exists; otherwise a user
            # deleted between step-up verification and this write could
            # leave an orphaned credential row.
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "INSERT INTO api_tokens (user_id, label, token_hash, created_at) "
                "SELECT id, ?, ?, ? FROM users WHERE id = ? AND session_version = ?",
                (
                    label,
                    token_hash,
                    datetime.now(timezone.utc).isoformat(),
                    identity.user_id,
                    identity.session_version,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise HTTPException(status_code=401, detail="Account changed; authenticate again")
            await db.commit()
        finally:
            await db.close()
        # This response is the only time the raw credential is exposed.
        return JSONResponse(
            {"token": raw_token, "label": label},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/auth/tokens/{token_id}/revoke")
    async def revoke_token(request: Request, token_id: int, data: RevokeTokenIn):
        identity = await _require_account_identity(request)
        await _require_step_up(identity, data.current_password)
        db = await get_db()
        try:
            row = await (
                await db.execute("SELECT user_id FROM api_tokens WHERE id = ?", (token_id,))
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Token not found")
            if row["user_id"] != identity.user_id and identity.role != "admin":
                raise HTTPException(status_code=403, detail="Not your token")
            await db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
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

    @app.get("/auth/admin/tokens")
    async def admin_list_all_tokens(request: Request):
        await _require_admin(request)
        db = await get_db()
        try:
            rows = await (
                await db.execute(
                    "SELECT api_tokens.id, api_tokens.label, api_tokens.created_at, "
                    "api_tokens.last_used_at, users.username AS owner FROM api_tokens "
                    "JOIN users ON users.id = api_tokens.user_id "
                    "ORDER BY users.username, api_tokens.created_at, api_tokens.id"
                )
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
                if new_password:
                    # Same session-invalidation reasoning as
                    # change_own_password -- an admin-initiated reset must
                    # revoke the target's existing sessions too. Not a bind
                    # parameter (self-referential expression, no external
                    # value), so appended to the SQL text directly rather
                    # than through the `updates` dict/set_clause machinery
                    # above, which only handles `field = ?` pairs.
                    set_clause += ", session_version = session_version + 1"
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
            # SQLite foreign keys are not enabled in this project, so the
            # REFERENCES declaration cannot cascade. Remove credentials in
            # the same transaction before deleting their owner.
            await db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
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
