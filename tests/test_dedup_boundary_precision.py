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


async def _within_window(db, t1: str, t2: str, seconds: int) -> bool:
    """The exact WHERE-clause fragment vitalforge-weight/app.py uses,
    parameterized (not string-built) so this is genuinely the same call
    shape as production, not a hand-rebuilt approximation of it."""
    cursor = await db.execute(
        "SELECT (julianday(?) >= julianday(?, ?)) AND (julianday(?) <= julianday(?, ?))",
        (t1, t2, f"-{seconds} seconds", t1, t2, f"+{seconds} seconds"),
    )
    (within,) = await cursor.fetchone()
    return bool(within)


async def _within_window_via_subtraction(db, t1: str, t2: str, seconds: int) -> bool:
    """The pre-fix form, kept only to prove it's the one that fails."""
    cursor = await db.execute(
        "SELECT ABS(julianday(?) - julianday(?)) <= ? / 86400.0",
        (t1, t2, seconds),
    )
    (within,) = await cursor.fetchone()
    return bool(within)


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

        within = await _within_window(db, row_ts, now_ts, DEDUP_WINDOW_SECONDS)
        assert within, (
            f"row at {row_ts} should be within +-{DEDUP_WINDOW_SECONDS}s of {now_ts} "
            "but the symmetric-modifier query excluded it"
        )
    finally:
        await db.close()


async def _find_a_failing_microsecond(db, seconds: int) -> int | None:
    """Search for a microsecond value where the pre-fix subtraction form
    actually fails on THIS SQLite build, instead of hardcoding one witness
    value observed on a specific build. A coarse sweep (every 37th value)
    is enough -- the failure rate was ~9% over 200k trials, so this finds
    one in a handful of steps if the failure mode reproduces at all."""
    for us in range(0, 1_000_000, 37):
        base = datetime(2026, 8, 23, 1, 28, 44, us, tzinfo=timezone.utc)
        row_ts = base.isoformat()
        now_ts = (base + timedelta(seconds=seconds)).isoformat()
        if not await _within_window_via_subtraction(db, row_ts, now_ts, seconds):
            return us
    return None


async def test_subtraction_form_can_miss_the_exact_60s_boundary():
    """Documents *why* the fix was needed: the pre-fix ABS(julianday diff)
    form genuinely fails at this exact boundary for at least one real
    microsecond value (not merely theoretical) on this SQLite build.

    Searches for a failing value rather than hardcoding one -- a hardcoded
    microsecond that happened to fail on one SQLite build's julianday()
    rounding could silently stop failing on a different build (different
    libm, different compiler), turning this into a test that always passes
    without protecting anything. A skip with a loud reason is honest about
    that; a silent pass is not."""
    db = await aiosqlite.connect(":memory:")
    try:
        failing_us = await _find_a_failing_microsecond(db, DEDUP_WINDOW_SECONDS)
        if failing_us is None:
            pytest.skip(
                "this SQLite build's julianday() didn't reproduce the float-rounding "
                "failure in a coarse sweep -- the fix in vitalforge-weight/app.py is "
                "still correct (see test_symmetric_modifier_bounds_include_exact_60s_boundary), "
                "but the pre-fix form's failure mode may be build-specific; revisit if "
                "curious rather than treating this skip as a problem"
            )

        base = datetime(2026, 8, 23, 1, 28, 44, failing_us, tzinfo=timezone.utc)
        row_ts = base.isoformat()
        now_ts = (base + timedelta(seconds=DEDUP_WINDOW_SECONDS)).isoformat()
        within = await _within_window_via_subtraction(db, row_ts, now_ts, DEDUP_WINDOW_SECONDS)
        assert not within, f"expected microsecond={failing_us} to reproduce the failure, but it passed"
    finally:
        await db.close()
