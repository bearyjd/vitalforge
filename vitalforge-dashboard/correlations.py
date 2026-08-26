"""Pure-Python cross-metric correlation math for the ad-hoc correlation view.

No database access lives here on purpose — `app.py` owns the single
per-request DB connection and hands this module plain `{date: value}`
dicts, keeping `pearson_r`/`align_series`/`compute_cell` trivially unit
testable without a DB fixture.

All `METRIC_TABLES` tables in `app.py` are `date TEXT PRIMARY KEY`, so
alignment between two metrics is a plain dict inner-join on date — no
interpolation, no resampling. `weight_log` (timestamp-keyed, one row per
log entry rather than one row per day) is not in `METRIC_TABLES` and is
therefore never reachable through this endpoint; that exclusion falls out
of reusing `METRIC_TABLES` rather than needing special-case logic here.
"""

import math
from datetime import datetime, timedelta


def pearson_r(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient between two equal-length series.

    Returns `None` — never `NaN` — when there are fewer than 2 points, the
    lengths mismatch, or either series has zero variance (a constant
    series has an undefined correlation, not a divide-by-zero one).
    Starlette's JSON encoder raises `ValueError` on a raw float NaN, so
    every exit path here must resolve to a finite float or `None` before
    the caller serializes it.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)

    if var_x == 0 or var_y == 0:
        return None

    r = cov / math.sqrt(var_x * var_y)
    # Clamp for float-precision safety (e.g. a "perfect" series can come
    # back as 1.0000000000000002 from the sqrt/division chain above).
    return max(-1.0, min(1.0, r))


def _shift_date(date_str: str, days: int) -> str:
    """Shift an ISO `YYYY-MM-DD` date string forward (or back) `days` days."""
    shifted = datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=days)
    return shifted.isoformat()


def align_series(
    row_series: dict[str, float], col_series: dict[str, float], lag_days: int = 0
) -> tuple[list[float], list[float]]:
    """Inner-join two date-keyed series, optionally lagged.

    `row_series`'s dates are shifted forward by `lag_days` before joining
    against `col_series`'s dates (unchanged). So for a matrix cell
    correlating row metric i against column metric j at lag L, this tests
    whether i's value on day D lines up with j's value on day D + L.

    This is deliberately one-directional: shifting only the row series is
    what makes lag != 0 produce an asymmetric matrix (cell[i][j] and
    cell[j][i] are generally different joins, not mirror images), even
    though `pearson_r` itself is a symmetric function of its two inputs.
    At lag == 0 no shift happens and the join is symmetric.

    Dates are joined and returned in sorted order for determinism, though
    `pearson_r` itself is order-independent.

    `row_series` keys are not guaranteed to be well-formed `YYYY-MM-DD`
    strings -- `weight_history` (unlike the Garmin sync tables) isn't as
    tightly controlled, so a malformed date can reach here. A row whose
    date can't be shifted is simply excluded from the join rather than
    raising, degrading the correlation for that row instead of failing
    the whole request.
    """
    if lag_days:
        shifted = {}
        for d, v in row_series.items():
            try:
                shifted[_shift_date(d, lag_days)] = v
            except ValueError:
                continue
    else:
        shifted = row_series

    common_dates = sorted(set(shifted) & set(col_series))
    xs = [shifted[d] for d in common_dates]
    ys = [col_series[d] for d in common_dates]
    return xs, ys


def compute_cell(
    row_series: dict[str, float],
    col_series: dict[str, float],
    lag_days: int,
    min_pairs: int,
) -> dict:
    """Build one `{"r": float|None, "n": int, "reason": str|None}` matrix cell.

    `n` always reflects the actual number of aligned pairs found, even
    when `r` comes back `None` because that count fell below `min_pairs`
    or the aligned values had zero variance. `reason` disambiguates those
    two null cases for the frontend tooltip (both are otherwise
    indistinguishable from `r`/`n` alone): `"insufficient_pairs"` when
    `n < min_pairs`, `"zero_variance"` when there were enough pairs but
    one series was constant over them, `None` whenever `r` is not null.
    """
    xs, ys = align_series(row_series, col_series, lag_days)
    n = len(xs)
    if n < min_pairs:
        return {"r": None, "n": n, "reason": "insufficient_pairs"}
    r = pearson_r(xs, ys)
    reason = None if r is not None else "zero_variance"
    return {"r": r, "n": n, "reason": reason}
