import asyncio
import csv
import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# `shared` is installed as a proper package (see pyproject.toml), so only the
# sibling-module import below still needs a sys.path hack: `sync.py`,
# `recommendations.py`, and `fit_import.py` live next to this file and are
# imported by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fit_import
from correlations import compute_cell
from goals import (
    GoalCreate,
    GoalOut,
    GoalProgress,
    GoalUpdate,
    compute_progress,
    create_goal,
    delete_goal,
    get_goal,
    list_goals,
    update_goal,
)
from readiness import compute_readiness
from recommendations import get_recommendations, get_rules_only
from sync import SyncRegistry, run_sync, scheduled_sync

from shared.auth import (
    add_auth_routes,
    bootstrap_first_admin,
    bootstrap_migrated_token,
    get_current_identity,
    require_account_identity,
    require_person,
)
from shared.database import (
    ensure_primary_person_grant,
    garmin_credential_person_id,
    get_db,
    init_db,
)
from shared.garmin_client import authenticate
from shared.persons_admin import add_person_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Track whether a sync is currently running
_sync_lock = asyncio.Lock()

# WHICH persons have a sync in flight -- queued or running. See SyncRegistry
# in sync.py for why this exists and why it is reference-counted; it lives
# there rather than here because scheduled_sync must register in it too, and
# sync.py cannot import this module.
#
# Acquired on the REQUEST path before the task is created and released in the
# task's finally, so it covers the queued window as well as the running one --
# a person cannot stack up tasks by clicking twice while someone else's sync
# holds the lock.
#
# Making the LOCK itself per-person is Phase 4: one person's sync still
# serializes everyone's, and that is deliberately left alone here. Only the
# leaked observable is fixed.
_syncing_person_ids = SyncRegistry()

# Strong references to the detached sync tasks. asyncio keeps only a weak one,
# so a task nobody holds can be garbage-collected part-way through a sync.
_inflight_sync_tasks: set[asyncio.Task] = set()

METRIC_TABLES = {
    "sleep_duration": ("sleep", "duration_seconds"),
    "sleep_score": ("sleep", "sleep_score"),
    "resting_hr": ("resting_hr", "value"),
    "hrv": ("hrv", "last_night_avg"),
    "body_battery": ("body_battery", "highest"),
    "body_battery_low": ("body_battery", "lowest"),
    "stress": ("stress", "avg_level"),
    "vo2max": ("vo2max", "vo2max_value"),
    "weight": ("weight_history", "weight_grams"),
    "body_fat": ("weight_history", "body_fat"),
    "body_water": ("weight_history", "body_water"),
    "bone_mass": ("weight_history", "bone_mass_g"),
    "muscle_mass": ("weight_history", "muscle_mass_g"),
    "training_load": ("training_load", "acute_load"),
    "steps": ("steps", "value"),
    "active_calories": ("active_calories", "value"),
}


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
        logger.warning("Garmin authentication failed (will retry on first sync): %s", e)

    # Start background sync scheduler
    sync_task = asyncio.create_task(scheduled_sync(_sync_lock, _syncing_person_ids))
    yield
    sync_task.cancel()


app = FastAPI(title="VitalForge Dashboard", lifespan=lifespan)

# Auth routes and middleware (must be added before other routes)
add_auth_routes(app)
# Person-collection admin (/api/persons, /auth/admin/persons). Registered on
# BOTH services for the same reason add_auth_routes is: one login covers both,
# so an admin who opened the weight service should not have to switch ports to
# add someone.
add_person_routes(app)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vitalforge-dashboard"}


# Shown by GET / when the caller can reach no person at all. Static text on
# purpose -- nothing from the request or the database is interpolated into it,
# so this page can never echo a username or a display name back into HTML.
_NO_REACHABLE_PERSON_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VitalForge — no person available</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 3rem auto; max-width: 34rem; padding: 0 1rem;
         background: #111; color: #eee; line-height: 1.5; }
  a { color: #6cf; }
</style>
</head>
<body>
<h1>Nothing to show yet</h1>
<p>Your account does not have access to anyone's health data, so there is no dashboard to open.</p>
<p>An administrator can grant you access to a person from the admin pages. Once that grant
exists, this page will take you straight to that person's dashboard.</p>
<p><a href="/auth/logout">Sign out</a></p>
</body>
</html>
"""


async def _reachable_persons(request: Request) -> tuple[list[tuple[int, str]], int | None]:
    """Every active person the caller may open, as (id, slug) lowest id first,
    plus their `users.default_person_id` (None for the anonymous sentinel, or
    when the column was never set).

    Mirrors require_person's own reachability rules rather than inventing a
    second answer: archived persons are excluded by construction (an archived
    person is unreachable through the dependency, so redirecting to one would
    be a permanent dead end), and the anonymous sentinel of open-access mode
    -- an empty `users` table, which CLAUDE.md documents as intentional local
    development behaviour -- reaches every active person because there are no
    grants to consult.

    Account-bound callers are grant-scoped, admins included. require_person
    does let an admin bypass grants, but that bypass is about reaching a
    person they addressed explicitly; using it here would make the home page
    400 (ambiguous) for an admin in a multi-person household who holds
    exactly one grant. They can still open /p/{slug}/ directly.

    `get_current_identity` is the identity-or-None accessor, used here
    because it is the only identity resolver that returns the anonymous
    sentinel instead of 401ing on it (require_account_identity, used by the
    goals routes below, deliberately rejects it). Worth promoting to a public
    alias if a second caller ever needs it.
    """
    identity = await get_current_identity(request)
    if identity is None:
        # Unreachable behind auth_middleware, which redirects an unauthenticated
        # browser to the login page before routing. Fail closed anyway.
        raise HTTPException(status_code=401, detail="Not authenticated")

    db = await get_db()
    try:
        if identity.user_id is None:
            cursor = await db.execute(
                "SELECT id, slug FROM persons WHERE archived_at IS NULL ORDER BY id"
            )
        else:
            cursor = await db.execute(
                # The access IN (...) predicate is what makes "mirrors
                # require_person" true rather than nearly true. require_person
                # denies an unrecognised grant value via
                # _ACCESS_ORDER.get(granted, -1); without this predicate the
                # join accepts it, and the two disagree: GET / would redirect
                # to a /p/{slug}/ that then 404s -- a dead end the user cannot
                # clear. person_grants' CHECK constraint makes that value
                # unreachable today, but CHECK constraints are exactly what
                # table rebuilds relax (see CLAUDE.md on _REBUILD_TABLES), and
                # matching the authorization rule costs one line.
                "SELECT p.id AS id, p.slug AS slug FROM persons p "
                "JOIN person_grants g ON g.person_id = p.id AND g.user_id = ? "
                "WHERE p.archived_at IS NULL AND g.access IN ('view', 'manage', 'own') "
                "ORDER BY p.id",
                (identity.user_id,),
            )
        rows = await cursor.fetchall()

        default_person_id = None
        if identity.user_id is not None:
            cursor = await db.execute(
                "SELECT default_person_id FROM users WHERE id = ?", (identity.user_id,)
            )
            row = await cursor.fetchone()
            default_person_id = row["default_person_id"] if row is not None else None
    finally:
        await db.close()

    return [(row["id"], row["slug"]) for row in rows], default_person_id


@app.get("/")
async def index(request: Request):
    """Send the caller to their own person's dashboard.

    `users.default_person_id` builds this redirect and nothing else (design
    spec §f.2) -- it is never an implicit fallback inside a person-scoped data
    route. NULL means "the single person this caller can reach, or 400 if
    ambiguous" (§a.2). Reaching *zero* persons isn't covered by the spec; a
    bare 400 is a dead end for a real user, so that case gets an explanatory
    page instead.

    The default is honoured only if it is still a person the caller can
    reach: one pointing at an archived person or at a since-revoked grant
    falls back to the single-person rule rather than redirecting into a
    permanent 404 the user has no way out of.
    """
    persons, default_person_id = await _reachable_persons(request)
    if not persons:
        return HTMLResponse(_NO_REACHABLE_PERSON_HTML)

    for person_id, slug in persons:
        if person_id == default_person_id:
            return RedirectResponse(f"/p/{slug}/", status_code=302)

    if len(persons) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "More than one person is available and no default is set. "
                "Open one directly at /p/{slug}/, e.g. "
                + ", ".join(f"/p/{slug}/" for _, slug in persons[:5])
            ),
        )

    return RedirectResponse(f"/p/{persons[0][1]}/", status_code=302)


@app.get("/p/{slug}/")
async def person_index(request: Request, slug: str, person_id: int = Depends(require_person("view"))):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "person_slug": slug,
        "weight_url": os.environ.get("WEIGHT_URL", ""),
        "default_unit": os.environ.get("DEFAULT_UNIT", "lbs"),
        "tz": os.environ.get("TZ", ""),
    })


@app.post("/p/{slug}/api/sync")
async def trigger_sync(
    days: int = Query(default=7, ge=1, le=90),
    person_id: int = Depends(require_person("manage")),
):
    """Trigger a manual data sync."""
    # require_person authorized this caller FOR THIS PERSON. It cannot
    # authorize them for the DATA SOURCE, and there is only one: a single
    # module-level Garmin client on deployment-wide credentials. Without this
    # check, a caller holding `manage` on their own person triggers a pull of
    # the primary person's sleep, HRV and heart rate, writes it under theirs,
    # and reads it back at 200 -- every SQL statement correctly person-scoped
    # the whole way. INSERT OR REPLACE on (person_id, date) means it also
    # silently overwrites any real measurement they already had for those
    # dates.
    #
    # 409, not 404: the caller demonstrably holds `manage` on this person, so
    # naming the reason leaks nothing -- the same reasoning that makes the
    # ingest token/slug mismatch a 403 rather than a 404.
    source_person_id = await garmin_credential_person_id()
    if person_id != source_person_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "This person has no Garmin account of their own. The deployment holds one "
                "set of Garmin credentials, which belong to a different person, and syncing "
                "would file their measurements under this one. Per-person Garmin linking "
                "arrives in Phase 3."
            ),
        )

    # THIS person's sync, not _sync_lock.locked(): the lock is module-level, so
    # answering from it told a caller "already running" because somebody ELSE
    # was syncing -- the same cross-person observable that used to leak out of
    # /api/sync/status. A second person's request now queues on the lock
    # instead, which is what the shared lock has always meant.
    if person_id in _syncing_person_ids:
        return {"status": "already_running", "message": "A sync is already in progress"}
    # Added here rather than inside the task so a double-click cannot stack up
    # two tasks during the window where the first is waiting on the lock.
    _syncing_person_ids.acquire(person_id)

    async def _do_sync():
        # person_id is captured from the enclosing scope, never re-resolved in
        # here: this closure runs via create_task after the response has been
        # sent, with no request left to authorize against.
        async with _sync_lock:
            await run_sync(days=days, person_id=person_id)

    # The release is a done-callback rather than a `finally` inside _do_sync,
    # because the acquire happens out here on the request path: a task
    # cancelled BEFORE its first step never enters its own body, so a `finally`
    # there would not run and this person would stay marked as syncing until
    # the process restarts. add_done_callback fires for that case too, which
    # makes the pairing structural instead of positional. (Nothing cancels
    # these tasks today -- the lifespan's cancel targets scheduled_sync -- so
    # this is defence against a future caller, not a live bug.)
    task = asyncio.create_task(_do_sync())
    task.add_done_callback(lambda _t: _syncing_person_ids.release(person_id))
    # asyncio only holds a weak reference to a running task, so a task nobody
    # retains can be garbage-collected mid-flight. Keep a strong reference
    # until it finishes.
    _inflight_sync_tasks.add(task)
    task.add_done_callback(_inflight_sync_tasks.discard)
    return {"status": "started", "days": days}


@app.get("/p/{slug}/api/sync/status")
async def sync_status(person_id: int = Depends(require_person("view"))):
    """Return last sync time and result."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT last_sync_time, last_sync_result, last_sync_days FROM sync_status WHERE person_id = ?",
            (person_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    # _syncing_person_ids, not _sync_lock.locked(): the lock is module-level, so
    # reporting it here would tell every person that "a sync is running"
    # whenever any OTHER person's sync is running -- household activity leaking
    # across an otherwise person-scoped response. See _syncing_person_ids.
    syncing = person_id in _syncing_person_ids

    if not row:
        return {"last_sync_time": None, "last_sync_result": "never", "syncing": syncing}

    return {
        "last_sync_time": row["last_sync_time"],
        "last_sync_result": row["last_sync_result"],
        "last_sync_days": row["last_sync_days"],
        "syncing": syncing,
    }


@app.get("/p/{slug}/api/metrics/{metric_name}")
async def get_metrics(
    metric_name: str,
    days: int = Query(default=30, ge=1, le=365),
    person_id: int = Depends(require_person("view")),
):
    """Return time series data for a metric with 7-day moving average."""
    if metric_name not in METRIC_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric '{metric_name}'. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )

    table, column = METRIC_TABLES[metric_name]

    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT date, [{column}] as value FROM [{table}] "
            f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
            (person_id, f"-{days} days"),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    data = [{"date": row["date"], "value": row["value"]} for row in rows if row["value"] is not None]

    # Compute 7-day moving average
    values = [d["value"] for d in data]
    moving_avg = []
    for i in range(len(values)):
        window = values[max(0, i - 6):i + 1]
        moving_avg.append(round(sum(window) / len(window), 2) if window else None)

    for i, d in enumerate(data):
        d["moving_avg_7d"] = moving_avg[i]

    return {
        "metric": metric_name,
        "days": days,
        "count": len(data),
        "data": data,
    }


@app.get("/p/{slug}/api/readiness")
async def api_readiness(person_id: int = Depends(require_person("view"))):
    """Get the composite readiness/recovery score (0-100)."""
    try:
        return await compute_readiness(person_id)
    except Exception as e:
        logger.error("Readiness scoring failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to compute readiness score")


async def _export_rows(person_id: int, metrics: list[str], days: int):
    """Yield (metric_name, date, value) tuples for the given metrics.

    Reuses get_metrics()'s exact query pattern (same WHERE/ORDER BY clause,
    same NULL-value filtering, same person_id scoping) against one shared DB
    connection for the whole export, rather than opening/closing a
    connection per metric. `table`/`column` are always looked up from
    METRIC_TABLES (never taken from the raw request), so the f-string
    interpolation into the SQL identifier positions below is safe.

    `person_id` is a parameter rather than something resolved in here: this
    generator is consumed by StreamingResponse after export_data() has
    returned, so there is no request scope left to authorize against.
    export_data() takes it from require_person and threads it down.
    """
    db = await get_db()
    try:
        for metric_name in metrics:
            table, column = METRIC_TABLES[metric_name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] "
                f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
                (person_id, f"-{days} days"),
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row["value"] is not None:
                    yield metric_name, row["date"], row["value"]
    except Exception:
        # The StreamingResponse has already sent a 200 and headers by the time
        # a failure happens here, so the client just sees a truncated
        # download with no indication anything went wrong. Log server-side
        # before re-raising so the failure isn't silently lost.
        logger.exception("Export failed mid-stream (metrics=%s, days=%s)", metrics, days)
        raise
    finally:
        await db.close()


async def _export_csv(person_id: int, metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as CSV text chunks."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["metric", "date", "value"] if include_metric_column else ["date", "value"])
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    async for metric_name, date, value in _export_rows(person_id, metrics, days):
        writer.writerow([metric_name, date, value] if include_metric_column else [date, value])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


async def _export_json(person_id: int, metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as a JSON array, one object per row."""
    first = True
    yield "["
    async for metric_name, date, value in _export_rows(person_id, metrics, days):
        if not first:
            yield ","
        first = False
        record = {"date": date, "value": value}
        if include_metric_column:
            record = {"metric": metric_name, **record}
        yield json.dumps(record)
    yield "]"


@app.get("/p/{slug}/api/export")
async def export_data(
    metric: str = Query(default="all"),
    days: int = Query(default=30, ge=1, le=365),
    format: str = Query(default="csv"),
    person_id: int = Depends(require_person("view")),
):
    """Stream metric data as a CSV or JSON file download.

    `metric=all` streams long/tidy `metric,date,value` rows across every
    known metric; a single metric name streams just `date,value`.
    """
    if metric != "all" and metric not in METRIC_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric '{metric}'. Valid: all, {', '.join(sorted(METRIC_TABLES))}",
        )
    if format not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail=f"Unknown format '{format}'. Valid: csv, json")

    metrics_to_export = sorted(METRIC_TABLES) if metric == "all" else [metric]
    include_metric_column = metric == "all"
    filename = f"vitalforge-export-{metric}-{days}d.{format}"

    if format == "csv":
        generator = _export_csv(person_id, metrics_to_export, days, include_metric_column)
        media_type = "text/csv"
    else:
        generator = _export_json(person_id, metrics_to_export, days, include_metric_column)
        media_type = "application/json"

    return StreamingResponse(
        generator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/p/{slug}/api/recommendations")
async def api_recommendations(
    refresh: bool = Query(default=False),
    person_id: int = Depends(require_person("view")),
):
    """Get AI-powered health recommendations."""
    try:
        return await get_recommendations(person_id, force=refresh)
    except Exception as e:
        logger.error("Recommendations failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations")


@app.get("/p/{slug}/api/recommendations/rules-only")
async def api_rules_only(person_id: int = Depends(require_person("view"))):
    """Get rules engine output without LLM."""
    return await get_rules_only(person_id)


@app.get("/p/{slug}/api/correlations")
async def api_correlations(
    metrics: str = Query(..., description="Comma-separated metric names, e.g. sleep_duration,hrv"),
    days: int = Query(default=30, ge=1, le=365),
    lag: int = Query(default=0, ge=-365, le=365, description="Calendar days to shift each row metric forward before joining"),
    min_pairs: int = Query(default=5, ge=2, description="Minimum aligned pairs required to report r instead of null"),
    person_id: int = Depends(require_person("view")),
):
    """Ad-hoc cross-metric correlation matrix.

    Returns a row-major NxN matrix where `cells[i][j]` correlates
    `metrics[i]` (shifted forward `lag` days) against `metrics[j]`
    (unshifted) — see `correlations.align_series` for why that makes the
    matrix asymmetric when `lag != 0`. Every metric name must be a key in
    `METRIC_TABLES`, which only ever contains date-keyed tables — this
    endpoint has no path to `weight_log` (timestamp-keyed) at all.

    Opens exactly one DB connection for the whole request and fetches
    each requested metric's series once, regardless of how many N^2
    cells reuse it.
    """
    metric_names = [m.strip() for m in metrics.split(",") if m.strip()]
    if not metric_names:
        raise HTTPException(status_code=400, detail="metrics parameter must contain at least one metric name")

    unknown = [m for m in metric_names if m not in METRIC_TABLES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric(s): {', '.join(unknown)}. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )

    db = await get_db()
    try:
        series: dict[str, dict[str, float]] = {}
        for name in set(metric_names):
            table, column = METRIC_TABLES[name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] "
                f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
                (person_id, f"-{days} days"),
            )
            rows = await cursor.fetchall()
            series[name] = {row["date"]: row["value"] for row in rows if row["value"] is not None}
    finally:
        await db.close()

    cells = [
        [compute_cell(series[row_name], series[col_name], lag, min_pairs) for col_name in metric_names]
        for row_name in metric_names
    ]

    return {
        "metrics": metric_names,
        "days": days,
        "lag": lag,
        "min_pairs": min_pairs,
        "cells": cells,
    }


# How close two uploads' (sport, start_time_utc) must be to be treated as
# the same activity re-imported (e.g. the same watch export processed
# twice, landing a few seconds apart in wall-clock terms even though the
# file bytes differ slightly). This is the second dedup stage -- the first
# is the exact `file_sha256` match below.
ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS = 120

_ACTIVITY_COLUMNS = (
    "id", "start_time_utc", "sport", "duration_seconds", "distance_m", "calories",
    "avg_hr", "max_hr", "elevation_gain_m", "source_format", "file_sha256", "imported_at",
)


def _activity_row_to_dict(row) -> dict:
    return {col: row[col] for col in _ACTIVITY_COLUMNS}


async def _read_upload_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an UploadFile in bounded chunks, rejecting anything over
    `max_bytes` before it's fully buffered in memory -- trusting
    `Content-Length` alone isn't enough since a client can omit or lie
    about it."""
    chunk_size = 1024 * 1024
    chunks = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"file exceeds {max_bytes} byte upload limit")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/p/{slug}/api/import/activity")
async def import_activity(
    file: UploadFile = File(...),
    person_id: int = Depends(require_person("manage")),
):
    """Import a local FIT activity file. FIT-only for this first slice --
    TCX/GPX are explicitly deferred. Dedup is two-stage and race-free: an
    exact `file_sha256` match, then a (sport, start_time_utc) time-window
    match for near-duplicates, both performed inside one `BEGIN IMMEDIATE`
    transaction so two concurrent uploads of the same file can never both
    pass the check before either commits -- mirrors the fix already applied
    to `vitalforge-weight/app.py`'s weight_log dedup (see that file's
    `post_weight` for the full rationale)."""
    data = await _read_upload_capped(file, fit_import.MAX_UPLOAD_BYTES)

    try:
        record = fit_import.parse_fit_bytes(data)
    except fit_import.FitImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    file_hash = fit_import.compute_file_hash(data)
    imported_at = datetime.now(timezone.utc).isoformat()
    raw_summary_json = json.dumps(record.raw_summary, default=str)

    columns_sql = ", ".join(_ACTIVITY_COLUMNS)

    db = await get_db()
    try:
        # Atomic: the exact-hash check, the near-duplicate check, and the
        # insert (if neither matches) all happen inside one transaction, so
        # two concurrent uploads of the same file can never both observe
        # "no duplicate" and both insert.
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            f"SELECT {columns_sql} FROM activities WHERE person_id = ? AND file_sha256 = ?",
            (person_id, file_hash),
        )
        existing = await cursor.fetchone()
        duplicate_reason = "exact_duplicate" if existing is not None else None

        if existing is None:
            cursor = await db.execute(
                f"SELECT {columns_sql} FROM activities "
                "WHERE person_id = ? "
                "AND sport IS ? "
                "AND julianday(start_time_utc) >= julianday(?, ?) "
                "AND julianday(start_time_utc) <= julianday(?, ?) "
                "ORDER BY start_time_utc DESC LIMIT 1",
                (
                    person_id,
                    record.sport,
                    record.start_time_utc,
                    f"-{ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS} seconds",
                    record.start_time_utc,
                    f"+{ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS} seconds",
                ),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                duplicate_reason = "near_duplicate"

        if existing is not None:
            # Nothing to write -- commit() here is a no-op against the DB
            # but still releases the IMMEDIATE lock, mirroring
            # vitalforge-weight/app.py's post_weight, which also commits
            # unconditionally after its dedup check-then-insert regardless
            # of which branch ran.
            await db.commit()
            row = existing
        else:
            insert_cursor = await db.execute(
                "INSERT INTO activities (person_id, start_time_utc, sport, duration_seconds, distance_m, calories, "
                "avg_hr, max_hr, elevation_gain_m, source_format, file_sha256, imported_at, raw_summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    record.start_time_utc,
                    record.sport,
                    record.duration_seconds,
                    record.distance_m,
                    record.calories,
                    record.avg_hr,
                    record.max_hr,
                    record.elevation_gain_m,
                    record.source_format,
                    file_hash,
                    imported_at,
                    raw_summary_json,
                ),
            )
            row_id = insert_cursor.lastrowid
            await db.commit()
            cursor = await db.execute(
                f"SELECT {columns_sql} FROM activities WHERE id = ? AND person_id = ?",
                (row_id, person_id),
            )
            row = await cursor.fetchone()
    finally:
        await db.close()

    result = _activity_row_to_dict(row)
    if duplicate_reason is not None:
        result["duplicate"] = True
        result["duplicate_reason"] = duplicate_reason
    return result


@app.get("/p/{slug}/api/activities")
async def list_activities(
    limit: int = Query(default=50, ge=1, le=200),
    person_id: int = Depends(require_person("view")),
):
    """List imported activities, most recent first."""
    columns_sql = ", ".join(_ACTIVITY_COLUMNS)
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT {columns_sql} FROM activities "
            "WHERE person_id = ? ORDER BY start_time_utc DESC LIMIT ?",
            (person_id, limit),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return {"count": len(rows), "activities": [_activity_row_to_dict(row) for row in rows]}


@app.get("/p/{slug}/api/activities/{activity_id}")
async def get_activity(activity_id: int, person_id: int = Depends(require_person("view"))):
    """A single imported activity, including its full raw FIT session
    summary."""
    columns_sql = ", ".join(_ACTIVITY_COLUMNS)
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT {columns_sql}, raw_summary_json FROM activities "
            "WHERE id = ? AND person_id = ?",
            (activity_id, person_id),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(status_code=404, detail="activity not found")

    result = _activity_row_to_dict(row)
    result["raw_summary"] = json.loads(row["raw_summary_json"]) if row["raw_summary_json"] else None
    return result


# ---------------------------------------------------------------------------
# Goal / target tracking
#
# Every route below requires an account-bound identity (require_account_identity
# 401s for both "no session" and the auth-not-configured anonymous identity,
# since a goal always belongs to a real user_id). In dev mode with an empty
# users table, this means goal endpoints 401 while every other dashboard
# endpoint stays open -- expected, not a bug: goals inherently need an
# owning account.
# ---------------------------------------------------------------------------

def _validate_goal_metric(metric: str | None):
    if metric is not None and metric not in METRIC_TABLES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown metric '{metric}'. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )


async def _goal_progress(goal: dict, person_id: int) -> GoalProgress | None:
    mapping = METRIC_TABLES.get(goal["metric"])
    if mapping is None:
        # Only reachable if a row's metric predates a since-removed
        # METRIC_TABLES entry -- degrade to no progress rather than 500.
        return None
    table, column = mapping
    return await compute_progress(table, column, person_id, goal["target_value"], goal["target_date"])


async def _goal_out(goal: dict, person_id: int) -> GoalOut:
    return GoalOut(**goal, progress=await _goal_progress(goal, person_id))


async def _owned_goal_or_404(request: Request, goal_id: int) -> dict:
    """404 if the goal doesn't exist, 403 if it exists but belongs to
    someone else and the caller isn't an admin -- mirrors shared/auth.py's
    revoke_token ownership check exactly (existence checked, and only then
    ownership), so a wrong-owner request can never be mistaken for a
    not-found one.

    THE SECOND DELIBERATE 403 IN THE /p/{slug}/ FAMILY, stated here because a
    security review flagged it against the phase's 404-never-403 rule. That
    rule exists to hide PERSON existence: a 403 from require_person would
    confirm a household member by name to anyone who guessed it. Goals are
    USER-owned, not person-owned, so the three observables here (404 absent /
    403 not-yours / 200 yours) leak only that a goal id exists somewhere in
    the account -- they say nothing about which persons exist or who can
    reach them, and require_person still gates the person before any of this
    runs.

    The cost is real but bounded: a caller can enumerate goal ids and count
    other users' goals. Accepted rather than collapsed to 404 because the
    ownership-check shape is this codebase's existing convention for
    user-owned resources (revoke_token), and having one rule for person-scoped
    resources and a different one for account-scoped resources is clearer than
    a single rule with an unexplained exception."""
    identity = await require_account_identity(request)
    goal = await get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    if goal["user_id"] != identity.user_id and identity.role != "admin":
        raise HTTPException(status_code=403, detail="Not your goal")
    return goal


@app.post("/p/{slug}/api/goals", status_code=201)
async def create_goal_route(
    data: GoalCreate,
    request: Request,
    person_id: int = Depends(require_person("view")),
):
    identity = await require_account_identity(request)
    _validate_goal_metric(data.metric)
    goal_id = await create_goal(identity.user_id, data)
    goal = await get_goal(goal_id)
    return await _goal_out(goal, person_id)


@app.get("/p/{slug}/api/goals")
async def list_goals_route(request: Request, person_id: int = Depends(require_person("view"))):
    identity = await require_account_identity(request)
    goals = await list_goals(identity.user_id)
    return [await _goal_out(goal, person_id) for goal in goals]


@app.get("/p/{slug}/api/goals/{goal_id}")
async def get_goal_route(goal_id: int, request: Request, person_id: int = Depends(require_person("view"))):
    goal = await _owned_goal_or_404(request, goal_id)
    return await _goal_out(goal, person_id)


@app.patch("/p/{slug}/api/goals/{goal_id}")
async def patch_goal_route(
    goal_id: int,
    data: GoalUpdate,
    request: Request,
    person_id: int = Depends(require_person("view")),
):
    await _owned_goal_or_404(request, goal_id)
    _validate_goal_metric(data.metric)
    updated = await update_goal(goal_id, data)
    return await _goal_out(updated, person_id)


# Account-scoped, and therefore the one goals route that does NOT move under
# /p/{slug}/: deleting a goal takes no person_id at all (the person only ever
# fed progress computation on the routes above), so there is nothing here for
# require_person to authorize.
@app.delete("/api/goals/{goal_id}")
async def delete_goal_route(goal_id: int, request: Request):
    await _owned_goal_or_404(request, goal_id)
    await delete_goal(goal_id)
    return {"success": True}
