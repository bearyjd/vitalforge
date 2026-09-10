"""Simple cookie-based session auth for VitalForge services."""

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Literal, NamedTuple

import aiosqlite
from fastapi import HTTPException, Request
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

# Person slugs live in shared/slugs.py: "safe as a path segment under
# /p/{slug}/" is a different and larger rule than _RESERVED_USERNAMES' "safe
# as a username", and keeping it out of here lets shared/migrations.py avoid
# importing this module at all.

# Person-scoped API paths, i.e. /p/{slug}/api/... -- the shape Phase 2 moves
# every person-scoped route to. Deliberately NOT anchored to a valid slug:
# auth_middleware runs before routing, so it cannot know whether the slug
# resolves, and a request shaped like an API call must get a machine-readable
# 401 either way. An unknown slug then 404s from the router, after auth.
_PERSON_API_PATH_RE = re.compile(r"^/p/[^/]+/api(?:/|$)")


def _is_api_path(path: str) -> bool:
    """True for paths whose unauthenticated response must be a 401 JSON body
    rather than a 302 to the HTML login page.

    Both shapes count: the root `/api/...` routes, and the `/p/{slug}/api/...`
    routes Phase 2 introduces. Testing only `startswith("/api/")` -- which is
    what this replaced -- silently sends every person-scoped API call to the
    login page instead, and the bearer-token clients README documents
    (Tasker, Bascule) would receive HTML they cannot parse rather than a 401
    with WWW-Authenticate. That break would land the moment the first route
    moved, which is why this ships one PR ahead of the move.
    """
    return path.startswith("/api/") or _PERSON_API_PATH_RE.match(path) is not None

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


async def _require_admin(request: Request) -> "_Identity":
    """require_auth() plus the admin-only role check every /auth/admin/*
    route needs -- was five copies of the same three lines (fix-review
    finding).

    Returns the full identity rather than just the username: the person-admin
    routes in shared/persons_admin.py need the acting admin's `user_id` (the
    creator's automatic `own` grant, and `person_grants.granted_by`), and
    re-resolving the identity a second time in those routes would mean two
    queries where the rest of this module deliberately uses one. Every
    /auth/admin/* caller below discards the return value, so widening it
    changes nothing for them.

    Note this refuses the open-access anonymous sentinel, whose role is None --
    the whole /auth/admin/* surface is closed while the users table is empty.
    That is existing, intentional behaviour, and persons_admin.py mirrors it
    rather than inventing a second superuser story.
    """
    identity = await _get_current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return identity


# Public alias for callers outside this module (e.g. dashboard goal
# ownership checks) that need the full account-bound identity, including
# live role, without reaching into this module's private names.
Identity = _Identity

# Public alias for _require_admin, so shared/persons_admin.py gates the person
# collection with the *same* function every /auth/admin/* route uses rather
# than a second copy of the role check. Same precedent as Identity,
# get_current_identity and require_account_identity below: a private name that
# a sibling module legitimately needs is a missing public accessor, not a
# reason to import the underscore.
require_admin = _require_admin


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


# Public alias for _get_current_identity, for callers that must handle "no
# identity" themselves rather than have it raised at them.
#
# require_account_identity is the wrong tool for a landing page: it 401s the
# open-access anonymous sentinel (user_id is None), and open access on a fresh
# volume is this project's primary development path. Both services' GET /
# routes need the sentinel, and during the Phase 2 sweep both independently
# imported the private name to get it -- two private imports for the same
# reason is a missing public accessor, not two mistakes.
#
# Returns None when auth is configured and the caller presented nothing
# valid; returns _Identity("anonymous", None, None, None) when the users
# table is empty. Callers must distinguish those two cases themselves, which
# is precisely why this is separate from require_account_identity.
get_current_identity = _get_current_identity


# --- person-scoped access control (multi-tenancy Phase 2) ---------------------

# view < manage < own. Compared by rank, so a route asking for "view" is
# satisfied by any higher level.
_ACCESS_ORDER = {"view": 0, "manage": 1, "own": 2}


async def _identity_and_grant(
    request: Request, slug: str
) -> tuple[_Identity | None, str | None, int | None]:
    """Resolve the caller AND their grant on `slug` in ONE query.

    One statement, not two, and bound to the identity that was just
    established: a two-query version (resolve person, then look up the grant)
    leaves a window in which a grant revoked between the two still authorizes
    the request.

    `archived_at IS NULL` is in the query, not in the caller: an archived
    person is unreachable through this dependency by construction. Admin
    routes that must reach archived persons use _require_admin and address by
    id instead.

    LEFT JOIN, so "the slug exists but you have no grant" and "you have a
    grant" are distinguishable here -- both collapse to the same 404 in
    require_person, but only after the anonymous and admin branches have had a
    chance to look at them.

    `slug` is NOT validated against SLUG_RE first, deliberately. It is
    parameterized so there is no injection surface, and rejecting a malformed
    slug with a 422 would create a THIRD observable -- malformed vs
    exists-but-not-yours vs yours -- reintroducing a smaller version of the
    leak the 404-not-403 rule exists to close. An unvalidated slug simply
    matches no row and 404s like everything else. SLUG_RE governs creation,
    which is where the check belongs.

    This opens its own connection, so a person-scoped request costs three in
    total (this, _get_current_identity's, and the route's own). That is the
    documented per-operation convention this codebase uses everywhere rather
    than an oversight -- see CLAUDE.md. SQLite connections are process-local
    and cheap; if it ever matters, the fix is threading one connection through
    the request, which is a change to the whole codebase's pattern and not to
    this function.
    """
    identity = await _get_current_identity(request)
    if identity is None:
        return None, None, None
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT p.id AS person_id, g.access AS access "
                "FROM persons p "
                "LEFT JOIN person_grants g "
                "  ON g.person_id = p.id AND g.user_id = ? "
                "WHERE p.slug = ? AND p.archived_at IS NULL",
                (identity.user_id, slug),
            )
        ).fetchone()
    finally:
        await db.close()
    if row is None:
        return identity, None, None
    return identity, row["access"], row["person_id"]


def require_person(level: str):
    """FastAPI dependency factory: authorize the caller for `slug` at `level`
    and return that person's id.

    This is the ONLY way a request path may obtain a person_id. There is
    deliberately no module-level helper that returns one without authorizing,
    because such a helper is the thing that gets called by mistake --
    get_primary_person_id() was exactly that during Phase 1 and is retired
    from request paths here. It survives only in startup bootstrap
    (ensure_primary_person_grant) and in scheduled_sync, which has no request
    to authorize; Phase 4's round-robin replaces the latter.

    404, NEVER 403, for a missing grant. A 403 would confirm the person
    exists, which leaks household membership to anyone who can guess a name.
    "No such slug" and "no grant" return the same 404 on purpose. The one
    deliberate exception in this design is the ingest token/slug mismatch,
    which IS a 403 -- there the caller demonstrably holds a valid token, so
    saying no leaks nothing.

    The anonymous check precedes the admin check DEFENSIVELY, not because the
    two orders differ today. Verified by mutation: swapping them changes no
    test, because _get_current_identity returns
    _Identity("anonymous", None, None, None) in open-access mode and
    `None == "admin"` is False, so the admin branch falls through either way.

    The ordering becomes load-bearing the moment that stops being true -- if
    the anonymous sentinel ever gains a role, or the admin branch widens to
    something like `role != "user"`. Since no behavioural test can tell the
    two orders apart while role is None, the invariant is pinned directly
    instead: see test_anonymous_sentinel_has_no_role in
    tests/test_require_person.py, which fails if the sentinel gains one and
    points the reader back here.
    """
    if level not in _ACCESS_ORDER:
        raise ValueError(f"unknown access level {level!r}; expected one of {sorted(_ACCESS_ORDER)}")

    async def dependency(request: Request, slug: str) -> int:
        # FastAPI binds `slug` from the QUERY STRING when the route's path has
        # no {slug} placeholder -- silently, and it documents it as
        # `in: query`. Verified: GET /api/x?slug=primary returns 200 through a
        # dependency that was meant to be reachable only under /p/{slug}/.
        # That would make ?slug= a second way to address a person, which is
        # precisely the implicit-fallback path this phase exists to delete.
        # Path params are the only accepted source; anything else is a wiring
        # mistake in this repo, not a client error, so it raises rather than
        # returning a status code.
        if "slug" not in request.scope.get("path_params", {}):
            raise RuntimeError(
                "require_person() is mounted on a route whose path has no {slug} parameter; "
                f"FastAPI bound slug={slug!r} from the query string instead. Person "
                "addressing is by path segment only -- mount the route under /p/{slug}/."
            )
        identity, granted, person_id = await _identity_and_grant(request, slug)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if person_id is None:
            # No such active slug. Same answer as "no grant", deliberately.
            raise HTTPException(status_code=404, detail="Person not found")
        if identity.user_id is None:
            # Open-access mode: anonymous holds implicit `own` on everyone.
            # `granted` is always None here and is not consulted.
            return person_id
        if identity.role == "admin":
            # Matches the admin bypass every /auth/admin/* route already uses.
            # One superuser story, not two.
            return person_id
        # .get(granted, -1), not [granted]: an access value outside the three
        # would otherwise KeyError into a 500, and a 500 is a DIFFERENT
        # observable from a 404 -- it confirms the person exists, which is the
        # exact leak the 404 convention is here to prevent. -1 sorts below
        # every real level, so an unrecognised grant denies. The CHECK
        # constraint on person_grants.access makes this unreachable today;
        # constraints get relaxed by future table rebuilds, and this costs
        # nothing.
        if granted is None or _ACCESS_ORDER.get(granted, -1) < _ACCESS_ORDER[level]:
            raise HTTPException(status_code=404, detail="Person not found")
        return person_id

    return dependency


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
