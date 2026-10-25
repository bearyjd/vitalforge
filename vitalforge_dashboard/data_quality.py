"""Consistent local metric snapshots shared by quality reports and enriched exports."""

import math
import re
from datetime import date, datetime, timedelta, timezone

from shared.database import get_db
from vitalforge_dashboard.metric_metadata import METRIC_METADATA
from vitalforge_dashboard.metrics import METRIC_TABLES

PRIVATE_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}
STALE_AFTER_DAYS = 3
_SYNC_RESULTS = frozenset({"success", "link_required", "auth_failed", "rate_limited", "network", "unknown", "error"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _timestamp(value: object) -> str | None:
    """Expose only valid, timezone-aware UTC timestamps, never raw database text."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _sync_report(row, now: datetime) -> dict:
    raw_result = row["last_sync_result"] if row else None
    result = raw_result if isinstance(raw_result, str) and raw_result in _SYNC_RESULTS else "unknown"
    if isinstance(raw_result, str) and re.fullmatch(r"completed with [0-9]{1,6} errors", raw_result):
        result = "partial"
    attempted = _timestamp(row["last_sync_time"]) if row else None
    backoff = _timestamp(row["backoff_until"]) if row else None
    status = "unknown"
    if row is None:
        status = "never_attempted"
    elif result in {"success", "partial"}:
        status = result
    elif result != "unknown":
        status = "failed"
    if backoff is not None and datetime.fromisoformat(backoff) > now:
        status = "backoff"
    return {
        "last_attempt_at": attempted,
        "last_result": result,
        "status": status,
        "backoff_until": backoff,
        "metric_outcomes": "unknown",
    }


async def profile_snapshot(person_id: int, days: int, *, include_context: bool = False) -> tuple[dict, dict, dict | None]:
    """Read one SQLite snapshot; source calendar dates are not timezone-converted.

    Query identifiers come exclusively from METRIC_TABLES. Failures propagate
    before a response is built, so a broken database cannot produce a partial
    successful download. The read transaction also includes sync status.
    """
    now = utc_now()
    today = now.date()
    start = today - timedelta(days=days - 1)
    period = {
        "start_date": start.isoformat(), "end_date": today.isoformat(), "days": days,
        "boundaries": "inclusive", "window_timezone": "UTC",
        "observation_date_semantics": (
            "Dates are Garmin source calendar dates; their timezone is unknown. "
            "The selection window ends on today's UTC date, with no conversion of source dates."
        ),
    }
    metrics, data = {}, {}
    context = None
    db = await get_db()
    try:
        await db.execute("BEGIN")
        for name, (table, column) in METRIC_TABLES.items():
            cursor = await db.execute(
                f"SELECT date, [{column}] AS value FROM [{table}] "
                "WHERE person_id = ? AND date >= ? AND date <= ? ORDER BY date",
                (person_id, start.isoformat(), today.isoformat()),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            data[name] = [
                {"date": row["date"], "value": row["value"]} for row in rows
                if _valid_date(row["date"]) is not None
                and isinstance(row["value"], (int, float)) and math.isfinite(row["value"])
            ]
            latest = None
            cursor = await db.execute(
                f"SELECT date FROM [{table}] WHERE person_id = ? AND date <= ? "
                f"AND [{column}] IS NOT NULL AND typeof([{column}]) IN ('integer', 'real') "
                f"AND [{column}] BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 ORDER BY date DESC",
                (person_id, today.isoformat()),
            )
            async for row in cursor:
                latest = _valid_date(row["date"])
                if latest is not None:
                    break
            await cursor.close()
            observed = len(data[name])
            metadata = METRIC_METADATA[name]
            freshness = "unknown"
            if latest is not None and metadata["expected_cadence"] == "daily":
                freshness = "stale" if (today - latest).days > STALE_AFTER_DAYS else "current"
            metrics[name] = {
                **metadata,
                "latest_observation_date": latest.isoformat() if latest else None,
                "observed_days": observed, "missing_days": days - observed,
                "coverage_percent": round(observed / days * 100, 1),
                "availability": "available" if latest else "empty",
                "freshness": freshness,
                "freshness_stale_after_days": STALE_AFTER_DAYS if metadata["expected_cadence"] == "daily" else None,
                "completeness": "complete" if observed == days else "partial" if observed else "empty",
            }
        cursor = await db.execute(
            "SELECT last_sync_time, last_sync_result, backoff_until FROM sync_status WHERE person_id = ?",
            (person_id,),
        )
        sync = _sync_report(await cursor.fetchone(), now)
        await cursor.close()
        if include_context:
            from vitalforge_dashboard.context_store import read_context

            context = await read_context(db, person_id, start.isoformat(), today.isoformat())
    finally:
        await db.close()
    return {
        "schema_version": "1.0", "generated_at": now.isoformat(), "period": period,
        "sync": sync, "metrics": metrics,
        "notes": [
            "Coverage counts calendar days with a non-null stored value; zero is a value. Today may be incomplete.",
            "Missing days do not establish missed measurements, lack of device support, or a failed sync.",
            "Daily observations are stale after more than 3 calendar days; event/snapshot freshness is unknown.",
            "Latest observation may precede the selected period. Observation dates do not prove a new measurement.",
            "Sync time records the last attempt, not the last success. Per-metric sync outcomes are unknown.",
            "Household scheduling and provider failures can delay updates; no per-person refresh interval is inferred.",
        ],
    }, data, context


async def metric_snapshot(person_id: int, days: int) -> tuple[dict, dict]:
    """Compatibility wrapper; ordinary quality reads never query context tables."""
    quality, data, _ = await profile_snapshot(person_id, days)
    return quality, data
