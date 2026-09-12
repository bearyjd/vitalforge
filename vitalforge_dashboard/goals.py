"""Goal / target tracking: CRUD over the `goals` table plus a trend-based ETA.

This module owns which metric names a goal may use: `is_valid_metric` and
`valid_metrics` answer from METRIC_TABLES, and `progress_for_goal` resolves a
stored row's metric itself. That knowledge used to sit in app.py because
METRIC_TABLES lived there and importing app from this sibling would have been
circular; metrics.py exists precisely to break that cycle.

Raising the rejection stays in the route layer -- this module deliberately does
not import fastapi, so app.py asks `is_valid_metric` and owns the HTTP status
and message. compute_progress() still takes an already resolved (table, column)
pair, the same shape recommendations.get_metric expects.
"""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict

from shared.database import get_db
from vitalforge_dashboard.metrics import METRIC_TABLES
from vitalforge_dashboard.recommendations import get_metric, trend_slope


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    target_value: float
    target_date: str | None = None


class GoalUpdate(BaseModel):
    """All fields optional (PATCH semantics). A field explicitly sent as
    null is treated as "leave unchanged", not "clear it" -- metric and
    target_value are NOT NULL columns, and update_goal drops None values
    before building the SQL SET clause, so there is no separate path where
    the two behave differently."""

    model_config = ConfigDict(extra="forbid")

    metric: str | None = None
    target_value: float | None = None
    target_date: str | None = None


class GoalProgress(BaseModel):
    latest_value: float | None
    trend_slope: float | None
    eta_date: str | None
    on_track: bool | None


class GoalOut(BaseModel):
    id: int
    user_id: int
    metric: str
    target_value: float
    target_date: str | None
    created_at: str
    progress: GoalProgress | None = None


_SELECT_COLUMNS = "id, user_id, metric, target_value, target_date, created_at"


def is_valid_metric(metric: str) -> bool:
    """Whether a goal may be stored against `metric`.

    The gate is "can progress be computed for it", so METRIC_TABLES membership
    is the whole definition -- a metric absent from it has no (table, column) to
    read, which is the same reason `progress_for_goal` degrades to None.
    """
    return metric in METRIC_TABLES


def valid_metrics() -> list[str]:
    """Every acceptable metric name, sorted, for callers building an error
    message. Sorted here rather than at each call site so the ordering a client
    sees cannot drift between routes."""
    return sorted(METRIC_TABLES)


async def create_goal(user_id: int, data: GoalCreate) -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO goals (user_id, metric, target_value, target_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, data.metric, data.target_value, data.target_date, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_goals(user_id: int) -> list[dict]:
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                f"SELECT {_SELECT_COLUMNS} FROM goals WHERE user_id = ? ORDER BY created_at, id",
                (user_id,),
            )
        ).fetchall()
    finally:
        await db.close()
    return [dict(row) for row in rows]


async def get_goal(goal_id: int) -> dict | None:
    db = await get_db()
    try:
        row = await (
            await db.execute(f"SELECT {_SELECT_COLUMNS} FROM goals WHERE id = ?", (goal_id,))
        ).fetchone()
    finally:
        await db.close()
    return dict(row) if row is not None else None


async def update_goal(goal_id: int, data: GoalUpdate) -> dict | None:
    updates = data.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        return await get_goal(goal_id)
    db = await get_db()
    try:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        await db.execute(f"UPDATE goals SET {set_clause} WHERE id = ?", (*updates.values(), goal_id))
        await db.commit()
    finally:
        await db.close()
    return await get_goal(goal_id)


async def delete_goal(goal_id: int) -> None:
    db = await get_db()
    try:
        await db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        await db.commit()
    finally:
        await db.close()


async def compute_progress(
    table: str, column: str, person_id: int, target_value: float, target_date: str | None, days: int = 90
) -> GoalProgress:
    """ETA-to-goal from the metric's own recent trend.

    Reuses recommendations.trend_slope, which fits a simple linear trend
    over the last (up to) 14 *rows*, not 14 calendar days -- metric tables
    are one row per date with no forced daily cadence, so the slope and the
    derived ETA below are both expressed in "per row" terms, not strictly
    "per day". For every metric table currently in METRIC_TABLES this is
    normally a good day-cadence approximation, but a metric with sparse
    rows (long sync gaps) will get a looser ETA. target_value is expected
    in whatever unit the metric column itself stores (e.g. weight_grams for
    "weight", not lbs/kg) -- callers are responsible for that agreement.
    """
    data = await get_metric(table, column, person_id, days=days)
    latest_value = data[-1]["value"] if data else None
    slope = trend_slope(data) if data else None

    eta_date = None
    if latest_value is not None:
        remaining = target_value - latest_value
        if remaining == 0:
            eta_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif slope not in (None, 0) and (remaining > 0) == (slope > 0):
            # Trend is moving toward the target -- project forward. A trend
            # moving away from it (opposite sign) has no finite forward ETA.
            steps_needed = remaining / slope
            if steps_needed >= 0:
                eta_date = (datetime.now(timezone.utc) + timedelta(days=steps_needed)).strftime("%Y-%m-%d")

    on_track = None
    if eta_date is not None:
        on_track = eta_date <= target_date if target_date else True

    return GoalProgress(latest_value=latest_value, trend_slope=slope, eta_date=eta_date, on_track=on_track)


async def progress_for_goal(goal: dict, person_id: int) -> GoalProgress | None:
    """Progress for a stored goal row, resolving its metric to (table, column).

    Returns None -- not an error -- when the row's metric is absent from
    METRIC_TABLES. Only reachable if the metric predates a since-removed entry,
    and a goal that outlived its metric should read back as "no progress"
    rather than 500 the whole list route.
    """
    mapping = METRIC_TABLES.get(goal["metric"])
    if mapping is None:
        return None
    table, column = mapping
    return await compute_progress(table, column, person_id, goal["target_value"], goal["target_date"])
