"""Descriptive personal comparisons from one sanitized local database snapshot."""

import math
import statistics
from datetime import date, timedelta

from vitalforge_dashboard.data_quality import STALE_AFTER_DAYS, metric_snapshot
from vitalforge_dashboard.readiness import WEIGHTS, score_readiness
from vitalforge_dashboard.recommendations import avg, run_rules, stdev, trend_slope

BAND_METRICS = frozenset({"sleep_duration", "sleep_score", "hrv", "resting_hr", "stress"})
READINESS_METRICS = {"hrv": "hrv", "rhr": "resting_hr", "sleep_score": "sleep_score"}


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _mean(rows: list[dict]) -> float | None:
    return _finite(statistics.mean(row["value"] for row in rows)) if rows else None


def _period(end: date, days: int) -> dict:
    return {"start_date": (end - timedelta(days=days - 1)).isoformat(), "end_date": end.isoformat(), "days": days}


def _within(rows: list[dict], period: dict) -> list[dict]:
    return [row for row in rows if period["start_date"] <= row["date"] <= period["end_date"]]


def _fresh(rows: list[dict], today: date) -> bool:
    return bool(rows) and all((today - date.fromisoformat(row["date"])).days <= STALE_AFTER_DAYS for row in rows)


def _algorithm_safe(rows: list[dict]) -> bool:
    """Existing scoring helpers use sums/squares: refuse overflow, never rescale inputs."""
    if not rows:
        return True
    try:
        values = [row["value"] for row in rows]
        return all(value is None or math.isfinite(value) for value in (
            avg(values), stdev(values), trend_slope(rows),
        ))
    except (ArithmeticError, ValueError):
        return False


def _all_finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_finite(item) for item in value)
    return True


def _comparison(rows: list[dict], metadata: dict, current: dict, previous: dict) -> dict:
    current_rows, previous_rows = _within(rows, current), _within(rows, previous)
    current_mean, previous_mean = _mean(current_rows), _mean(previous_rows)
    daily = metadata["expected_cadence"] == "daily"
    minimum = max(3, math.ceil(current["days"] / 2))
    status, difference, percent, baseline = "insufficient_data", None, None, None
    notes = []
    if not daily:
        status = "snapshot_only"
        notes.append("Counts and means describe stored snapshots, which may repeat an older measurement; no daily coverage or change interpretation is inferred.")
    elif metadata["freshness"] == "stale":
        status = "stale"
    elif min(len(current_rows), len(previous_rows)) >= minimum:
        difference = _finite(current_mean - previous_mean)
        percent = _finite(difference / previous_mean * 100) if difference is not None and previous_mean else None
        status = "available" if difference is not None else "insufficient_data"
        if status == "available" and metadata["name"] in BAND_METRICS:
            try:
                sd = _finite(statistics.stdev(row["value"] for row in previous_rows))
                lower, upper = (_finite(previous_mean - sd), _finite(previous_mean + sd)) if sd is not None else (None, None)
            except (ArithmeticError, ValueError):
                sd, lower, upper = None, None, None
            if lower is not None and upper is not None:
                baseline = {"method": "previous_period_mean_plus_minus_sample_sd", "n": len(previous_rows),
                            "mean": previous_mean, "sample_sd": sd, "lower": lower, "upper": upper}
            else:
                status, difference, percent = "insufficient_data", None, None
                notes.append("Finite observations exceed the supported arithmetic range; interpretation is unavailable.")
        elif status != "available":
            notes.append("Finite observations exceed the supported arithmetic range; interpretation is unavailable.")
    indexed = {row["date"]: row["value"] for row in current_rows}
    start = date.fromisoformat(current["start_date"])
    return {
        **{key: metadata[key] for key in ("label", "unit", "expected_cadence", "latest_observation_date", "freshness")},
        "current": {"n": len(current_rows), "mean": current_mean},
        "previous": {"n": len(previous_rows), "mean": previous_mean},
        "difference": difference, "percent_change": percent, "status": status, "minimum_samples": minimum,
        "baseline": baseline, "notes": notes,
        "data": [{"date": (start + timedelta(days=i)).isoformat(),
                  "value": indexed.get((start + timedelta(days=i)).isoformat())} for i in range(current["days"])],
    }


def _findings(data: dict, metadata: dict, today: date) -> list[dict]:
    findings = []
    for metric, rows in data.items():
        if metadata[metric]["expected_cadence"] != "daily" or not _algorithm_safe(rows):
            continue
        recent, baseline = rows[-3:], rows[-24:-3]
        if len(recent) < 3 or len(baseline) < 10 or not _fresh(recent, today):
            continue
        try:
            # Per-metric execution isolates numerical failures without changing anomaly rules.
            candidates = run_rules({metric: rows})
        except (ArithmeticError, ValueError):
            continue
        for finding in candidates:
            if finding["rule"] != f"{metric}_anomaly" or not _all_finite(finding):
                continue
            baseline_sd = stdev([row["value"] for row in baseline])
            constant = baseline_sd == 0
            score = finding["data"]["z_score"]
            evidence = {
                "method": "latest_3_observations_vs_preceding_up_to_21",
                "recent_dates": [row["date"] for row in recent], "baseline_dates": [row["date"] for row in baseline],
                "recent_n": len(recent), "baseline_n": len(baseline),
                "recent_avg": avg([row["value"] for row in recent]),
                "baseline_mean": avg([row["value"] for row in baseline]), "baseline_sd": baseline_sd,
                "z_score": None if constant else score, "assigned_rule_score": score if constant else None,
                "z_score_kind": "constant_baseline_rule_flag" if constant else "computed", "freshness": "current",
            }
            if not _all_finite(evidence):
                continue
            finding = {**finding, "metric": metric, "label": metadata[metric]["label"], "unit": metadata[metric]["unit"],
                       "data": {**finding["data"], "z_score": evidence["z_score"]}, "evidence": evidence}
            if constant:
                finding["message"] = f"{metadata[metric]['label']} differs from its constant historical baseline; the existing rule assigns an alert flag, not an estimated z-score."
            findings.append(finding)
    return findings


def _readiness(data: dict, metadata: dict, today: date) -> dict:
    safe_data, unsafe = {}, []
    for metric in READINESS_METRICS.values():
        rows = data[metric]
        if _algorithm_safe(rows):
            safe_data[metric] = rows
        else:
            unsafe.append(metric)
    try:
        result = score_readiness(safe_data)
    except (ArithmeticError, ValueError):
        # A ratio can still overflow even when sums and variances are finite.
        result = {"score": None, "components": dict.fromkeys(WEIGHTS), "status": "insufficient_data"}
        unsafe = list(READINESS_METRICS.values())
    details = {}
    present_weight = sum(WEIGHTS[key] for key, value in result["components"].items() if value is not None)
    explanations = {
        "hrv": "HRV compares the latest 3 observations with the preceding observations in this 30-date window; at least 5 observations and a nonzero baseline are required.",
        "rhr": "Resting HR compares the latest observation with the preceding baseline (60%) and the slope of up to 14 observations (40%); at least 5 observations and a nonzero baseline are required.",
        "sleep_score": "Sleep uses the average of the latest 3 Garmin sleep-score observations; at least 5 observations are required.",
    }
    for key, metric in READINESS_METRICS.items():
        rows = data[metric]
        recent_size = 1 if key == "rhr" else 3
        recent = rows[-recent_size:]
        available = result["components"][key] is not None
        details[key] = {
            "metric": metric, "label": metadata[metric]["label"], "weight": WEIGHTS[key],
            "effective_weight": WEIGHTS[key] / present_weight if available else 0,
            "available": available, "freshness": "current" if _fresh(recent, today) else "stale" if rows else "unknown",
            "latest_observation_date": metadata[metric]["latest_observation_date"], "observation_count": len(rows),
            "recent_dates": [row["date"] for row in recent],
            "baseline_dates": [row["date"] for row in rows[:-recent_size]] if key != "sleep_score" else [],
            "trend_dates": [row["date"] for row in rows[-14:]] if key == "rhr" else [],
            "explanation": explanations[key],
        }
    available = [value for value in details.values() if value["available"]]
    freshness = "stale" if any(value["freshness"] != "current" for value in available) else "current" if available else "unknown"
    notes = ["Available component weights are renormalized; missing components are not scored as zero.",
             "Windows use observations, not necessarily consecutive calendar days. This is the existing descriptive readiness score, not a clinical assessment."]
    if freshness == "stale":
        notes.append("Some scored observations are stale. This historical score does not describe current health.")
    if unsafe:
        notes.append("Some observations exceed the supported arithmetic range; their scoring inputs are unavailable.")
    return {**result, "freshness": freshness, "component_details": details,
            "explanation": "Existing readiness formula: HRV 40%, resting HR 30%, Garmin sleep score 30%.", "notes": notes}


async def comparison_report(person_id: int, days: int) -> dict:
    quality, data = await metric_snapshot(person_id, max(2 * days, 30))
    today = date.fromisoformat(quality["period"]["end_date"])
    current, previous = _period(today, days), _period(today - timedelta(days=days), days)
    finding_period = _period(today, 30)
    recent_data = {metric: _within(rows, finding_period) for metric, rows in data.items()}
    metadata = quality["metrics"]
    return {
        "generated_at": quality["generated_at"], "current_period": current, "previous_period": previous,
        "notes": [
            "Periods contain equal numbers of calendar dates, inclusive, ending on today's UTC date. Today may be incomplete.",
            "Observation dates are Garmin source calendar dates; their timezone is unknown. Missing dates are not imputed.",
            "Daily differences need at least half the dates (rounded up), and at least 3 observations, in each period; stale data suppresses comparison.",
            "Bands show the previous period mean plus or minus one sample standard deviation: descriptive historical spread, not a normal range, reference range, or confidence interval.",
            "Findings and readiness always use the last 30 calendar dates, independently of the selected comparison period.",
        ],
        "metrics": {metric: _comparison(rows, {**metadata[metric], "name": metric}, current, previous) for metric, rows in data.items()},
        "findings_period": finding_period, "findings": _findings(recent_data, metadata, today),
        "readiness": _readiness(recent_data, metadata, today),
    }
