import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# `shared` is installed as a proper package (see pyproject.toml), so only the
# sibling-module import below still needs a sys.path hack: `sync.py`,
# `recommendations.py`, and `fit_import.py` live next to this file and are
# imported by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fit_import
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

    db = await get_db()
    try:
        # Atomic: the exact-hash check, the near-duplicate check, and the
        # insert (if neither matches) all happen inside one transaction, so
        # two concurrent uploads of the same file can never both observe
        # "no duplicate" and both insert.
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            f"SELECT {columns_sql} FROM activities WHERE file_sha256 = ?",
            (file_hash,),
        )
        existing = await cursor.fetchone()
        duplicate_reason = "exact_duplicate" if existing is not None else None

        if existing is None:
            cursor = await db.execute(
                f"SELECT {columns_sql} FROM activities "
                "WHERE sport IS ? "
                "AND julianday(start_time_utc) >= julianday(?, ?) "
                "AND julianday(start_time_utc) <= julianday(?, ?) "
                "ORDER BY start_time_utc DESC LIMIT 1",
                (
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
                "INSERT INTO activities (start_time_utc, sport, duration_seconds, distance_m, calories, "
                "avg_hr, max_hr, elevation_gain_m, source_format, file_sha256, imported_at, raw_summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
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
            cursor = await db.execute(f"SELECT {columns_sql} FROM activities WHERE id = ?", (row_id,))
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
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT {columns_sql} FROM activities ORDER BY start_time_utc DESC LIMIT ?",
            (limit,),
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
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT {columns_sql}, raw_summary_json FROM activities WHERE id = ?",
            (activity_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(status_code=404, detail="activity not found")

    result = _activity_row_to_dict(row)
    result["raw_summary"] = json.loads(row["raw_summary_json"]) if row["raw_summary_json"] else None
    return result
