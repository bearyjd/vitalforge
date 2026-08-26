"""Unit tests for `recommendations.py`'s rules engine and numeric helpers.

`run_rules` and its helpers (`avg`, `trend_slope`, `consecutive_below`,
`consecutive_above`) are pure functions with no I/O, so these tests build a
minimal `data` dict directly — shaped like `get_all_metrics()`'s output
(metric name -> list of {"date": str, "value": num}, oldest first) — and
assert on the findings `run_rules` returns. No DB, no HTTP client, no Garmin.

Each metric key is populated with only what its target rule needs; other
rules may incidentally also fire on the same synthetic data (documented
inline where relevant) — tests only assert the target finding is present,
not that it's the sole finding.
"""

import importlib
from datetime import datetime, timedelta

recommendations = importlib.import_module("vitalforge-dashboard.recommendations")

run_rules = recommendations.run_rules
avg = recommendations.avg
stdev = recommendations.stdev
trend_slope = recommendations.trend_slope
consecutive_below = recommendations.consecutive_below
consecutive_above = recommendations.consecutive_above

TODAY = datetime.now().date()


def series(values, end_date=None):
    """Build an oldest-first list of {"date","value"} dicts ending at end_date."""
    end_date = end_date or TODAY
    n = len(values)
    return [
        {"date": (end_date - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"), "value": v}
        for i, v in enumerate(values)
    ]


def find(findings, rule):
    return next((f for f in findings if f["rule"] == rule), None)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def test_avg_ignores_none_and_empty():
    assert avg([1, 2, None, 3]) == 2
    assert avg([]) is None
    assert avg([None, None]) is None


def test_trend_slope_none_below_three_points():
    assert trend_slope(series([1, 2])) is None


def test_trend_slope_positive_for_rising_series():
    assert trend_slope(series([10, 20, 30, 40]), n=4) == 10


def test_consecutive_below_stops_at_first_non_matching_from_end():
    data = series([5, 5, 1, 1, 1])
    assert consecutive_below(data, 3, from_end=5) == 3


def test_consecutive_above_stops_at_first_non_matching_from_end():
    data = series([50, 50, 90, 90, 90])
    assert consecutive_above(data, 60, from_end=5) == 3


def test_stdev_ignores_none_and_needs_two_points():
    assert stdev([1]) is None
    assert stdev([]) is None
    assert stdev([None, 5]) is None
    assert stdev([2, None, 4]) == stdev([2, 4])
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == (32 / 7) ** 0.5


# ---------------------------------------------------------------------------
# Quiet/healthy data -> no findings
# ---------------------------------------------------------------------------

def test_no_findings_on_stable_healthy_data():
    data = {
        "sleep_duration": series([27000] * 30),   # 7.5h/night, flat
        "sleep_score": series([85] * 30),
        "resting_hr": series([55] * 30),
        "hrv": series([45] * 30),
        "body_battery": series([90] * 30),
        "stress": series([25] * 30),
        "steps": series([9000] * 30),
    }
    assert run_rules(data) == []


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def test_sleep_low_duration():
    data = {"sleep_duration": series([27000, 27000, 27000, 27000, 21600, 21600, 21600])}
    f = find(run_rules(data), "sleep_low_duration")
    assert f is not None
    assert f["severity"] == "warning"
    assert f["data"]["consecutive_days"] == 3


def test_sleep_low_duration_requires_three_consecutive_days():
    data = {"sleep_duration": series([27000, 27000, 27000, 27000, 27000, 21600, 21600])}
    assert find(run_rules(data), "sleep_low_duration") is None


def test_sleep_declining():
    data = {"sleep_duration": series([30000 - i * 200 for i in range(14)])}
    f = find(run_rules(data), "sleep_declining")
    assert f is not None
    assert f["data"]["trend_min_per_day"] == round(-200 / 60, 1)


def test_sleep_low_score():
    data = {"sleep_score": series([85, 85, 85, 85, 65, 65, 65])}
    f = find(run_rules(data), "sleep_low_score")
    assert f is not None
    assert f["data"]["consecutive_days"] == 3


# ---------------------------------------------------------------------------
# Recovery: HRV / resting HR / body battery
# ---------------------------------------------------------------------------

def test_hrv_below_baseline():
    data = {"hrv": series([50] * 7 + [30] * 3)}
    f = find(run_rules(data), "hrv_below_baseline")
    assert f is not None
    assert f["data"]["consecutive_days"] == 3


def test_hrv_weekly_drop():
    data = {"hrv": series([50] * 7 + [40] * 7)}
    f = find(run_rules(data), "hrv_weekly_drop")
    assert f is not None
    assert f["severity"] == "alert"
    assert f["data"]["pct_change"] == -20.0


def test_rhr_elevated():
    data = {"resting_hr": series([50, 50, 50, 50, 50, 50, 60])}
    f = find(run_rules(data), "rhr_elevated")
    assert f is not None
    assert f["data"]["current"] == 60


def test_rhr_trending_up():
    data = {"resting_hr": series([50 + 0.5 * i for i in range(14)])}
    f = find(run_rules(data), "rhr_trending_up")
    assert f is not None


def test_body_battery_low():
    data = {"body_battery": series([90, 90, 90, 90, 70, 70, 70])}
    f = find(run_rules(data), "body_battery_low")
    assert f is not None
    assert f["data"]["consecutive_days"] == 3


# ---------------------------------------------------------------------------
# Stress
# ---------------------------------------------------------------------------

def test_stress_high():
    data = {"stress": series([25, 25, 25, 25, 60, 60, 60])}
    f = find(run_rules(data), "stress_high")
    assert f is not None
    assert f["data"]["consecutive_days"] == 3


def test_stress_trending_up():
    data = {"stress": series([20 + i for i in range(14)])}
    f = find(run_rules(data), "stress_trending_up")
    assert f is not None


# ---------------------------------------------------------------------------
# Body composition (weight)
# ---------------------------------------------------------------------------

def test_weight_no_data():
    data = {"weight": series([90000], end_date=TODAY - timedelta(days=10))}
    f = find(run_rules(data), "weight_no_data")
    assert f is not None
    assert f["data"]["days_since"] == 10


def test_weight_no_data_requires_seven_days():
    data = {"weight": series([90000], end_date=TODAY - timedelta(days=6))}
    assert find(run_rules(data), "weight_no_data") is None


def test_weight_rapid_gain():
    data = {"weight": series([80000] * 7 + [81300] * 7)}
    f = find(run_rules(data), "weight_rapid_gain")
    assert f is not None
    assert f["data"]["weekly_change_g"] == 1300


def test_weight_plateau_with_active_training():
    data = {
        "weight": series([80000] * 21),
        "training_load": series([80] * 7),
    }
    f = find(run_rules(data), "weight_plateau")
    assert f is not None


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

def test_steps_low():
    data = {"steps": series([5000] * 7)}
    f = find(run_rules(data), "steps_low")
    assert f is not None
    assert f["data"]["weekly_avg"] == 5000


def test_training_load_spike():
    data = {"training_load": series([50] * 7 + [90] * 7)}
    f = find(run_rules(data), "training_load_spike")
    assert f is not None


def test_vo2max_declining():
    data = {"vo2max": series([50 - 0.1 * i for i in range(14)])}
    f = find(run_rules(data), "vo2max_declining")
    assert f is not None


# ---------------------------------------------------------------------------
# Correlations
# ---------------------------------------------------------------------------

def test_recovery_deficit_correlation():
    data = {
        "sleep_duration": series([18000, 18000, 18000]),        # poor sleep (<6h)
        "resting_hr": series([50, 50, 50, 50, 50, 50, 60]),      # elevated latest
        "hrv": series([50] * 7 + [30] * 3),                      # low recent HRV
    }
    f = find(run_rules(data), "recovery_deficit")
    assert f is not None
    assert f["severity"] == "alert"


def test_overtraining_risk_correlation():
    data = {
        "training_load": series([50] * 7 + [85] * 7),            # high recent load
        "hrv": series([45 - i for i in range(7)]),                # declining HRV
        "resting_hr": series([50, 50, 50, 50, 50, 50, 60]),       # elevated latest
    }
    f = find(run_rules(data), "overtraining_risk")
    assert f is not None
    assert f["severity"] == "alert"


# ---------------------------------------------------------------------------
# Notable-change / anomaly alerts (generic per-metric z-score rule)
# ---------------------------------------------------------------------------

def test_metric_anomaly_triggers_alert_on_large_deviation():
    # 21-day baseline (low variance, alternating 44/46) followed by a trailing
    # 3-day average that's wildly above it -> |z| well past the 3.0 alert bar.
    data = {"vo2max": series(([44, 46] * 10 + [44]) + [70, 70, 70])}
    f = find(run_rules(data), "vo2max_anomaly")
    assert f is not None
    assert f["category"] == "activity"
    assert f["severity"] == "alert"
    assert abs(f["data"]["z_score"]) >= 3.0
    assert f["data"]["baseline_n"] == 21


def test_metric_anomaly_no_trigger_when_recent_matches_baseline():
    # Same low-variance baseline, but the trailing 3 days sit right at the
    # baseline mean -> |z| stays well under the 2.0 warning threshold.
    data = {"training_load": series(([44, 46] * 10 + [44]) + [45, 45, 45])}
    assert find(run_rules(data), "training_load_anomaly") is None


def test_metric_anomaly_skips_metric_with_insufficient_baseline_history():
    # Baseline has real variance (sd=2, not the zero-variance case) so the
    # z-score would otherwise be enormous; only 7 baseline points are
    # available (10 total - 3 trailing) -> below the 10-point minimum, so
    # the metric is skipped purely on baseline-count, not on zero stdev.
    data = {"resting_hr": series([48, 52] * 3 + [50] + [90, 90, 90])}
    assert find(run_rules(data), "resting_hr_anomaly") is None
