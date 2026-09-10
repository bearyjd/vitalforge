"""Registration of the /auth/* routes.

Split out of shared/auth.py so that file stays about sessions, identity and
authorization primitives. This module is the routing layer over them.

WHY `auth._authenticate_credentials` AND `auth._require_step_up` ARE REACHED
THROUGH THE MODULE while everything else is imported by name: tests monkeypatch
those two on `shared.auth` (tests/test_api_tokens.py, tests/test_user_management.py).
A `from shared.auth import _require_step_up` here would bind the function at
import time, and patching shared.auth afterwards would not reach this binding --
the patch would silently do nothing and the test would pass having exercised the
real code path. Nothing else in this module is patched by any test, which
tests/test_auth_split.py pins so the distinction cannot rot.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone

import aiosqlite
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared import auth
from shared.auth import (
    _COOKIE_NAME,
    _MAX_AGE,
    _RESERVED_USERNAMES,
    CreateTokenIn,
    CreateUserIn,
    PasswordChangeIn,
    RevokeTokenIn,
    UpdateUserIn,
    _get_current_identity,
    _hash_password,
    _is_api_path,
    _is_auth_configured,
    _request_is_https,
    _require_account_identity,
    _require_admin,
    create_session_cookie,
    get_current_user,
    require_auth,
)
from shared.auth_pages import ACCOUNT_PAGE_HTML, ADMIN_USERS_PAGE_HTML, LOGIN_PAGE_HTML
from shared.database import get_db

logger = logging.getLogger(__name__)


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
        id_and_version = await auth._authenticate_credentials(username, password)
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
        verified_identity = await auth._authenticate_credentials(identity.username, data.current_password)
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
        await auth._require_step_up(identity, data.current_password)
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
        await auth._require_step_up(identity, data.current_password)
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
            # vitalforge_weight/app.py's dedup transaction.
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
            # REFERENCES declaration cannot cascade. Remove credentials,
            # goals and person access in the same transaction before deleting
            # their owner.
            await db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM goals WHERE user_id = ?", (user_id,))
            # users.id is AUTOINCREMENT, so an orphaned grant left behind here
            # would hand the NEXT account created with a reused id this
            # account's access to someone's health data -- the same
            # username-reuse hazard validate_session's docstring reasons about,
            # one table over.
            await db.execute("DELETE FROM person_grants WHERE user_id = ?", (user_id,))
            # granted_by is the same hazard one step removed: display-only
            # today ("granted by alice"), but a dangling users.id that a later
            # AUTOINCREMENT reuse re-points at a different account is an
            # audit-trail lie. NULL it rather than leave it.
            await db.execute(
                "UPDATE person_grants SET granted_by = NULL WHERE granted_by = ?", (user_id,)
            )
            # users.default_person_id needs nothing: it is a column on the row
            # being deleted, and no other user's copy references this user.
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
            if _is_api_path(path):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return RedirectResponse("/auth/login", status_code=302)

        return await call_next(request)
