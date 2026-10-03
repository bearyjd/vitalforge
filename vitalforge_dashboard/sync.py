from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from shared import garmin_registry
from shared.database import get_db

if TYPE_CHECKING:
    # Annotation only: the registry hands each op its client; nothing here
    # may hold or authenticate one.
    from garminconnect import Garmin

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "2"))
# A person's first scheduled sync after boot re-scans this many days (and so
# does every later one, until a re-scan runs without stopping early).
# run_sync skips a past date only when ALL its metric tables have it, and a
# metric a device never reports never gets a row, so a re-scan is NOT just
# local reads: it can re-fetch most of the window from Garmin (6 reads per
# date, plus one weight-history call per run) under the shared _sync_lock. That is the cost the single-person
# boot backfill always had; it is now paid once per linked person, one tick
# at a time. Later ticks fetch only the recent window.
SYNC_BACKFILL_DAYS = 90
SYNC_INCREMENTAL_DAYS = 3
# Operation errors that make every further read of this run pointless: a
# rejected session fails each of them the same way, and a throttled account
# (a provider 429) gets worse with every extra call.
_TERMINAL_OPERATION_CODES = frozenset({"auth_failed", "rate_limited"})
# Results that end a sync early; none is an error count, and none should be
# overwritten by one.  A failed cold login stops the run under whichever
# code it failed with -- there is no session to continue on -- but only
# ``auth_failed`` asks the person to relink; the rest retry next sync.
_STOPPED_RESULTS = frozenset({"link_required", "auth_failed", "rate_limited", "network", "unknown"})
# Garmin 429 backoff (spec §e.3): a person whose sync was rate limited is left
# alone for BACKOFF_BASE, doubling with each consecutive 429 up to BACKOFF_CAP.
# Persisted in sync_status.backoff_until/backoff_streak so a restart does not
# reset it -- restart loops are how a rate limit turns into a ban.
BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_CAP = timedelta(hours=6)
# What _record_failed_tick stores for a run that raised before it could record
# its own result.
FAILED_TICK_RESULT = "error"


async def has_usable_garmin_link(person_id: int) -> bool:
    """Whether this person can be synced without borrowing another account."""
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT state FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    return row is not None and row["state"] == "linked"


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


def _stop_reason(exc: Exception) -> str | None:
    """The result a failure ends the whole run with, or None to carry on.

    No usable link, a cold login that failed for any reason, and an
    operation the provider rejected as unauthenticated or throttled all make
    every further read pointless (or, for a throttle, actively harmful).

    A failed login is reported by its own bounded code rather than as a
    blanket ``auth_failed``: a transient 403, network error, or throttle
    leaves the link intact and is retried at the next sync, and telling the
    person to relink for one of those would be wrong.
    """
    if isinstance(exc, garmin_registry.GarminNotLinked):
        return "link_required"
    if isinstance(exc, garmin_registry.GarminAuthenticationError):
        return exc.code
    if isinstance(exc, garmin_registry.GarminOperationError) and exc.code in _TERMINAL_OPERATION_CODES:
        return exc.code
    return None


async def _fetch_metric(
    person_id: int, label: str, op: Callable[[Garmin], Any], skipped: list[str]
) -> Any | None:
    """Read one metric; a failed read is skipped so the rest of the date syncs.

    Only a failure that makes every further read pointless propagates (see
    :func:`_stop_reason`).  Anything else is logged by bounded code or type
    name -- never provider text -- and the metric is left for the next sync.
    """
    try:
        return await garmin_registry.call_paced(person_id, op)
    except garmin_registry.GarminOperationError as exc:
        if _stop_reason(exc) is not None:
            raise
        logger.warning("Skipping %s for person %s: Garmin operation failed (%s)", label, person_id, exc.code)
    except (garmin_registry.GarminNotLinked, garmin_registry.GarminAuthenticationError):
        raise
    except Exception as exc:
        logger.warning("Skipping %s for person %s (%s)", label, person_id, type(exc).__name__)
    skipped.append(label)
    return None


async def sync_date(date_str: str, person_id: int) -> int:
    """Pull all metrics from Garmin for a single date and store them.

    Returns how many metrics were skipped because their read failed, so
    ``run_sync`` can still report a partial date honestly.
    """
    skipped: list[str] = []

    # --- Sleep ---
    sleep = await _fetch_metric(person_id, "sleep", lambda client: client.get_sleep_data(date_str), skipped)
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
    summary = await _fetch_metric(
        person_id, "user summary", lambda client: client.get_user_summary(date_str), skipped
    )
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
    hrv = await _fetch_metric(person_id, "hrv", lambda client: client.get_hrv_data(date_str), skipped)
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
    bb = await _fetch_metric(person_id, "body battery", lambda client: client.get_body_battery(date_str), skipped)
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
    stress = await _fetch_metric(person_id, "stress", lambda client: client.get_stress_data(date_str), skipped)
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
    training = await _fetch_metric(
        person_id, "training status", lambda client: client.get_training_status(date_str), skipped
    )
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

    return len(skipped)


async def sync_weight_history(start_date: str, end_date: str, person_id: int) -> None:
    """Pull weight data from Garmin and store in weight_history table."""
    data = await garmin_registry.call_paced(
        person_id, lambda client: client.get_weigh_ins(start_date, end_date)
    )
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


def backoff_delay(streak: int) -> timedelta:
    """How long to leave a person alone after their `streak`-th consecutive 429:
    15 minutes, doubling, capped at 6 hours."""
    if streak < 1:
        raise ValueError(f"backoff streak must be >= 1, got {streak}")
    # Clamp the exponent: the cap is reached at 2**5 and nothing is gained by
    # shifting further.
    return min(BACKOFF_CAP, BACKOFF_BASE * 2 ** min(streak - 1, 16))


def _next_backoff(
    result: str, until: str | None, streak: int | None, now: datetime
) -> tuple[str | None, int | None]:
    """The (backoff_until, backoff_streak) a sync leaves behind.

    Only a 429 is evidence about the throttle, so only it moves the streak
    up. Any other early stop (a network blip, a relink prompt, a rejected
    session) and a run that crashed say nothing about it: the backoff is left
    exactly as it was, so flapping connectivity can neither erase a ban in
    progress nor grow it. A run that got answers (success, or only skipped
    metrics) means the account is not throttled: both are cleared.

    `backoff_until` is TEXT compared against `datetime.now(timezone.utc)
    .isoformat()` by next_person_to_sync, so it must be written in that form.
    """
    if result == "rate_limited":
        streak = (streak or 0) + 1
        return (now + backoff_delay(streak)).isoformat(), streak
    if result in _STOPPED_RESULTS or result == FAILED_TICK_RESULT:
        return until, streak
    return None, None


async def _write_sync_status(person_id: int, started_at: datetime, result: str, days: int) -> None:
    """The one place sync_status is written (run_sync and _record_failed_tick).

    Reads the person's current backoff, derives the next one from `result`,
    and upserts every column in one statement. ON CONFLICT DO UPDATE, not
    INSERT OR REPLACE: REPLACE deletes the row and reinserts it, so every
    column absent from the statement would silently revert to its default.
    Callers hold the dashboard's `_sync_lock` and this service is the only
    writer of these columns, so the read-then-write needs no transaction of
    its own.
    """
    now = datetime.now(timezone.utc)
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT backoff_until, backoff_streak FROM sync_status WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
        until, streak = _next_backoff(
            result, row["backoff_until"] if row else None, row["backoff_streak"] if row else None, now
        )
        await db.execute(
            "INSERT INTO sync_status "
            "(person_id, last_sync_time, last_sync_result, last_sync_days, backoff_until, backoff_streak) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (person_id) DO UPDATE SET "
            "last_sync_time = excluded.last_sync_time, "
            "last_sync_result = excluded.last_sync_result, "
            "last_sync_days = excluded.last_sync_days, "
            "backoff_until = excluded.backoff_until, "
            "backoff_streak = excluded.backoff_streak",
            (person_id, started_at.isoformat(), result, days, until, streak),
        )
        await db.commit()
    finally:
        await db.close()
    if result == "rate_limited":
        logger.warning("Garmin rate limited person %s; backing off until %s (streak %s)", person_id, until, streak)


async def run_sync(days: int = 7, *, person_id: int) -> str:
    """Run a full sync for the given number of days back from today."""
    logger.info("Starting sync for person %s, last %d days", person_id, days)
    start_time = datetime.now(timezone.utc)
    result = "success"
    errors = 0

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
            errors += await sync_date(date_str, person_id)
        except garmin_registry.GarminRegistryError as exc:
            # A scheduled run can race an unlink after its initial link
            # check; a token store can stop resuming; the provider can reject
            # or throttle the session mid-run.  Record the bounded state
            # rather than retrying every metric for every requested date --
            # against a throttled account that would only deepen the ban.
            stop = _stop_reason(exc)
            if stop is None:
                logger.warning("Error syncing date %s for person %s (%s)", date_str, person_id, type(exc).__name__)
                errors += 1
                continue
            logger.warning("Stopping sync for person %s: %s", person_id, stop)
            result = stop
            break
        except Exception:
            logger.exception("Error syncing date %s", date_str)
            errors += 1

    # Weight history — fetch as a range.  It reads through call_paced
    # directly, not _fetch_metric, so the terminal codes are handled here.
    if result not in _STOPPED_RESULTS:
        try:
            start_date = (today - timedelta(days=days)).isoformat()
            await sync_weight_history(start_date, today_str, person_id)
        except garmin_registry.GarminRegistryError as exc:
            stop = _stop_reason(exc)
            if stop is None:
                logger.warning("Error syncing weight history for person %s (%s)", person_id, type(exc).__name__)
                errors += 1
            else:
                logger.warning("Weight history skipped for person %s: %s", person_id, stop)
                result = stop
        except Exception as e:
            logger.error("Error syncing weight history: %s", e)
            errors += 1

    if errors and result not in _STOPPED_RESULTS:
        result = f"completed with {errors} errors"

    # Update sync status
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info("Sync completed in %.1fs — %s", elapsed, result)

    await _write_sync_status(person_id, start_time, result, days)

    return result


class SyncRegistry:
    """Which persons have a sync in flight -- queued or running.

    `/api/sync/status` answers its person-scoped `syncing` field from this, and
    `POST /api/sync` refuses a second run from it. Neither can be answered from
    the shared `_sync_lock`: that lock is module-level, so `locked()` is true
    whenever ANY person is syncing, which leaked one household member's
    activity to another and refused a sync to someone who had started nothing.

    **Every writer must register here**, not just the manual trigger. Both this
    loop and `/api/sync` take the same lock, so a status endpoint that only
    knew about one of them would report "not syncing" through the whole
    90-day boot backfill -- and the dashboard's poll, which stops as soon as
    `syncing` goes false, would announce a finished sync that never ran.

    Reference-counted rather than a set, because the two writers can overlap:
    the manual trigger registers before creating its task, so a scheduled sync
    that registers the same person while that task waits on the lock would --
    with a plain set -- have its own `discard` clear the flag out from under
    the still-running manual sync. Mutated only from the event loop, so it
    needs no lock of its own.
    """

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}

    def __contains__(self, person_id: int) -> bool:
        return person_id in self._counts

    def acquire(self, person_id: int) -> None:
        self._counts[person_id] = self._counts.get(person_id, 0) + 1

    def release(self, person_id: int) -> None:
        remaining = self._counts.get(person_id, 0) - 1
        if remaining > 0:
            self._counts[person_id] = remaining
        else:
            self._counts.pop(person_id, None)


async def next_person_to_sync(now_iso: str) -> int | None:
    """The linked person whose sync is most overdue, or None.

    Spec §e.3's derived cursor: no stored rotation state, just the oldest
    `sync_status.last_sync_time` (a person with no row goes first, ties by
    id), so it survives restarts and self-heals as persons are added,
    archived, linked or unlinked. The link predicate is
    `has_usable_garmin_link`'s (`state = 'linked'`): the retired
    `legacy_bound`/`legacy_disabled` states are never synced from. A person
    inside `backoff_until` is skipped until it passes.

    Both comparisons are TEXT comparisons, which equal time order only
    because every writer stores `datetime.now(timezone.utc).isoformat()`
    (a `+00:00` suffix). A future backoff_until writer must use the same
    form -- a `Z` suffix or a naive timestamp would compare wrongly.
    """
    db = await get_db()
    try:
        row = await (
            await db.execute(
                """
                SELECT p.id
                FROM persons p
                JOIN garmin_links g ON g.person_id = p.id AND g.state = 'linked'
                LEFT JOIN sync_status s ON s.person_id = p.id
                WHERE p.archived_at IS NULL
                  AND (s.backoff_until IS NULL OR s.backoff_until <= ?)
                ORDER BY s.last_sync_time IS NOT NULL, s.last_sync_time ASC, p.id ASC
                LIMIT 1
                """,
                (now_iso,),
            )
        ).fetchone()
    finally:
        await db.close()
    return row["id"] if row else None


async def _record_failed_tick(person_id: int, started_at: datetime, days: int) -> None:
    """Advance a person whose run_sync raised before writing sync_status.

    Without this their last_sync_time never moves, and the oldest-first
    cursor would pick them again on every tick while everyone else starves.
    A crash says nothing about the throttle, so any active backoff is kept.
    """
    await _write_sync_status(person_id, started_at, FAILED_TICK_RESULT, days)


async def _sync_next_person(lock: asyncio.Lock, registry: SyncRegistry, backfilled: set[int]) -> None:
    """One scheduler tick: sync the most overdue linked person, if any.

    The person is chosen INSIDE the lock, so a manual sync that held it
    has already written its sync_status and no longer looks overdue.
    """
    async with lock:
        started_at = datetime.now(timezone.utc)
        person_id = await next_person_to_sync(started_at.isoformat())
        if person_id is None:
            logger.info("Scheduled sync: no linked person is due")
            return
        days = SYNC_INCREMENTAL_DAYS if person_id in backfilled else SYNC_BACKFILL_DAYS
        logger.info("Scheduled sync for person %s, %d days", person_id, days)
        registry.acquire(person_id)
        try:
            result = await run_sync(days=days, person_id=person_id)
        except Exception:
            logger.exception("Scheduled sync failed for person %s", person_id)
            await _record_failed_tick(person_id, started_at, days)
            return
        finally:
            registry.release(person_id)
        # A backfill that stopped early (network, relink needed, a 429) is
        # retried as a backfill rather than demoted to the incremental window,
        # or the rest of its days would never be fetched. After a 429 the
        # person's backoff decides WHEN: they are not picked again until it
        # passes, and run_sync skips the dates it already stored.
        if result not in _STOPPED_RESULTS:
            backfilled.add(person_id)


async def scheduled_sync(lock: asyncio.Lock, registry: SyncRegistry) -> None:
    """Background loop: every SYNC_INTERVAL_HOURS, sync ONE linked person.

    Spec §e.2's round-robin (E2): one person per tick keeps each burst at a
    single person's size however many are linked, so each person's refresh
    interval is SYNC_INTERVAL_HOURS x N. The deployment-wide Garmin call
    permit (shared/garmin_registry) is the backstop for anything that
    overlaps a tick, such as a manual sync.

    The first tick runs at boot. Each person's first scheduled sync after
    boot is a SYNC_BACKFILL_DAYS re-scan, retried while it stops early (a
    429 additionally puts the person in backoff, see _next_backoff); once one
    completes, 3-day runs. At one linked person that is the old boot backfill
    followed by 3-day runs, plus the retry.

    Takes the same lock `/api/sync`'s manual trigger holds during `run_sync`
    (see vitalforge_dashboard/app.py's `_sync_lock`) -- every write goes
    through `upsert()`'s last-writer-wins INSERT OR REPLACE, so without
    shared serialization a manual sync and a scheduled one can interleave and
    let an older pull silently overwrite a newer one.

    `registry` is required, not optional: see SyncRegistry's docstring for what
    a writer that forgets to register looks like from the dashboard.
    """
    backfilled: set[int] = set()
    while True:
        try:
            await _sync_next_person(lock, registry, backfilled)
        except Exception:
            logger.exception("Scheduled sync tick failed")
        await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)
