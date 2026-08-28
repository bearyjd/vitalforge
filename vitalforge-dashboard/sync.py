import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from shared import garmin_client
from shared.database import get_db

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "2"))


async def get_synced_dates(table: str, person_id: int) -> set[str]:
    """Return the set of dates already stored for a given metric table."""
    db = await get_db()
    try:
        cursor = await db.execute(f"SELECT date FROM [{table}] WHERE person_id = ?", (person_id,))
        rows = await cursor.fetchall()
        return {row["date"] for row in rows}
    finally:
        await db.close()


async def upsert(table: str, date: str, person_id: int, **columns):
    """Insert or replace a row in a metric table, scoped to person_id."""
    cols = ["person_id", "date"] + list(columns.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [person_id, date] + list(columns.values())

    db = await get_db()
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO [{table}] ({col_names}) VALUES ({placeholders})",
            values,
        )
        await db.commit()
    finally:
        await db.close()


def _extract_sleep_score(dto: dict, sleep: dict) -> int | None:
    """Extract sleep score from garminconnect response."""
    # New format: sleepScores.overall.value
    scores = dto.get("sleepScores") or sleep.get("sleepScores")
    if isinstance(scores, dict):
        overall = scores.get("overall")
        if isinstance(overall, dict) and overall.get("value") is not None:
            return overall["value"]
    # Legacy format
    return dto.get("overallSleepScoreValue") or sleep.get("overallSleepScoreValue")


async def sync_date(date_str: str, person_id: int):
    """Pull all metrics from Garmin for a single date and store them."""

    # --- Sleep ---
    sleep = garmin_client.get_sleep_data(date_str)
    if sleep and isinstance(sleep, dict):
        # garminconnect wraps sleep data under dailySleepDTO
        dto = sleep.get("dailySleepDTO", sleep)
        if isinstance(dto, dict) and dto.get("sleepTimeSeconds"):
            await upsert(
                "sleep", date_str, person_id,
                duration_seconds=dto.get("sleepTimeSeconds"),
                deep_seconds=dto.get("deepSleepSeconds"),
                light_seconds=dto.get("lightSleepSeconds"),
                rem_seconds=dto.get("remSleepSeconds"),
                awake_seconds=dto.get("awakeSleepSeconds"),
                sleep_score=_extract_sleep_score(dto, sleep),
                avg_spo2=dto.get("averageSpO2Value"),
                avg_respiration=dto.get("averageRespirationValue"),
            )

    # --- User summary (steps, calories, RHR) ---
    summary = garmin_client.get_user_summary(date_str)
    if summary and isinstance(summary, dict):
        rhr = summary.get("restingHeartRate")
        if rhr:
            await upsert("resting_hr", date_str, person_id, value=rhr)

        total_steps = summary.get("totalSteps")
        if total_steps is not None:
            await upsert("steps", date_str, person_id, value=total_steps)

        active_cal = summary.get("activeKilocalories")
        if active_cal is not None:
            await upsert("active_calories", date_str, person_id, value=active_cal)

    # --- HRV ---
    hrv = garmin_client.get_hrv_data(date_str)
    if hrv and isinstance(hrv, dict):
        hrv_summary = hrv.get("hrvSummary", hrv)
        if isinstance(hrv_summary, dict):
            last_night = hrv_summary.get("lastNightAvg")
            if last_night:
                await upsert(
                    "hrv", date_str, person_id,
                    last_night_avg=last_night,
                    last_night_5min_high=hrv_summary.get("lastNight5MinHigh"),
                    weekly_avg=hrv_summary.get("weeklyAvg"),
                    status=hrv_summary.get("status"),
                )

    # --- Body Battery ---
    bb = garmin_client.get_body_battery(date_str)
    if bb:
        entry = bb[0] if isinstance(bb, list) and bb else bb
        if isinstance(entry, dict):
            # New format: compute highest/lowest from bodyBatteryValuesArray
            bb_array = entry.get("bodyBatteryValuesArray", [])
            highest = None
            lowest = None
            if bb_array:
                bb_levels = [item[1] for item in bb_array if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1] is not None]
                if bb_levels:
                    highest = max(bb_levels)
                    lowest = min(bb_levels)

            # Fall back to legacy keys if present
            if highest is None:
                highest = entry.get("bodyBatteryHighestValue")
            if lowest is None:
                lowest = entry.get("bodyBatteryLowestValue")

            if highest is not None:
                await upsert(
                    "body_battery", date_str, person_id,
                    charged=entry.get("charged") or entry.get("bodyBatteryChargedValue"),
                    drained=entry.get("drained") or entry.get("bodyBatteryDrainedValue"),
                    highest=highest,
                    lowest=lowest,
                )

    # --- Stress ---
    stress = garmin_client.get_stress_data(date_str)
    if stress and isinstance(stress, dict):
        # garminconnect uses avgStressLevel / overallStressLevel
        avg_stress = stress.get("avgStressLevel") or stress.get("overallStressLevel")
        if avg_stress is not None:
            await upsert(
                "stress", date_str, person_id,
                avg_level=avg_stress,
                max_level=stress.get("maxStressLevel"),
                rest_duration=stress.get("restStressDuration"),
                low_duration=stress.get("lowStressDuration"),
                medium_duration=stress.get("mediumStressDuration"),
                high_duration=stress.get("highStressDuration"),
            )

    # --- VO2 Max (from training status, since get_max_metrics often returns null) ---
    training = garmin_client.get_training_status(date_str)
    if training and isinstance(training, dict):
        # Extract VO2 Max from training status
        most_recent = training.get("mostRecentVO2Max", {})
        if isinstance(most_recent, dict):
            generic = most_recent.get("generic") or {}
            if isinstance(generic, dict):
                vo2 = generic.get("vo2MaxValue")
                if vo2:
                    await upsert(
                        "vo2max", date_str, person_id,
                        vo2max_value=vo2,
                        fitness_age=generic.get("fitnessAge"),
                    )

        # Extract training load from mostRecentTrainingLoadBalance
        load_balance = training.get("mostRecentTrainingLoadBalance")
        if isinstance(load_balance, dict):
            load_map = load_balance.get("metricsTrainingLoadBalanceDTOMap", {})
            if isinstance(load_map, dict):
                # Use the primary device's data (first entry or the one marked primary)
                for device_id, device_data in load_map.items():
                    if isinstance(device_data, dict):
                        aero_low = device_data.get("monthlyLoadAerobicLow") or 0
                        aero_high = device_data.get("monthlyLoadAerobicHigh") or 0
                        anaerobic = device_data.get("monthlyLoadAnaerobic") or 0
                        total = round(aero_low + aero_high + anaerobic, 1)
                        if total > 0:
                            await upsert(
                                "training_load", date_str, person_id,
                                acute_load=total,
                                chronic_load=None,
                                load_ratio=None,
                            )
                        break  # use first/primary device only

        # Fallback: legacy aggregatedTrainingLoad format
        if not load_balance:
            agg = training.get("aggregatedTrainingLoad") or {}
            acute = training.get("acuteLoad") or (agg.get("acuteLoad") if isinstance(agg, dict) else None)
            if acute is not None:
                await upsert(
                    "training_load", date_str, person_id,
                    acute_load=acute,
                    chronic_load=training.get("chronicLoad") or (agg.get("chronicLoad") if isinstance(agg, dict) else None),
                    load_ratio=training.get("loadRatio") or (agg.get("loadRatio") if isinstance(agg, dict) else None),
                )


async def sync_weight_history(start_date: str, end_date: str, person_id: int):
    """Pull weight data from Garmin and store in weight_history table."""
    data = garmin_client.get_weight_range(start_date, end_date)
    if not data:
        return

    # garminconnect returns {dailyWeightSummaries: [...]}
    weights = data.get("dailyWeightSummaries", data) if isinstance(data, dict) else data
    if not isinstance(weights, list):
        return

    for entry in weights:
        if not isinstance(entry, dict):
            continue

        # New format: summaryDate + latestWeight nested object
        date_val = entry.get("summaryDate") or entry.get("calendarDate") or entry.get("date")
        latest = entry.get("latestWeight", entry)
        if isinstance(latest, dict):
            weight_g = latest.get("weight")
            if isinstance(date_val, (int, float)):
                date_val = datetime.fromtimestamp(date_val / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if date_val and weight_g:
                await upsert(
                    "weight_history", date_val, person_id,
                    weight_grams=weight_g,
                    bmi=latest.get("bmi"),
                    body_fat=latest.get("bodyFat"),
                    body_water=latest.get("bodyWater"),
                    bone_mass_g=latest.get("boneMass"),
                    muscle_mass_g=latest.get("muscleMass"),
                )


async def run_sync(days: int = 7, *, person_id: int):
    """Run a full sync for the given number of days back from today."""
    logger.info("Starting sync for person %s, last %d days", person_id, days)
    start_time = datetime.now(timezone.utc)
    result = "success"
    errors = 0

    garmin_client.authenticate()

    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

    # Determine which dates need syncing per table
    # For incremental: skip dates we already have (except today, always refresh)
    tables = [
        "sleep", "resting_hr", "hrv", "body_battery",
        "stress", "vo2max", "training_load", "steps", "active_calories",
    ]
    existing = {}
    for table in tables:
        existing[table] = await get_synced_dates(table, person_id)

    today_str = today.isoformat()

    for date_str in dates:
        # Check if ALL tables already have this date (and it's not today)
        if date_str != today_str:
            all_present = all(date_str in existing[t] for t in tables)
            if all_present:
                continue

        try:
            await sync_date(date_str, person_id)
        except Exception:
            logger.exception("Error syncing date %s", date_str)
            errors += 1

    # Weight history — fetch as a range
    try:
        start_date = (today - timedelta(days=days)).isoformat()
        await sync_weight_history(start_date, today_str, person_id)
    except Exception as e:
        logger.error("Error syncing weight history: %s", e)
        errors += 1

    if errors:
        result = f"completed with {errors} errors"

    # Update sync status
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info("Sync completed in %.1fs — %s", elapsed, result)

    db = await get_db()
    try:
        # ON CONFLICT DO UPDATE, not INSERT OR REPLACE: REPLACE deletes the
        # row and reinserts it, so every column absent from the statement
        # silently reverts to its default. backoff_until (spec §e, the
        # Garmin 429 backoff) is exactly such a column -- a plain REPLACE
        # here would clear an active backoff on every single sync, which is
        # how a rate limit turns into a ban.
        await db.execute(
            "INSERT INTO sync_status (person_id, last_sync_time, last_sync_result, last_sync_days) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET "
            "last_sync_time = excluded.last_sync_time, "
            "last_sync_result = excluded.last_sync_result, "
            "last_sync_days = excluded.last_sync_days",
            (person_id, start_time.isoformat(), result, days),
        )
        await db.commit()
    finally:
        await db.close()

    return result


async def scheduled_sync(lock: asyncio.Lock):
    """Background loop that syncs every SYNC_INTERVAL_HOURS.

    Takes the same lock `/api/sync`'s manual trigger holds during `run_sync`
    (see vitalforge-dashboard/app.py's `_sync_lock`) -- every write here goes
    through `upsert()`'s last-writer-wins INSERT OR REPLACE, so without
    shared serialization a manual sync and this backfill/scheduled loop can
    interleave and let an older pull silently overwrite a newer one.
    """
    from shared.database import get_primary_person_id

    # Initial backfill of 90 days
    logger.info("Running initial 90-day backfill...")
    try:
        person_id = await get_primary_person_id()
        async with lock:
            await run_sync(days=90, person_id=person_id)
    except Exception as e:
        logger.error("Initial backfill failed: %s", e)

    while True:
        await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)
        try:
            person_id = await get_primary_person_id()
            logger.info("Running scheduled sync...")
            async with lock:
                await run_sync(days=3, person_id=person_id)
        except Exception as e:
            logger.error("Scheduled sync failed: %s", e)
