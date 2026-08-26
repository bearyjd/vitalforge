import asyncio
import csv
import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# `shared` is installed as a proper package (see pyproject.toml), so only the
# sibling-module import below still needs a sys.path hack: `sync.py` and
# `recommendations.py` live next to this file and are imported by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from shared.database import get_db, init_db
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
        async with _sync_lock:
            await run_sync(days=days)

    asyncio.create_task(_do_sync())
    return {"status": "started", "days": days}


@app.get("/api/sync/status")
async def sync_status():
    """Return last sync time and result."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT last_sync_time, last_sync_result, last_sync_days FROM sync_status WHERE id = 1")
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

    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT date, [{column}] as value FROM [{table}] WHERE date >= date('now', ?) ORDER BY date ASC",
            (f"-{days} days",),
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
        return await compute_readiness()
    except Exception as e:
        logger.error("Readiness scoring failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to compute readiness score")


async def _export_rows(metrics: list[str], days: int):
    """Yield (metric_name, date, value) tuples for the given metrics.

    Reuses get_metrics()'s exact query pattern (same WHERE/ORDER BY clause,
    same NULL-value filtering) against one shared DB connection for the
    whole export, rather than opening/closing a connection per metric.
    `table`/`column` are always looked up from METRIC_TABLES (never taken
    from the raw request), so the f-string interpolation into the SQL
    identifier positions below is safe.
    """
    db = await get_db()
    try:
        for metric_name in metrics:
            table, column = METRIC_TABLES[metric_name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] WHERE date >= date('now', ?) ORDER BY date ASC",
                (f"-{days} days",),
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
        return await get_recommendations(force=refresh)
    except Exception as e:
        logger.error("Recommendations failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations")


@app.get("/api/recommendations/rules-only")
async def api_rules_only():
    """Get rules engine output without LLM."""
    return await get_rules_only()


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


async def _goal_progress(goal: dict) -> GoalProgress | None:
    mapping = METRIC_TABLES.get(goal["metric"])
    if mapping is None:
        # Only reachable if a row's metric predates a since-removed
        # METRIC_TABLES entry -- degrade to no progress rather than 500.
        return None
    table, column = mapping
    return await compute_progress(table, column, goal["target_value"], goal["target_date"])


async def _goal_out(goal: dict) -> GoalOut:
    return GoalOut(**goal, progress=await _goal_progress(goal))


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
    return await _goal_out(goal)


@app.get("/api/goals")
async def list_goals_route(request: Request):
    identity = await require_account_identity(request)
    goals = await list_goals(identity.user_id)
    return [await _goal_out(goal) for goal in goals]


@app.get("/api/goals/{goal_id}")
async def get_goal_route(goal_id: int, request: Request):
    goal = await _owned_goal_or_404(request, goal_id)
    return await _goal_out(goal)


@app.patch("/api/goals/{goal_id}")
async def patch_goal_route(goal_id: int, data: GoalUpdate, request: Request):
    await _owned_goal_or_404(request, goal_id)
    _validate_goal_metric(data.metric)
    updated = await update_goal(goal_id, data)
    return await _goal_out(updated)


@app.delete("/api/goals/{goal_id}")
async def delete_goal_route(goal_id: int, request: Request):
    await _owned_goal_or_404(request, goal_id)
    await delete_goal(goal_id)
    return {"success": True}
