"""Tests for `readiness.py`'s composite readiness/recovery scoring.

`score_readiness` is a pure function with no I/O, so these tests build a
minimal `data` dict directly -- shaped like `get_all_metrics()`'s output
(metric name -> list of {"date": str, "value": num}, oldest first) -- and
assert on the dict it returns. No DB, no HTTP client, no Garmin. Mirrors
tests/test_recommendations.py's approach to `run_rules`.

A separate HTTP-level test at the bottom exercises `GET /api/readiness`
through the real FastAPI app to confirm the module wires up correctly when
loaded via `importlib` (the scenario the sys.path fix in readiness.py
guards against).
"""

import importlib
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import PERSON_PREFIX

readiness = importlib.import_module("vitalforge-dashboard.readiness")

score_readiness = readiness.score_readiness
_hrv_score = readiness._hrv_score
_rhr_score = readiness._rhr_score
MIN_BASELINE_DAYS = readiness.MIN_BASELINE_DAYS

TODAY = datetime.now().date()


def series(values, end_date=None):
    """Build an oldest-first list of {"date","value"} dicts ending at end_date."""
    end_date = end_date or TODAY
    n = len(values)
    return [
        {"date": (end_date - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"), "value": v}
        for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------------------
# Full-data scoring
# ---------------------------------------------------------------------------

def test_full_data_scoring_flat_baselines():
    """All three components present, each flat vs. its own baseline: HRV and
    RHR sit exactly at their neutral midpoint (50), sleep_score passes
    through Garmin's own 0-100 value (80) unchanged."""
    data = {
        "hrv": series([45] * 10),
        "resting_hr": series([55] * 10),
        "sleep_score": series([80] * 10),
    }
    result = score_readiness(data)

    assert result["status"] == "ok"
    assert result["components"] == {"hrv": 50, "rhr": 50, "sleep_score": 80}
    # weighted = 50*0.4 + 50*0.3 + 80*0.3 = 59
    assert result["score"] == 59


def test_full_data_scoring_hrv_above_baseline_scores_higher():
    data = {
        # baseline (avg of the 7 pre-window days) = 40, recent 3d avg = 50 -> above baseline
        "hrv": series([40] * 7 + [50] * 3),
        "resting_hr": series([55] * 10),
        "sleep_score": series([80] * 10),
    }
    result = score_readiness(data)
    assert result["components"]["hrv"] > 50


def test_full_data_scoring_elevated_rhr_scores_lower():
    data = {
        "hrv": series([45] * 10),
        # baseline 50, latest 60 -> pct_diff=0.2 -> level_score = 50 - 40 = 10
        "resting_hr": series([50] * 9 + [60]),
        "sleep_score": series([80] * 10),
    }
    result = score_readiness(data)
    assert result["components"]["rhr"] < 50


# ---------------------------------------------------------------------------
# Baseline must exclude the recent evaluation window (regression coverage)
# ---------------------------------------------------------------------------

def test_hrv_score_baseline_excludes_recent_window():
    """Regression test for a HIGH-severity bug: the baseline average used to
    include the trailing RECENT_WINDOW_DAYS days -- the same days being
    evaluated against it -- which diluted a real deviation into its own
    baseline and biased the score back toward 'normal'.

    7 days flat at 45, then a 3-day deviation up to 55.
    Correct baseline = avg of the 7 pre-deviation days = 45 (window excluded).
    recent = avg of the 3 deviation days = 55.
    pct_diff = (55-45)/45 = 0.2222 -> score = 50 + 0.2222*200 = 94.44 -> 94.

    With the bug (window included in baseline), baseline would be
    avg of all 10 = 48, pct_diff = (55-48)/48 = 0.1458 -> score = 79 --
    a materially smaller, wrongly-dampened deviation.
    """
    data = {
        "hrv": series([45] * 7 + [55] * 3),
        "resting_hr": series([55] * 10),
        "sleep_score": series([80] * 10),
    }
    result = score_readiness(data)
    assert result["components"]["hrv"] == 94


def test_rhr_score_level_component_excludes_latest_day_from_baseline():
    """Same regression, for `_rhr_score`'s level sub-score: the baseline
    used to include the single latest day being evaluated against it.

    9 days flat at 50, then a 1-day deviation up to 60.
    Correct baseline (latest day excluded) = 50 -> level_score = 10,
    blended (60/40) with the trend sub-score -> combined score 17.

    With the bug (latest day included in baseline), baseline would be 51,
    level_score = ~14.7, blended -> combined score 20 -- a wrongly-dampened
    deviation pulled back toward 'normal'.
    """
    rhr_data = series([50] * 9 + [60])
    result = _rhr_score(rhr_data)
    assert round(result) == 17


def test_hrv_score_flat_data_unaffected_by_baseline_window_exclusion():
    """Sanity check: excluding the recent window from the baseline doesn't
    change anything when the metric is genuinely flat throughout."""
    assert _hrv_score(series([45] * 10)) == 50


# ---------------------------------------------------------------------------
# Partial-data renormalization
# ---------------------------------------------------------------------------

def test_partial_data_renormalizes_across_available_components():
    """RHR has fewer than MIN_BASELINE_DAYS of data and drops out; the
    composite renormalizes across HRV (40%) and sleep_score (30%) instead of
    treating the missing RHR component as a zero."""
    data = {
        "hrv": series([45] * 10),          # score 50
        "resting_hr": series([55] * 3),    # below MIN_BASELINE_DAYS -> None
        "sleep_score": series([90] * 10),  # score 90
    }
    result = score_readiness(data)

    assert result["status"] == "partial_data"
    assert result["components"] == {"hrv": 50, "rhr": None, "sleep_score": 90}
    # weighted = (50*0.4 + 90*0.3) / (0.4 + 0.3) = 47 / 0.7 = 67.14... -> 67
    assert result["score"] == 67


def test_partial_data_single_component_present():
    data = {"hrv": series([45] * 10)}
    result = score_readiness(data)

    assert result["status"] == "partial_data"
    assert result["components"]["hrv"] == 50
    assert result["components"]["rhr"] is None
    assert result["components"]["sleep_score"] is None
    # Renormalized across just HRV -> its own score, unchanged.
    assert result["score"] == 50


# ---------------------------------------------------------------------------
# Insufficient-data status
# ---------------------------------------------------------------------------

def test_insufficient_data_when_no_component_has_enough_history():
    data = {
        "hrv": series([45] * 2),
        "resting_hr": series([55] * 3),
        "sleep_score": series([80] * 4),
    }
    result = score_readiness(data)

    assert result["status"] == "insufficient_data"
    assert result["score"] is None
    assert result["components"] == {"hrv": None, "rhr": None, "sleep_score": None}


def test_insufficient_data_on_completely_empty_input():
    result = score_readiness({})
    assert result == {
        "score": None,
        "components": {"hrv": None, "rhr": None, "sleep_score": None},
        "status": "insufficient_data",
    }


# ---------------------------------------------------------------------------
# Boundary of the 5-day minimum
# ---------------------------------------------------------------------------

def test_four_days_is_insufficient_for_a_component():
    assert MIN_BASELINE_DAYS == 5
    data = {"hrv": series([45] * (MIN_BASELINE_DAYS - 1))}
    result = score_readiness(data)
    assert result["components"]["hrv"] is None
    assert result["status"] == "insufficient_data"
    assert result["score"] is None


def test_five_days_is_sufficient_for_a_component():
    data = {"hrv": series([45] * MIN_BASELINE_DAYS)}
    result = score_readiness(data)
    assert result["components"]["hrv"] == 50
    assert result["status"] == "partial_data"
    assert result["score"] == 50


def test_boundary_applies_independently_per_component():
    """RHR at exactly the boundary is scored while a still-short sleep_score
    series stays excluded -- the 5-day minimum is checked per component, not
    globally."""
    data = {
        "resting_hr": series([55] * MIN_BASELINE_DAYS),
        "sleep_score": series([80] * (MIN_BASELINE_DAYS - 1)),
    }
    result = score_readiness(data)
    assert result["components"]["rhr"] == 50
    assert result["components"]["sleep_score"] is None
    assert result["status"] == "partial_data"


# ---------------------------------------------------------------------------
# body_battery is deliberately excluded from v1 scoring
# ---------------------------------------------------------------------------

def test_body_battery_does_not_affect_score():
    base = {
        "hrv": series([45] * 10),
        "resting_hr": series([55] * 10),
        "sleep_score": series([80] * 10),
    }
    with_bb = {**base, "body_battery": series([10] * 10)}  # extreme low value

    assert score_readiness(base) == score_readiness(with_bb)
    assert "body_battery" not in score_readiness(with_bb)["components"]


# ---------------------------------------------------------------------------
# HTTP-level: confirms the sys.path fix lets the module load and wire up
# correctly under app.py's importlib-based module loading.
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_readiness_endpoint_insufficient_data_on_empty_db(client):
    resp = await client.get(f"{PERSON_PREFIX}/api/readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "insufficient_data"
    assert body["score"] is None
    assert set(body["components"]) == {"hrv", "rhr", "sleep_score"}


async def test_readiness_endpoint_does_not_call_garmin(client, fake_garmin_client):
    resp = await client.get(f"{PERSON_PREFIX}/api/readiness")
    assert resp.status_code == 200
    assert fake_garmin_client.pushed_weights == []
