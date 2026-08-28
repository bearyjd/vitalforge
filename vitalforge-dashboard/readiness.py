"""Composite readiness/recovery score (0-100) for VitalForge dashboard.

Combines three independently-normalized 0-100 components:
- HRV vs. its own trailing baseline (40% weight)
- Resting HR level + 14-day trend vs. baseline (30% weight)
- Garmin's native sleep_score (30% weight)

`body_battery` is deliberately excluded from v1 scoring: it's strongly
correlated with HRV and sleep and doesn't carry an independent signal.

Each component requires at least MIN_BASELINE_DAYS of trailing data to be
included at all. When fewer than three components have enough data, the
composite renormalizes weights across whichever components ARE available
rather than crashing or silently zeroing missing inputs out of the average.
"""

import sys
from pathlib import Path

# `recommendations.py` lives next to this file and is imported by bare name
# below. app.py already puts this directory on sys.path before importing
# either sibling module, but this module also needs to be importable
# standalone (e.g. `importlib.import_module` from a test), so it carries the
# same sys.path hack itself rather than relying on app.py having run first.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from recommendations import avg, get_all_metrics, trend_slope

MIN_BASELINE_DAYS = 5
RECENT_WINDOW_DAYS = 3
RHR_LEVEL_WINDOW_DAYS = 1  # the single "latest" day evaluated against baseline
RHR_TREND_WINDOW_DAYS = 14
RHR_TREND_SCALE = 40  # bpm/day slope that swings the trend sub-score by 100 pts
RHR_LEVEL_WEIGHT = 0.6
RHR_TREND_WEIGHT = 0.4

WEIGHTS = {"hrv": 0.4, "rhr": 0.3, "sleep_score": 0.3}


def _clamp(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, value))


def _recent_avg(data: list[dict], n: int = RECENT_WINDOW_DAYS) -> float | None:
    window = data[-n:] if len(data) >= n else data
    return avg([d["value"] for d in window])


def _baseline_avg(data: list[dict], exclude_recent: int) -> float | None:
    """Average of `data` excluding the trailing `exclude_recent` days (the
    evaluation window), so a real deviation in the recent days isn't diluted
    into the very baseline it's being compared against -- mirrors how
    `recommendations.py`'s week-over-week comparison excludes its own
    evaluation window from the baseline it uses (see `hrv_data[-14:-7]` in
    `run_rules`). Falls back to averaging all of `data` if excluding the
    window would leave nothing to average.
    """
    pool = data[:-exclude_recent] if len(data) > exclude_recent else data
    return avg([d["value"] for d in pool])


def _hrv_score(hrv_data: list[dict]) -> float | None:
    """HRV vs. its own trailing baseline: above baseline scores higher."""
    if len(hrv_data) < MIN_BASELINE_DAYS:
        return None
    baseline = _baseline_avg(hrv_data, RECENT_WINDOW_DAYS)
    if not baseline:
        return None
    recent = _recent_avg(hrv_data)
    if recent is None:
        return None
    pct_diff = (recent - baseline) / baseline
    return _clamp(50 + pct_diff * 200)


def _rhr_score(rhr_data: list[dict]) -> float | None:
    """Resting HR level (vs. baseline) blended with its 14-day trend.

    A lower-than-baseline RHR and a downward/flat trend score higher; an
    elevated RHR or an upward trend scores lower. Falls back to the level
    sub-score alone when there isn't enough data for a trend (fewer than 3
    trailing points -- see `trend_slope`).
    """
    if len(rhr_data) < MIN_BASELINE_DAYS:
        return None
    baseline = _baseline_avg(rhr_data, RHR_LEVEL_WINDOW_DAYS)
    if not baseline:
        return None
    latest = rhr_data[-1]["value"]
    pct_diff = (latest - baseline) / baseline
    level_score = _clamp(50 - pct_diff * 200)

    slope = trend_slope(rhr_data, RHR_TREND_WINDOW_DAYS)
    if slope is None:
        return level_score

    trend_score = _clamp(50 - slope * RHR_TREND_SCALE)
    return _clamp(level_score * RHR_LEVEL_WEIGHT + trend_score * RHR_TREND_WEIGHT)


def _sleep_score(sleep_score_data: list[dict]) -> float | None:
    """Garmin's native sleep_score, averaged over the recent window."""
    if len(sleep_score_data) < MIN_BASELINE_DAYS:
        return None
    recent = _recent_avg(sleep_score_data)
    if recent is None:
        return None
    return _clamp(recent)


def score_readiness(data: dict) -> dict:
    """Pure scoring function: metric-name -> [{"date","value"}] -> readiness dict.

    `data` is shaped like `get_all_metrics()`'s output (only the "hrv",
    "resting_hr", and "sleep_score" keys are consulted -- `body_battery` and
    everything else is ignored by design). Renormalizes the 40/30/30
    weighting across whichever components have enough trailing data instead
    of crashing or zeroing missing inputs out of the average.
    """
    components = {
        "hrv": _hrv_score(data.get("hrv", [])),
        "rhr": _rhr_score(data.get("resting_hr", [])),
        "sleep_score": _sleep_score(data.get("sleep_score", [])),
    }
    rounded_components = {k: (round(v) if v is not None else None) for k, v in components.items()}

    present = {k: v for k, v in components.items() if v is not None}
    if not present:
        return {"score": None, "components": rounded_components, "status": "insufficient_data"}

    weight_sum = sum(WEIGHTS[k] for k in present)
    weighted = sum(components[k] * WEIGHTS[k] for k in present) / weight_sum
    status = "ok" if len(present) == len(components) else "partial_data"

    return {"score": round(_clamp(weighted)), "components": rounded_components, "status": status}


async def compute_readiness(person_id: int) -> dict:
    """Fetch trailing metrics and compute the composite readiness score."""
    data = await get_all_metrics(person_id, days=30)
    return score_readiness(data)
