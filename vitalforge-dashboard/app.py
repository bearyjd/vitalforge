import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# `shared` is installed as a proper package (see pyproject.toml), so only the
# sibling-module import below still needs a sys.path hack: `sync.py` and
# `recommendations.py` live next to this file and are imported by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from correlations import compute_cell
from recommendations import get_recommendations, get_rules_only
from sync import run_sync, scheduled_sync

from shared.auth import add_auth_routes, bootstrap_first_admin, bootstrap_migrated_token
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

    db = await get_db()
    try:
        series: dict[str, dict[str, float]] = {}
        for name in set(metric_names):
            table, column = METRIC_TABLES[name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] WHERE date >= date('now', ?) ORDER BY date ASC",
                (f"-{days} days",),
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
