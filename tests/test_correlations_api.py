"""Tests for the vitalforge-dashboard ad-hoc cross-metric correlation API.

Like `test_dashboard_api.py`, this never touches Garmin -- `/api/correlations`
only reads the local metric tables populated by `sync.py`, so every test
seeds those tables directly.
"""

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared.database import get_db

# `vitalforge-dashboard` is a hyphenated directory name, so `correlations.py`
# is loaded via `importlib.import_module` (same mechanism `conftest.py`'s
# `import_service_module` uses for `vitalforge-dashboard.app`) rather than a
# normal `import` statement.
_correlations = importlib.import_module("vitalforge-dashboard.correlations")
align_series = _correlations.align_series
compute_cell = _correlations.compute_cell
pearson_r = _correlations.pearson_r


def date_n_days_ago(n: int) -> str:
    """A synthetic date string N days before now, for seeding within the
    correlation endpoint's default 30-day lookback window."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
async def client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def seed_metric(table: str, column: str, rows: list[tuple[str, float]]):
    """Insert (date, value) rows into a metric table for testing."""
    db = await get_db()
    try:
        for date, value in rows:
            await db.execute(
                f"INSERT OR REPLACE INTO [{table}] (date, [{column}]) VALUES (?, ?)",
                (date, value),
            )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Pure-Python math (correlations.py), no DB / HTTP involved
# ---------------------------------------------------------------------------


def test_pearson_r_perfect_positive():
    assert pearson_r([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


def test_pearson_r_perfect_negative():
    assert pearson_r([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_pearson_r_zero_variance_returns_none():
    assert pearson_r([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


def test_pearson_r_too_few_points_returns_none():
    assert pearson_r([1.0], [1.0]) is None


def test_pearson_r_never_returns_nan():
    # Both constant -- naive implementations divide 0/0 into NaN.
    r = pearson_r([7.0, 7.0], [3.0, 3.0])
    assert r is None


def test_align_series_lag_zero_is_symmetric_join():
    a = {"2026-01-01": 1.0, "2026-01-02": 2.0}
    b = {"2026-01-02": 20.0, "2026-01-03": 30.0}
    xs, ys = align_series(a, b, lag_days=0)
    assert xs == [2.0]
    assert ys == [20.0]


def test_align_series_lag_shifts_row_series_forward():
    a = {"2026-01-01": 1.0}
    b = {"2026-01-02": 99.0}
    xs, ys = align_series(a, b, lag_days=1)
    assert xs == [1.0]
    assert ys == [99.0]


def test_compute_cell_below_min_pairs_nulls_r_but_keeps_n():
    a = {"2026-01-01": 1.0, "2026-01-02": 2.0}
    b = {"2026-01-01": 2.0, "2026-01-02": 4.0}
    cell = compute_cell(a, b, lag_days=0, min_pairs=5)
    assert cell["n"] == 2
    assert cell["r"] is None
    assert cell["reason"] == "insufficient_pairs"


def test_compute_cell_zero_variance_reason():
    a = {"2026-01-01": 5.0, "2026-01-02": 5.0, "2026-01-03": 5.0}
    b = {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": 3.0}
    cell = compute_cell(a, b, lag_days=0, min_pairs=2)
    assert cell["n"] == 3
    assert cell["r"] is None
    assert cell["reason"] == "zero_variance"


def test_compute_cell_with_r_has_no_reason():
    a = {"2026-01-01": 1.0, "2026-01-02": 2.0}
    b = {"2026-01-01": 1.0, "2026-01-02": 2.0}
    cell = compute_cell(a, b, lag_days=0, min_pairs=2)
    assert cell["r"] is not None
    assert cell["reason"] is None


def test_align_series_skips_malformed_row_date_instead_of_raising():
    row = {"2026-01-01": 1.0, "not-a-date": 999.0, "2026-01-02": 2.0}
    col = {"2026-01-02": 20.0, "2026-01-03": 30.0}
    xs, ys = align_series(row, col, lag_days=1)
    # The well-formed dates shift and join normally ("2026-01-01" ->
    # "2026-01-02", "2026-01-02" -> "2026-01-03"); the malformed date is
    # silently excluded rather than raising ValueError.
    assert xs == [1.0, 2.0]
    assert ys == [20.0, 30.0]


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


async def test_correlations_perfect_positive(client):
    dates = [date_n_days_ago(i) for i in range(10, 0, -1)]
    values = list(range(1, 11))
    await seed_metric("steps", "value", list(zip(dates, values)))
    await seed_metric("active_calories", "value", list(zip(dates, values)))

    resp = await client.get("/api/correlations?metrics=steps,active_calories")
    assert resp.status_code == 200
    body = resp.json()

    assert body["metrics"] == ["steps", "active_calories"]
    assert len(body["cells"]) == 2
    assert len(body["cells"][0]) == 2

    cell = body["cells"][0][1]
    assert cell["n"] == 10
    assert cell["r"] == pytest.approx(1.0, abs=1e-9)
    # Diagonal cells are self-correlation: always 1.0 at lag 0.
    assert body["cells"][0][0]["r"] == pytest.approx(1.0, abs=1e-9)
    assert body["cells"][1][1]["r"] == pytest.approx(1.0, abs=1e-9)


async def test_correlations_perfect_negative(client):
    dates = [date_n_days_ago(i) for i in range(10, 0, -1)]
    values_a = list(range(1, 11))
    values_b = list(range(10, 0, -1))
    await seed_metric("resting_hr", "value", list(zip(dates, values_a)))
    await seed_metric("stress", "avg_level", list(zip(dates, values_b)))

    resp = await client.get("/api/correlations?metrics=resting_hr,stress")
    assert resp.status_code == 200
    cell = resp.json()["cells"][0][1]
    assert cell["n"] == 10
    assert cell["r"] == pytest.approx(-1.0, abs=1e-9)


async def test_correlations_insufficient_pairs_returns_null(client):
    dates = [date_n_days_ago(i) for i in range(3, 0, -1)]
    values_a = [1.0, 2.0, 3.0]
    values_b = [3.0, 2.0, 1.0]
    await seed_metric("resting_hr", "value", list(zip(dates, values_a)))
    await seed_metric("hrv", "last_night_avg", list(zip(dates, values_b)))

    resp = await client.get("/api/correlations?metrics=resting_hr,hrv&min_pairs=5")
    assert resp.status_code == 200
    cell = resp.json()["cells"][0][1]
    # Real (anti-correlated) variance in the data -- nulled purely for
    # falling short of min_pairs, not for a zero-variance reason.
    assert cell["n"] == 3
    assert cell["r"] is None


async def test_correlations_zero_variance_returns_null(client):
    dates = [date_n_days_ago(i) for i in range(10, 0, -1)]
    constant = [50.0] * 10
    varying = list(range(1, 11))
    await seed_metric("resting_hr", "value", list(zip(dates, constant)))
    await seed_metric("hrv", "last_night_avg", list(zip(dates, varying)))

    resp = await client.get("/api/correlations?metrics=resting_hr,hrv")
    assert resp.status_code == 200
    cell = resp.json()["cells"][0][1]
    assert cell["n"] == 10
    assert cell["r"] is None


async def test_correlations_lag_produces_asymmetric_matrix(client):
    """Metric A spans 6 consecutive days D0..D5; metric B spans the same
    6-day span shifted one day later, D1..D6. Shifting A forward by 1 day
    to join against B gives full 6-point overlap (D1..D6); shifting B
    forward by 1 day to join against A only overlaps 4 of A's dates
    (D2..D5). So cells[A][B] and cells[B][A] must come back with
    different `n` at lag=1, even though the underlying data is otherwise
    symmetric (both perfectly increasing 1..6)."""
    anchor = 6
    dates_a = [date_n_days_ago(anchor - i) for i in range(6)]  # D0..D5
    dates_b = [date_n_days_ago(anchor - 1 - i) for i in range(6)]  # D1..D6
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    await seed_metric("steps", "value", list(zip(dates_a, values)))
    await seed_metric("active_calories", "value", list(zip(dates_b, values)))

    resp = await client.get("/api/correlations?metrics=steps,active_calories&lag=1&min_pairs=2")
    assert resp.status_code == 200
    cells = resp.json()["cells"]

    cell_ab = cells[0][1]  # steps (row, shifted +1d) vs active_calories (col)
    cell_ba = cells[1][0]  # active_calories (row, shifted +1d) vs steps (col)

    assert cell_ab["n"] == 6
    assert cell_ba["n"] == 4
    assert cell_ab["n"] != cell_ba["n"]


async def test_correlations_lag_zero_matrix_is_symmetric_in_n(client):
    """Sanity check for the asymmetry claim above: with lag=0 (no shift),
    the same two series produce the same overlap count in both
    directions."""
    anchor = 6
    dates_a = [date_n_days_ago(anchor - i) for i in range(6)]
    dates_b = [date_n_days_ago(anchor - 1 - i) for i in range(6)]
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    await seed_metric("steps", "value", list(zip(dates_a, values)))
    await seed_metric("active_calories", "value", list(zip(dates_b, values)))

    resp = await client.get("/api/correlations?metrics=steps,active_calories&lag=0&min_pairs=2")
    cells = resp.json()["cells"]
    assert cells[0][1]["n"] == cells[1][0]["n"] == 5


async def test_correlations_malformed_date_returns_200_and_excludes_row(client):
    """`weight_history` isn't as tightly controlled as the Garmin sync
    tables, so a malformed date can reach `align_series`. With `lag != 0`
    that used to raise an unhandled ValueError (-> 500); it must now
    degrade to a 200 with that one row simply excluded from the join."""
    dates = [date_n_days_ago(i) for i in range(5, 0, -1)]
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    await seed_metric("weight_history", "weight_grams", list(zip(dates, values)))
    # A malformed date sneaks into weight_history alongside the good rows.
    await seed_metric("weight_history", "weight_grams", [("not-a-date", 999.0)])
    await seed_metric("steps", "value", list(zip(dates, values)))

    resp = await client.get("/api/correlations?metrics=weight,steps&lag=1&min_pairs=2")
    assert resp.status_code == 200
    cell = resp.json()["cells"][0][1]
    # The malformed row is excluded from alignment, not counted or crashed on.
    assert cell["n"] == 4


async def test_correlations_cell_reason_field_present(client):
    dates = [date_n_days_ago(i) for i in range(10, 0, -1)]
    constant = [50.0] * 10
    varying = list(range(1, 11))
    await seed_metric("resting_hr", "value", list(zip(dates, constant)))
    await seed_metric("hrv", "last_night_avg", list(zip(dates, varying)))

    resp = await client.get("/api/correlations?metrics=resting_hr,hrv")
    assert resp.status_code == 200
    cell = resp.json()["cells"][0][1]
    assert cell["r"] is None
    assert cell["reason"] == "zero_variance"

    # resting_hr's own diagonal is also zero-variance (it's constant), so
    # check hrv's diagonal instead, which has real variance and self-r == 1.
    diag_cell = resp.json()["cells"][1][1]
    assert diag_cell["r"] is not None
    assert diag_cell["reason"] is None


async def test_correlations_unknown_metric_returns_400(client):
    resp = await client.get("/api/correlations?metrics=not_a_real_metric,steps")
    assert resp.status_code == 400


async def test_correlations_weight_log_not_reachable(client):
    """weight_log is timestamp-keyed and deliberately excluded from v1 by
    never being in METRIC_TABLES -- it must be rejected exactly like any
    other unknown metric name, not silently accepted."""
    resp = await client.get("/api/correlations?metrics=weight_log")
    assert resp.status_code == 400


async def test_correlations_missing_metrics_param_returns_422(client):
    resp = await client.get("/api/correlations")
    assert resp.status_code == 422


async def test_correlations_single_db_connection_per_request(client, dashboard_app_module, monkeypatch):
    """Correctness improvement over recommendations.py's per-call-connection
    pattern: computing an NxN matrix must open exactly one DB connection
    for the whole request, not one per metric or metric pair."""
    from shared import database

    call_count = 0
    real_get_db = database.get_db

    async def counting_get_db():
        nonlocal call_count
        call_count += 1
        return await real_get_db()

    monkeypatch.setattr(dashboard_app_module, "get_db", counting_get_db)

    resp = await client.get("/api/correlations?metrics=steps,active_calories,resting_hr")
    assert resp.status_code == 200
    assert call_count == 1
