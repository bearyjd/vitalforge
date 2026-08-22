import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.auth import add_auth_routes
from shared.database import get_db, init_db
from shared.garmin_client import authenticate, push_weight

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
    model_config = ConfigDict(extra="forbid")

    weight: float
    unit: str = "lbs"  # kept as a plain str, not a Literal -- see docs/prp/00-design.md SS3.1
    body_fat_pct: float | None = Field(default=None, ge=3.0, le=75.0)
    body_water_pct: float | None = Field(default=None, ge=30.0, le=80.0)
    muscle_pct: float | None = Field(default=None, ge=10.0, le=90.0)
    bone_mass_kg: float | None = Field(default=None, ge=0.5, le=10.0)
    source: Literal["pwa", "bascule", "bridge", "tasker"] | None = None

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
            "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
            "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                round(weight_lbs, 2),
                round(weight_kg, 2),
                weight_grams,
                timestamp,
                synced,
                data.body_fat_pct,
                data.body_water_pct,
                data.muscle_pct,
                data.bone_mass_kg,
                data.source,
            ),
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
    # Composition keys appear only when supplied (docs/prp/00-design.md SS4.4).
    for field_name in ("body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "source"):
        value = getattr(data, field_name)
        if value is not None:
            result[field_name] = value
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
