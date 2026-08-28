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

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
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
from sync import run_sync, scheduled_sync

from shared.auth import add_auth_routes, bootstrap_first_admin, bootstrap_migrated_token, require_account_identity
from shared.database import ensure_primary_person_grant, get_db, get_primary_person_id, init_db
from shared.garmin_client import authenticate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Track whether a sync is currently running
_sync_lock = asyncio.Lock()

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
    sync_task = asyncio.create_task(scheduled_sync(_sync_lock))
    yield
    sync_task.cancel()


app = FastAPI(title="VitalForge Dashboard", lifespan=lifespan)

# Auth routes and middleware (must be added before other routes)
add_auth_routes(app)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vitalforge-dashboard"}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "weight_url": os.environ.get("WEIGHT_URL", ""),
        "default_unit": os.environ.get("DEFAULT_UNIT", "lbs"),
        "tz": os.environ.get("TZ", ""),
    })


@app.post("/api/sync")
async def trigger_sync(days: int = Query(default=7, ge=1, le=90)):
    """Trigger a manual data sync."""
    if _sync_lock.locked():
        return {"status": "already_running", "message": "A sync is already in progress"}

    async def _do_sync():
        person_id = await get_primary_person_id()
        async with _sync_lock:
            await run_sync(days=days, person_id=person_id)

    asyncio.create_task(_do_sync())
    return {"status": "started", "days": days}


@app.get("/api/sync/status")
async def sync_status():
    """Return last sync time and result."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT last_sync_time, last_sync_result, last_sync_days FROM sync_status WHERE person_id = ?",
            (person_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        return {"last_sync_time": None, "last_sync_result": "never", "syncing": _sync_lock.locked()}

    return {
        "last_sync_time": row["last_sync_time"],
        "last_sync_result": row["last_sync_result"],
        "last_sync_days": row["last_sync_days"],
        "syncing": _sync_lock.locked(),
    }


@app.get("/api/metrics/{metric_name}")
async def get_metrics(metric_name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return time series data for a metric with 7-day moving average."""
    if metric_name not in METRIC_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric '{metric_name}'. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )

    table, column = METRIC_TABLES[metric_name]
    person_id = await get_primary_person_id()

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


@app.get("/api/readiness")
async def api_readiness():
    """Get the composite readiness/recovery score (0-100)."""
    try:
        person_id = await get_primary_person_id()
        return await compute_readiness(person_id)
    except Exception as e:
        logger.error("Readiness scoring failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to compute readiness score")


async def _export_rows(metrics: list[str], days: int):
    """Yield (metric_name, date, value) tuples for the given metrics.

    Reuses get_metrics()'s exact query pattern (same WHERE/ORDER BY clause,
    same NULL-value filtering, same person_id scoping) against one shared DB
    connection for the whole export, rather than opening/closing a
    connection per metric. `table`/`column` are always looked up from
    METRIC_TABLES (never taken from the raw request), so the f-string
    interpolation into the SQL identifier positions below is safe.
    """
    person_id = await get_primary_person_id()
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


async def _export_csv(metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as CSV text chunks."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["metric", "date", "value"] if include_metric_column else ["date", "value"])
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    async for metric_name, date, value in _export_rows(metrics, days):
        writer.writerow([metric_name, date, value] if include_metric_column else [date, value])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


async def _export_json(metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as a JSON array, one object per row."""
    first = True
    yield "["
    async for metric_name, date, value in _export_rows(metrics, days):
        if not first:
            yield ","
        first = False
        record = {"date": date, "value": value}
        if include_metric_column:
            record = {"metric": metric_name, **record}
        yield json.dumps(record)
    yield "]"


@app.get("/api/export")
async def export_data(
    metric: str = Query(default="all"),
    days: int = Query(default=30, ge=1, le=365),
    format: str = Query(default="csv"),
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
        generator = _export_csv(metrics_to_export, days, include_metric_column)
        media_type = "text/csv"
    else:
        generator = _export_json(metrics_to_export, days, include_metric_column)
        media_type = "application/json"

    return StreamingResponse(
        generator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/recommendations")
async def api_recommendations(refresh: bool = Query(default=False)):
    """Get AI-powered health recommendations."""
    try:
        person_id = await get_primary_person_id()
        return await get_recommendations(person_id, force=refresh)
    except Exception as e:
        logger.error("Recommendations failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations")


@app.get("/api/recommendations/rules-only")
async def api_rules_only():
    """Get rules engine output without LLM."""
    person_id = await get_primary_person_id()
    return await get_rules_only(person_id)


@app.get("/api/correlations")
async def api_correlations(
    metrics: str = Query(..., description="Comma-separated metric names, e.g. sleep_duration,hrv"),
    days: int = Query(default=30, ge=1, le=365),
    lag: int = Query(default=0, ge=-365, le=365, description="Calendar days to shift each row metric forward before joining"),
    min_pairs: int = Query(default=5, ge=2, description="Minimum aligned pairs required to report r instead of null"),
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

    person_id = await get_primary_person_id()

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


@app.post("/api/import/activity")
async def import_activity(file: UploadFile = File(...)):
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
    person_id = await get_primary_person_id()

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


@app.get("/api/activities")
async def list_activities(limit: int = Query(default=50, ge=1, le=200)):
    """List imported activities, most recent first."""
    columns_sql = ", ".join(_ACTIVITY_COLUMNS)
    person_id = await get_primary_person_id()
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


@app.get("/api/activities/{activity_id}")
async def get_activity(activity_id: int):
    """A single imported activity, including its full raw FIT session
    summary."""
    columns_sql = ", ".join(_ACTIVITY_COLUMNS)
    person_id = await get_primary_person_id()
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
    not-found one."""
    identity = await require_account_identity(request)
    goal = await get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    if goal["user_id"] != identity.user_id and identity.role != "admin":
        raise HTTPException(status_code=403, detail="Not your goal")
    return goal


@app.post("/api/goals", status_code=201)
async def create_goal_route(data: GoalCreate, request: Request):
    identity = await require_account_identity(request)
    _validate_goal_metric(data.metric)
    goal_id = await create_goal(identity.user_id, data)
    goal = await get_goal(goal_id)
    person_id = await get_primary_person_id()
    return await _goal_out(goal, person_id)


@app.get("/api/goals")
async def list_goals_route(request: Request):
    identity = await require_account_identity(request)
    goals = await list_goals(identity.user_id)
    person_id = await get_primary_person_id()
    return [await _goal_out(goal, person_id) for goal in goals]


@app.get("/api/goals/{goal_id}")
async def get_goal_route(goal_id: int, request: Request):
    goal = await _owned_goal_or_404(request, goal_id)
    person_id = await get_primary_person_id()
    return await _goal_out(goal, person_id)


@app.patch("/api/goals/{goal_id}")
async def patch_goal_route(goal_id: int, data: GoalUpdate, request: Request):
    await _owned_goal_or_404(request, goal_id)
    _validate_goal_metric(data.metric)
    updated = await update_goal(goal_id, data)
    person_id = await get_primary_person_id()
    return await _goal_out(updated, person_id)


@app.delete("/api/goals/{goal_id}")
async def delete_goal_route(goal_id: int, request: Request):
    await _owned_goal_or_404(request, goal_id)
    await delete_goal(goal_id)
    return {"success": True}
