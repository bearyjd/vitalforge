import logging
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.
from shared.auth import (
    bootstrap_first_admin,
    bootstrap_migrated_token,
    get_current_identity,
    require_person,
)
from shared.auth_routes import add_auth_routes
from shared.database import (
    ensure_primary_person_grant,
    get_db,
    init_db,
)
from shared.garmin_client import (
    authenticate,
)
from shared.persons_admin import add_person_routes
from vitalforge_weight.activity_routes import add_activity_routes
from vitalforge_weight.weight_routes import add_weight_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    # Both services call this independently against the same DB file with
    # no startup ordering between them -- bootstrap_first_admin() is safe
    # under that race itself (see its own docstring), so no coordination
    # is needed here.
    await bootstrap_first_admin()
    # Must follow bootstrap_first_admin(): on a fresh database the migration
    # that creates the primary person runs inside init_db(), before any admin
    # exists to own it. See ensure_primary_person_grant()'s docstring.
    await ensure_primary_person_grant()
    await bootstrap_migrated_token()
    logger.info("Authenticating with Garmin Connect...")
    try:
        authenticate()
    except Exception as e:
        logger.warning("Garmin authentication failed (will retry on first request): %s", e)
    yield

app = FastAPI(title="VitalForge Weight", lifespan=lifespan)

# Auth routes and middleware
add_auth_routes(app)

# BOTH services for the same reason add_auth_routes is: one login covers both,
# so an admin who opened the weight service should not have to switch ports to
# add someone.
add_person_routes(app)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

def _scrub_non_finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {k: _scrub_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_non_finite(v) for v in value]
    return value

@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """FastAPI's default handler JSON-encodes `exc.errors()` verbatim,
    including the rejected `input` value -- but `json.dumps` (Starlette's
    JSONResponse.render, allow_nan=False) rejects NaN/Infinity, which
    `json.loads` (and httpx's/requests' JSON encoders) accept as a
    non-standard extension. A composition value of NaN or Infinity is
    correctly rejected by Field's ge/le bounds, but then crashes this
    handler with a 500 text/plain response instead of returning the
    documented 422 -- silently reclassifying a terminal, don't-retry error
    into a retryable one for the client (docs/prp/00-design.md SS4.5; Phase
    4 adversarial review finding). Scrub non-finite floats out of the error
    payload before encoding so the intended 422 actually reaches the
    client.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": _scrub_non_finite(jsonable_encoder(exc.errors()))},
    )

@app.get("/health")
async def health():
    return {"status": "ok", "service": "vitalforge-weight"}

_NO_PERSONS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VitalForge &mdash; no person available</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Nothing to show yet</h1>
<p>Your account cannot currently reach any person's data, so there is no weight
log to open. An administrator needs to grant you access to a person.</p>
<p><a href="/auth/logout">Sign out</a></p>
</body></html>
"""

async def _reachable_persons(user_id: int | None) -> list[tuple[int, str]]:
    """Active persons this caller may reach, as (id, slug) in stable id order.

    Mirrors shared.auth._identity_and_grant's `archived_at IS NULL` predicate:
    a slug this returns must be one require_person("view") accepts on the very
    next request, or the redirect below would hand the browser a 404.

    Account-bound callers are grant-scoped, ADMINS INCLUDED, and this must
    stay identical to vitalforge_dashboard's `_reachable_persons`. The two
    services share one login, so a landing rule that differs between them
    sends the same person to different places depending on which port they
    opened.

    require_person does let an admin bypass grants, but that bypass is about
    reaching a person they addressed EXPLICITLY. Landing is about preference,
    not capability: applying it here would make the home page 400 (ambiguous)
    for an admin who holds exactly one grant in a three-person household,
    which is the common case rather than an edge one. Spec f.2 also gives
    default_person_id this redirect "and nothing else" -- expanding the
    fallback set by capability is not in it. An admin can still open any
    /p/{slug}/ directly.
    """
    db = await get_db()
    try:
        if user_id is None:
            # Open-access mode (empty users table) holds implicit `own` on
            # everyone, because there are no grants to consult.
            cursor = await db.execute(
                "SELECT id, slug FROM persons WHERE archived_at IS NULL ORDER BY id"
            )
        else:
            cursor = await db.execute(
                # See the identical predicate in vitalforge_dashboard's
                # _reachable_persons: require_person denies an unrecognised
                # grant value, so a join that accepts one would land the
                # browser on a /p/{slug}/ that immediately 404s. The two
                # services must stay identical here -- they share one login.
                "SELECT p.id AS id, p.slug AS slug FROM persons p "
                "JOIN person_grants g ON g.person_id = p.id AND g.user_id = ? "
                "WHERE p.archived_at IS NULL AND g.access IN ('view', 'manage', 'own') "
                "ORDER BY p.id",
                (user_id,),
            )
        return [(row["id"], row["slug"]) for row in await cursor.fetchall()]
    finally:
        await db.close()

async def _default_person_id(user_id: int) -> int | None:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT default_person_id FROM users WHERE id = ?", (user_id,))
        ).fetchone()
    finally:
        await db.close()
    return row["default_person_id"] if row is not None else None

@app.get("/")
async def index(request: Request):
    """Redirect to the caller's own person page.

    `users.default_person_id` builds this redirect and nothing else -- it is
    never an implicit fallback inside a person-scoped data route (design spec
    SSf.2). It is resolved *through* the reachable set rather than
    dereferenced directly, so a default pointing at an archived person, or one
    whose grant was revoked, falls through to the single-person rule instead
    of redirecting to a URL require_person() would 404.

    NULL (or unusable) default means "the single person this caller can
    reach", or 400 if that is ambiguous. Zero reachable persons is not covered
    by the spec and is not a client error either -- a newly created account
    waiting on a grant lands here -- so it renders an explanatory 200 page
    rather than a bare 400.
    """
    identity = await get_current_identity(request)
    if identity is None:
        # auth_middleware normally redirects an unauthenticated browser to the
        # login page before routing gets here; this is the belt-and-braces arm.
        raise HTTPException(status_code=401, detail="Not authenticated")

    reachable = await _reachable_persons(identity.user_id)
    if not reachable:
        return HTMLResponse(_NO_PERSONS_PAGE)

    slug = None
    if identity.user_id is not None:
        default_id = await _default_person_id(identity.user_id)
        if default_id is not None:
            slug = next((s for person_id, s in reachable if person_id == default_id), None)

    if slug is None:
        if len(reachable) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No default person is set and several are available; "
                    "open one directly: " + ", ".join(f"/p/{s}/" for _, s in reachable)
                ),
            )
        slug = reachable[0][1]

    return RedirectResponse(f"/p/{slug}/", status_code=302)

@app.get("/p/{slug}/")
async def person_index(request: Request, slug: str, person_id: int = Depends(require_person("view"))):
    # person_id is unused here -- the Depends IS the authorization, and
    # dropping it would make this page readable by anyone with an account.
    # See the identical call in vitalforge_dashboard: the signature is
    # (request, name, context), and the old (name, {"request": ...}) form is
    # gone in starlette 1.x -- it renders as `unhashable type: 'dict'` from
    # Jinja2's template cache, i.e. a 500 on every page load.
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "person_slug": slug,
            "dashboard_url": os.environ.get("DASHBOARD_URL", ""),
            "default_unit": os.environ.get("DEFAULT_UNIT", "lbs"),
            "tz": os.environ.get("TZ", ""),
        },
    )

# Routes defined outside this module, registered here. Order matches the order
# these were defined in before the split: weight first, then activity.
add_weight_routes(app)
add_activity_routes(app)
