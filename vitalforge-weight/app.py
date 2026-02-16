import sys
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

# Make shared module importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.database import get_db, init_db
from shared.garmin_client import authenticate, push_weight
from shared.auth import add_auth_routes

import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LBS_PER_KG = 2.20462
GRAMS_PER_LB = 453.592
GRAMS_PER_KG = 1000


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    logger.info("Authenticating with Garmin Connect...")
    try:
        authenticate()
    except Exception as e:
        logger.warning("Garmin authentication failed (will retry on first request): %s", e)
    yield


app = FastAPI(title="VitalForge Weight", lifespan=lifespan)

# Auth routes and middleware
add_auth_routes(app)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class WeightIn(BaseModel):
    weight: float
    unit: str = "lbs"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vitalforge-weight"}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "dashboard_url": os.environ.get("DASHBOARD_URL", ""),
        "default_unit": os.environ.get("DEFAULT_UNIT", "lbs"),
        "tz": os.environ.get("TZ", ""),
    })


@app.post("/api/weight")
async def post_weight(data: WeightIn):
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

    # Push to Garmin Connect
    garmin_error = None
    try:
        authenticate()
        push_weight(weight_grams, now)
        synced = 1
    except Exception as e:
        logger.error("Failed to push weight to Garmin: %s", e)
        garmin_error = str(e)
        synced = 0

    # Save to local database
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) VALUES (?, ?, ?, ?, ?)",
            (round(weight_lbs, 2), round(weight_kg, 2), weight_grams, timestamp, synced),
        )
        await db.commit()
    finally:
        await db.close()

    result = {
        "success": True,
        "weight_lbs": round(weight_lbs, 2),
        "weight_kg": round(weight_kg, 2),
        "timestamp": timestamp,
        "synced_to_garmin": bool(synced),
    }
    if garmin_error:
        result["garmin_error"] = garmin_error
    return result


@app.get("/api/weight/recent")
async def get_recent_weights():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, weight_lbs, weight_kg, timestamp, synced_to_garmin FROM weight_log ORDER BY timestamp DESC LIMIT 10"
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


@app.get("/api/weight/trend")
async def get_weight_trend():
    """Return last 30 days of weights for the trend chart."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT weight_lbs, weight_kg, timestamp FROM weight_log WHERE timestamp >= datetime('now', '-30 days') ORDER BY timestamp ASC"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return [
        {"weight_lbs": row["weight_lbs"], "weight_kg": row["weight_kg"], "timestamp": row["timestamp"]}
        for row in rows
    ]


@app.delete("/api/weight/{weight_id}")
async def delete_weight(weight_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM weight_log WHERE id = ?", (weight_id,))
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Weight entry not found")
    finally:
        await db.close()

    return {"success": True, "deleted_id": weight_id}
