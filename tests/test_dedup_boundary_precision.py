"""Regression for a Codex review finding (2026-08-23) on the dedup window's
symmetric-bounds rewrite: two timestamps exactly 60.000000s apart can compute
`ABS(julianday(a) - julianday(b))` as a few dozen microseconds *above*
60/86400 due to floating-point cancellation from subtracting two large,
independently-rounded Julian day values (confirmed ~9% of random microsecond
offsets over 200k trials) -- silently excluding a legitimate boundary
duplicate from dedup. vitalforge-weight/app.py's dedup query was rewritten to
compare against `julianday(now, '+-60 seconds')` (SQLite's own offset
arithmetic) instead of subtracting two julianday() values, which cannot hit
this failure mode.

HTTP-level tests (test_dedup.py) seed a row via wall-clock `seconds_ago` and
then POST, so they can't hit the boundary precisely enough to catch this --
real test-execution time elapses between seeding and the request (see
test_dedup_window_58s_collapses's own comment). This test controls both
timestamps directly via SQL instead, so it's exact and deterministic.
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

DEDUP_WINDOW_SECONDS = 60


def _within_window_sql(t1: str, t2: str, seconds: int) -> str:
    """The exact WHERE-clause fragment vitalforge-weight/app.py uses."""
    return (
        f"SELECT (julianday('{t1}') >= julianday('{t2}', '-{seconds} seconds')) "
        f"AND (julianday('{t1}') <= julianday('{t2}', '+{seconds} seconds'))"
    )


def _within_window_via_subtraction_sql(t1: str, t2: str, seconds: int) -> str:
    """The pre-fix form, kept here only to prove it's the one that fails."""
    return f"SELECT ABS(julianday('{t1}') - julianday('{t2}')) <= {seconds} / 86400.0"


@pytest.mark.parametrize("microsecond", [0, 1, 123456, 500000, 999999])
async def test_symmetric_modifier_bounds_include_exact_60s_boundary(microsecond):
    """The fixed form must never exclude a row exactly 60.000000s away,
    regardless of the microsecond component (which is what perturbs the
    float rounding)."""
    db = await aiosqlite.connect(":memory:")
    try:
        base = datetime(2026, 8, 23, 1, 28, 44, microsecond, tzinfo=timezone.utc)
        row_ts = base.isoformat()
        now_ts = (base + timedelta(seconds=DEDUP_WINDOW_SECONDS)).isoformat()

        cursor = await db.execute(_within_window_sql(row_ts, now_ts, DEDUP_WINDOW_SECONDS))
        (within,) = await cursor.fetchone()
        assert within, (
            f"row at {row_ts} should be within +-{DEDUP_WINDOW_SECONDS}s of {now_ts} "
            "but the symmetric-modifier query excluded it"
        )
    finally:
        await db.close()


async def test_subtraction_form_can_miss_the_exact_60s_boundary():
    """Documents *why* the fix was needed: the pre-fix ABS(julianday diff)
    form genuinely fails at this exact boundary for at least one real
    microsecond value (not merely theoretical)."""
    db = await aiosqlite.connect(":memory:")
    try:
        base = datetime(2026, 8, 23, 1, 28, 44, 588508, tzinfo=timezone.utc)
        row_ts = base.isoformat()
        now_ts = (base + timedelta(seconds=DEDUP_WINDOW_SECONDS)).isoformat()

        cursor = await db.execute(
            _within_window_via_subtraction_sql(row_ts, now_ts, DEDUP_WINDOW_SECONDS)
        )
        (within,) = await cursor.fetchone()
        assert not within, (
            "expected the pre-fix subtraction form to demonstrate the float-rounding "
            "failure at this microsecond value -- if this now passes, SQLite's "
            "julianday() precision behavior has changed and this test (and the "
            "code comment explaining the fix) should be revisited"
        )
    finally:
        await db.close()
