import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.
from shared.auth import (
    add_auth_routes,
    bootstrap_first_admin,
    bootstrap_migrated_token,
    get_current_identity,
    require_person,
)
from shared.database import ensure_primary_person_grant, get_db, init_db
from shared.garmin_client import authenticate, push_weight
from shared.persons_admin import add_person_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LBS_PER_KG = 2.20462
GRAMS_PER_LB = 453.592
GRAMS_PER_KG = 1000


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
# Person-collection admin (/api/persons, /auth/admin/persons). Registered on
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


class WeightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: float
    unit: str = "lbs"  # kept as a plain str, not a Literal -- see docs/prp/00-design.md SS3.1
    body_fat_pct: float | None = Field(default=None, ge=3.0, le=75.0)
    body_water_pct: float | None = Field(default=None, ge=30.0, le=80.0)
    muscle_pct: float | None = Field(default=None, ge=10.0, le=90.0)
    bone_mass_kg: float | None = Field(default=None, ge=0.5, le=10.0)
    source: Literal["pwa", "bascule", "bridge", "tasker"] | None = None

    @field_validator("weight", "body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", mode="before")
    @classmethod
    def _reject_bool(cls, value):
        # bool is a subclass of int in Python, so Pydantic's lax float mode
        # otherwise silently coerces JSON true/false to 1.0/0.0 -- which
        # bone_mass_kg's 0.5-10.0 kg bound doesn't exclude (Phase 4
        # adversarial review finding: `bone_mass_kg: true` reached the DB
        # and the Garmin FIT payload as a measured 1kg bone mass).
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value

    @model_validator(mode="after")
    def _validate_weight_bounds(self):
        unit = self.unit.lower()
        if unit not in ("lbs", "kg"):
            return self  # the route's own check produces the legacy 400
        weight_kg = self.weight if unit == "kg" else self.weight / LBS_PER_KG
        if not (2.0 <= weight_kg <= 500.0):
            raise ValueError("weight must be between 2 and 500 kg after unit conversion")
        return self


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
    stay identical to vitalforge-dashboard's `_reachable_persons`. The two
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
                # See the identical predicate in vitalforge-dashboard's
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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "person_slug": slug,
        "dashboard_url": os.environ.get("DASHBOARD_URL", ""),
        "default_unit": os.environ.get("DEFAULT_UNIT", "lbs"),
        "tz": os.environ.get("TZ", ""),
    })


DEDUP_WEIGHT_TOLERANCE_GRAMS = 50
DEDUP_WINDOW_SECONDS = 60
COMPOSITION_FIELDS = ("body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg")
# Same first-write-wins-or-conflict treatment as COMPOSITION_FIELDS, plus
# `source`. Kept separate from COMPOSITION_FIELDS (which also names exactly
# what _push_composition forwards to Garmin) so that boundary stays
# explicit; `source` has no Garmin analog and was previously excluded from
# enrichment entirely, so a row's provenance label could permanently
# misattribute composition data actually added by a different, later
# client (Phase 4 adversarial review finding).
ENRICHABLE_FIELDS = (*COMPOSITION_FIELDS, "source")


def _push_composition(weight_grams: int, timestamp: datetime, composition: dict) -> str | None:
    """Push weight + composition to Garmin; returns an error string, or None
    on success. Never raises -- callers decide what to do with the row."""
    try:
        authenticate()
        muscle_pct = composition.get("muscle_pct")
        muscle_mass_kg = (weight_grams / 1000.0) * muscle_pct / 100 if muscle_pct is not None else None
        push_weight(
            weight_grams,
            timestamp,
            percent_fat=composition.get("body_fat_pct"),
            percent_hydration=composition.get("body_water_pct"),
            muscle_mass_kg=muscle_mass_kg,
            bone_mass_kg=composition.get("bone_mass_kg"),
        )
        return None
    except Exception as e:
        logger.error("Failed to push weight to Garmin: %s", e)
        return str(e)


@app.post("/p/{slug}/api/weight")
async def post_weight(data: WeightIn, person_id: int = Depends(require_person("manage"))):
    unit = data.unit.lower()
    if unit not in ("lbs", "kg"):
        raise HTTPException(status_code=400, detail="unit must be 'lbs' or 'kg'")

    if unit == "lbs":
        weight_lbs = data.weight
        weight_kg = data.weight / LBS_PER_KG
    else:
        weight_kg = data.weight
        weight_lbs = data.weight * LBS_PER_KG

    weight_grams = round(weight_kg * GRAMS_PER_KG)
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    # Atomic: read for a duplicate and (if any) write inside one transaction,
    # so two concurrent requests can never both observe "no duplicate". The
    # Garmin push happens after COMMIT, outside the lock -- see
    # docs/prp/00-design.md SS3.7 for why (no timeout mechanism exists to
    # bound the call otherwise, and it is synchronous).
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        # `timestamp >= ?` is a sargable prefilter, not the authoritative
        # bound -- plain string comparison is NOT reliably safe here despite
        # every row coming from this same route's own
        # `datetime.now(timezone.utc).isoformat()`: isoformat() omits the
        # fractional part entirely when microseconds are exactly 0
        # ("...11+00:00" vs "...11.482913+00:00"), and '.' (0x2e) sorts
        # after '+' (0x2b), so a zero-microsecond row can sort BEFORE a
        # same-second fractional one -- the format is neither fixed-width
        # nor zero-padded (Phase 4 devil's-advocate review finding,
        # verified: `sorted(["...11+00:00", "...11.482913+00:00"])` puts
        # the fractional one first). This prefilter is still correct only
        # because it's 1s wider than the authoritative window
        # (DEDUP_WINDOW_SECONDS + 1, below) -- that one second of slack
        # absorbs the entire sub-second ordering error, so this clause can
        # never exclude a row the authoritative ABS(julianday()) clause
        # below would accept. Do not narrow that `+ 1` on the strength of
        # this comment's format claim -- it's the slack, not the format,
        # that makes the prefilter safe. That clause is what
        # idx_weight_log_timestamp cannot use directly (wrapping the column
        # in julianday() makes the index unusable for range pruning).
        #
        # The authoritative window is symmetric (+-60s around this request's
        # own `now`), not one-sided ending exactly at `now` -- an earlier
        # version bounded it as [now-60s, now], which (2026-08-22) turned out
        # to silently defeat dedup for genuinely-concurrent requests: two
        # requests each capture their own `now` microseconds apart, and
        # whichever captured the earlier `now` would run a query whose upper
        # bound excluded the other's already-committed row, since that row's
        # timestamp was technically "after" its own `now` snapshot -- both
        # would then see no duplicate and both would insert (see
        # tests/test_dedup_concurrency.py's repro in the same commit). A
        # symmetric window fixes that while still rejecting the case it was
        # originally added for -- a wildly clock-skewed poison row (e.g.
        # minutes or years off) is still far outside +-60s either direction.
        #
        # Bounds use julianday's own `'-60 seconds'`/`'+60 seconds'` modifier
        # arithmetic against `now`, not `ABS(julianday(a) - julianday(b))`:
        # subtracting two independently-rounded Julian day floats (each
        # ~2.46M with ~15-17 significant digits of double precision) loses
        # enough precision that two timestamps exactly 60.000000s apart can
        # compute a difference a few dozen microseconds *above* 60s roughly
        # 9% of the time (confirmed empirically, 200k trials) -- silently
        # excluding a legitimate boundary duplicate from dedup. Comparing
        # against SQLite's own offset computation instead avoids the
        # subtraction/cancellation entirely (0/200k failures). Caught by
        # Codex review the same day the symmetric-window fix landed.
        sargable_cutoff = (now - timedelta(seconds=DEDUP_WINDOW_SECONDS + 1)).isoformat()
        cursor = await db.execute(
            "SELECT id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
            "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source "
            "FROM weight_log "
            "WHERE person_id = ? "
            "AND timestamp >= ? "
            "AND ABS(weight_grams - ?) <= ? "
            "AND julianday(timestamp) >= julianday(?, ?) "
            "AND julianday(timestamp) <= julianday(?, ?) "
            "ORDER BY timestamp DESC LIMIT 1",
            (
                person_id,
                sargable_cutoff,
                weight_grams,
                DEDUP_WEIGHT_TOLERANCE_GRAMS,
                timestamp,
                f"-{DEDUP_WINDOW_SECONDS} seconds",
                timestamp,
                f"+{DEDUP_WINDOW_SECONDS} seconds",
            ),
        )
        existing = await cursor.fetchone()

        updates = {}
        conflicts = []
        if existing is not None:
            for field in ENRICHABLE_FIELDS:
                incoming_value = getattr(data, field)
                if incoming_value is None:
                    continue
                existing_value = existing[field]
                if existing_value is None:
                    updates[field] = incoming_value
                elif existing_value != incoming_value:
                    conflicts.append(field)

        if existing is None:
            cursor = await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
                "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    round(weight_lbs, 2),
                    round(weight_kg, 2),
                    weight_grams,
                    timestamp,
                    data.body_fat_pct,
                    data.body_water_pct,
                    data.muscle_pct,
                    data.bone_mass_kg,
                    data.source,
                ),
            )
            row_id = cursor.lastrowid
        elif updates:
            set_clause = ", ".join(f"{field} = ?" for field in updates)
            await db.execute(
                f"UPDATE weight_log SET {set_clause} WHERE id = ?",
                (*updates.values(), existing["id"]),
            )
            row_id = existing["id"]
        else:
            row_id = existing["id"]

        if conflicts:
            logger.warning("Weight POST conflicts with stored row %s on fields: %s", row_id, conflicts)

        await db.commit()

        # `updates` can now be source-only (ENRICHABLE_FIELDS includes
        # `source`, which has no Garmin analog) -- only an actual
        # composition change should trigger a re-push or touch
        # synced_to_garmin. Gating those two on plain `updates` re-pushed
        # unchanged composition data on every source-only enrich and could
        # flip a previously-true synced_to_garmin to a permanently stale
        # false if that incidental push ever failed (fix-review finding on
        # the ENRICHABLE_FIELDS change above).
        composition_changed = any(field in COMPOSITION_FIELDS for field in updates)

        # Push happens outside the transaction (see comment above); this
        # connection stays open only to record the outcome afterward. By this
        # point the row (and any enrichment) is already durably committed, so
        # a failure here must never surface as a 500 over already-successful
        # data -- it would tell the client the whole request failed when it
        # didn't. _push_composition itself never raises; this guards the
        # timestamp parse and the flag-update statement around it.
        #
        # Note: push_weight is synchronous and this route awaits nothing
        # during it, so it blocks the whole event loop -- which is also what
        # makes the flag-update below race-free against another request
        # reading this same row's synced_to_garmin mid-push. That's a
        # property of the current single-worker deployment, not something
        # this code enforces; moving the push to a thread/worker pool would
        # reopen a stale-read window here.
        garmin_error = None
        synced = False
        try:
            if existing is None:
                garmin_error = _push_composition(
                    weight_grams,
                    now,
                    {
                        "body_fat_pct": data.body_fat_pct,
                        "body_water_pct": data.body_water_pct,
                        "muscle_pct": data.muscle_pct,
                        "bone_mass_kg": data.bone_mass_kg,
                    },
                )
                synced = garmin_error is None
            elif composition_changed:
                merged = {field: updates.get(field, existing[field]) for field in COMPOSITION_FIELDS}
                # Parsed locally, not left to the outer except below: a row
                # whose timestamp SQLite's own julianday() accepted (so the
                # dedup match above fired) but Python's fromisoformat()
                # can't parse used to raise here and skip the
                # synced_to_garmin flag-update entirely -- the response
                # correctly said `false`, but the stored row kept whatever
                # stale value it already had (Phase 4 adversarial review
                # finding).
                try:
                    original_ts = datetime.fromisoformat(existing["timestamp"])
                except ValueError as e:
                    garmin_error = f"could not parse stored timestamp for Garmin push: {e}"
                else:
                    garmin_error = _push_composition(existing["weight_grams"], original_ts, merged)
                    synced = garmin_error is None
            else:
                synced = bool(existing["synced_to_garmin"])

            if existing is None or composition_changed:
                await db.execute("UPDATE weight_log SET synced_to_garmin = ? WHERE id = ?", (int(synced), row_id))
                await db.commit()
        except Exception as e:
            logger.error("Post-commit sync-flag update failed for row %s: %s", row_id, e)
            if garmin_error is None:
                garmin_error = f"sync status update failed: {e}"
            synced = False
    finally:
        await db.close()

    if existing is None:
        result = {
            "success": True,
            "weight_lbs": round(weight_lbs, 2),
            "weight_kg": round(weight_kg, 2),
            "timestamp": timestamp,
            "synced_to_garmin": synced,
        }
        for field_name in ENRICHABLE_FIELDS:
            value = getattr(data, field_name)
            if value is not None:
                result[field_name] = value
    else:
        result = {
            "success": True,
            "deduplicated": True,
            "id": row_id,
            "weight_lbs": existing["weight_lbs"],
            "weight_kg": existing["weight_kg"],
            "timestamp": existing["timestamp"],
            "synced_to_garmin": synced,
        }
        if updates:
            result["enriched"] = True
        if conflicts:
            result["conflict"] = True
            result["conflict_fields"] = conflicts
    if garmin_error:
        result["garmin_error"] = garmin_error

    return result


@app.get("/p/{slug}/api/weight/recent")
async def get_recent_weights(person_id: int = Depends(require_person("view"))):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, weight_lbs, weight_kg, timestamp, synced_to_garmin FROM weight_log "
            "WHERE person_id = ? ORDER BY timestamp DESC LIMIT 10",
            (person_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return [
        {
            "id": row["id"],
            "weight_lbs": row["weight_lbs"],
            "weight_kg": row["weight_kg"],
            "timestamp": row["timestamp"],
            "synced_to_garmin": bool(row["synced_to_garmin"]),
        }
        for row in rows
    ]


@app.get("/p/{slug}/api/weight/trend")
async def get_weight_trend(person_id: int = Depends(require_person("view"))):
    """Return last 30 days of weights for the trend chart."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT weight_lbs, weight_kg, timestamp FROM weight_log "
            "WHERE person_id = ? AND timestamp >= datetime('now', '-30 days') ORDER BY timestamp ASC",
            (person_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return [
        {"weight_lbs": row["weight_lbs"], "weight_kg": row["weight_kg"], "timestamp": row["timestamp"]}
        for row in rows
    ]


@app.delete("/p/{slug}/api/weight/{weight_id}")
async def delete_weight(weight_id: int, person_id: int = Depends(require_person("manage"))):
    db = await get_db()
    try:
        # The dependency authorizes the caller for this person; `person_id` in
        # the predicate is still what stops an id belonging to a *different*
        # person being deleted through a slug the caller does hold.
        cursor = await db.execute(
            "DELETE FROM weight_log WHERE id = ? AND person_id = ?", (weight_id, person_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Weight entry not found")
    finally:
        await db.close()

    return {"success": True, "deleted_id": weight_id}
