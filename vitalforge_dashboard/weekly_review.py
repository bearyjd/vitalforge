"""On-demand weekly review: deterministic, rules-only, built from existing local reports.

Composes the 7-day comparison report, the data-quality snapshot and the existing
rules engine. It adds no score, no prose generation and never reads context
notes or tags. Every item names its metric, period and supporting dates.
"""

import math
from datetime import date, datetime

from vitalforge_dashboard.comparisons import comparison_report
from vitalforge_dashboard.data_quality import STALE_AFTER_DAYS, profile_snapshot
from vitalforge_dashboard.goals import progress_from_rows
from vitalforge_dashboard.metric_metadata import METRIC_METADATA
from vitalforge_dashboard.recommendations import run_rules

SCHEMA_VERSION = "1.0"
WEEK_DAYS = 7
FINDINGS_DAYS = 30
# Goal series use /api/goals' basis: get_metric(days=90) keeps dates >= today - 90, i.e. 91 dates.
GOAL_SNAPSHOT_DAYS = 91
STABLE_REL_TOL = 1e-9
EMPTY_MESSAGE = "No data recorded this week"
SEVERITY_RANK = {"alert": 0, "warning": 1, "info": 2}
UNRANKED_SEVERITY = len(SEVERITY_RANK)
FAILED_SYNC_STATUSES = frozenset({"failed", "backoff"})
LAST_7 = slice(-7, None)
LAST_14 = slice(-14, None)
ALL_ROWS = slice(None)
# rule -> (metric, rows the rule reads as its current window, rows it compares them with or None).
# Mirrors vitalforge_dashboard/recommendations.py; the rules read observations, not calendar days.
RULE_EVIDENCE = {
    "sleep_low_duration": ("sleep_duration", LAST_7, None),
    "sleep_declining": ("sleep_duration", LAST_14, None),
    "sleep_low_score": ("sleep_score", LAST_7, None),
    "hrv_below_baseline": ("hrv", slice(-10, None), ALL_ROWS),
    "hrv_weekly_drop": ("hrv", LAST_7, slice(-14, -7)),
    "rhr_elevated": ("resting_hr", slice(-1, None), ALL_ROWS),
    "rhr_trending_up": ("resting_hr", LAST_14, None),
    "body_battery_low": ("body_battery", LAST_7, None),
    "stress_high": ("stress", LAST_7, None),
    "stress_trending_up": ("stress", LAST_14, None),
}
RULE_METRICS = {rule: spec[0] for rule, spec in RULE_EVIDENCE.items()}
# Observation-based wording: the rules count recorded observations, which may skip calendar dates.
RULE_MESSAGES = {
    "sleep_low_duration": "Sleep duration was under 7 hours in each of the latest {consecutive_days} recorded observations.",
    "sleep_declining": "Sleep duration trends downward across the latest {n} recorded observations.",
    "sleep_low_score": "Sleep score was under 70 in each of the latest {consecutive_days} recorded observations.",
    "hrv_below_baseline": "Overnight HRV was below its observed 30-date average in each of the latest {consecutive_days} recorded observations.",
    "hrv_weekly_drop": "Overnight HRV averaged {drop}% lower over the latest 7 recorded observations than over the 7 before them.",
    "rhr_elevated": "The latest resting heart rate ({current} bpm) is above its observed 30-date average ({baseline} bpm).",
    "rhr_trending_up": "Resting heart rate trends upward across the latest {n} recorded observations.",
    "body_battery_low": "Body Battery high stayed under 80 in each of the latest {consecutive_days} recorded observations.",
    "stress_high": "Average stress was above 50 in each of the latest {consecutive_days} recorded observations.",
    "stress_trending_up": "Stress trends upward across the latest {n} recorded observations.",
}
LIMITATIONS = (
    "Descriptive summary of recorded observations only; differences do not establish a cause.",
    "Periods are the last 7 calendar dates ending today (UTC) and the 7 before them; today may be incomplete.",
    "Comparisons need enough observations in both weeks; otherwise they are left out, not estimated.",
    "Findings use the existing rules over the last 30 calendar dates and only for currently fresh metrics.",
    "Observation dates are Garmin source calendar dates; their timezone is unknown. Missing dates are not imputed.",
    "This review is not a diagnosis, clinical assessment or treatment advice.",
)


def _finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(item) for item in value)
    return True


def _dates_in(rows: list[dict], period: dict) -> list[str]:
    return [row["date"] for row in rows if period["start_date"] <= row["date"] <= period["end_date"]]


def _finding_order(finding: dict) -> tuple:
    return (SEVERITY_RANK.get(finding["severity"], UNRANKED_SEVERITY), finding["metric"], finding.get("rule", ""))


def _change_items(comparison: dict, data: dict) -> list[dict]:
    items = []
    for metric, item in comparison["metrics"].items():
        if item["status"] != "available" or item["difference"] is None:
            continue
        sides = {}
        for side, period in (("current", comparison["current_period"]), ("previous", comparison["previous_period"])):
            sides[side] = {
                "start_date": period["start_date"], "end_date": period["end_date"],
                "n": item[side]["n"], "mean": item[side]["mean"],
                "dates": _dates_in(data.get(metric, []), period),
            }
        items.append({
            "metric": metric, "label": item["label"], "unit": item["unit"], **sides,
            "difference": item["difference"], "percent_change": item["percent_change"],
        })
    return items


def _is_stable(change: dict) -> bool:
    return change["difference"] == 0 or math.isclose(
        change["current"]["mean"], change["previous"]["mean"], rel_tol=STABLE_REL_TOL)


def build_changes(comparison: dict, data: dict) -> list[dict]:
    changed = [c for c in _change_items(comparison, data) if not _is_stable(c)]
    return sorted(changed, key=lambda c: (c["percent_change"] is None, -abs(c["percent_change"] or 0), c["metric"]))


def build_stable(comparison: dict, data: dict) -> list[dict]:
    """Metrics with enough observations in both weeks whose means are equal: stable, not missing."""
    return sorted((c for c in _change_items(comparison, data) if _is_stable(c)), key=lambda c: c["metric"])


def build_coverage(comparison: dict) -> list[dict]:
    items = []
    for metric, item in sorted(comparison["metrics"].items()):
        if item["expected_cadence"] != "daily":
            continue
        observed = item["current"]["n"]
        completeness = "complete" if observed >= WEEK_DAYS else "partial" if observed else "empty"
        if completeness == "complete" and item["freshness"] != "stale":
            continue
        items.append({
            "metric": metric, "label": item["label"], "unit": item["unit"], "freshness": item["freshness"],
            "completeness": completeness, "observed_days": observed, "missing_days": WEEK_DAYS - observed,
            "latest_observation_date": item["latest_observation_date"],
        })
    return items


def _rule_finding(finding: dict, metric: str, rows: list[dict], comparison: dict) -> dict:
    _, current_slice, baseline_slice = RULE_EVIDENCE[finding["rule"]]
    streak = finding["data"].get("consecutive_days")
    if streak:  # only the rows that formed the qualifying run, not the wider window the rule scanned
        current_slice = slice(-streak, None)
    current = [{"date": row["date"], "value": row["value"]} for row in rows[current_slice]]
    baseline = [{"date": row["date"], "value": row["value"]} for row in rows[baseline_slice]] if baseline_slice else []
    span = (date.fromisoformat(current[-1]["date"]) - date.fromisoformat(current[0]["date"])).days + 1
    values = {**finding["data"], "n": len(current), "drop": abs(finding["data"].get("pct_change", 0))}
    metadata = comparison["metrics"][metric]
    return {
        **finding, "message": RULE_MESSAGES[finding["rule"]].format(**values), "source": "rule", "metric": metric,
        "label": metadata["label"], "unit": metadata["unit"], "period": comparison["findings_period"],
        "observation_dates": [row["date"] for row in current],
        "evidence": {
            "method": "existing_rule_over_recorded_observations", "observations": current,
            "baseline_role": "compared_with" if baseline else None,
            "baseline_dates": [row["date"] for row in baseline], "baseline_observations": baseline,
            "span_days": span,
        },
    }


def _rule_findings(comparison: dict, data: dict) -> list[dict]:
    found = []
    for metric in sorted(set(RULE_METRICS.values())):
        rows = data.get(metric, [])
        if not rows or comparison["metrics"][metric]["freshness"] != "current":
            continue
        try:
            candidates = run_rules({metric: rows})
        except (ArithmeticError, ValueError):
            continue
        found.extend(
            _rule_finding(item, metric, rows, comparison)
            for item in candidates if RULE_METRICS.get(item["rule"]) == metric and _finite(item)
        )
    return found


def build_findings(comparison: dict, data: dict) -> list[dict]:
    anomalies = [
        {**item, "source": "comparison_anomaly", "period": comparison["findings_period"],
         "observation_dates": list(item["evidence"]["recent_dates"])}
        for item in comparison["findings"]
    ]
    return sorted(anomalies + _rule_findings(comparison, data), key=_finding_order)


def _goal_item(goal: dict, data: dict, comparison: dict, now: datetime) -> dict:
    metric = goal["metric"]
    metadata = METRIC_METADATA.get(metric, {})
    week = comparison["metrics"].get(metric)
    rows = data.get(metric, [])
    # `rows` come from profile_snapshot, which never reads dates after today (UTC), whereas /api/goals has
    # no upper bound: a row dated tomorrow is ignored here on purpose (pinned by a test).
    progress = progress_from_rows(rows, goal["target_value"], goal["target_date"], now) if metric in data else None
    latest = rows[-1]["date"] if rows else None
    age = (now.date() - date.fromisoformat(latest)).days if latest else None
    return {
        "goal_id": goal["id"], "metric": metric, "label": metadata.get("label", metric),
        "unit": metadata.get("unit"), "target_value": goal["target_value"], "target_date": goal["target_date"],
        "latest_value": progress.latest_value if progress else None, "latest_observation_date": latest,
        "stale": age > STALE_AFTER_DAYS if age is not None and metadata.get("expected_cadence") == "daily" else None,
        "on_track": progress.on_track if progress else None,
        "eta_date": progress.eta_date if progress else None,
        "week_mean": week["current"]["mean"] if week else None, "week_n": week["current"]["n"] if week else 0,
        "previous_mean": week["previous"]["mean"] if week else None,
        "previous_n": week["previous"]["n"] if week else 0,
    }


def build_goals(goals: list[dict], data: dict, comparison: dict, now: datetime) -> tuple[list[dict], list[str]]:
    """Goal progress from the same snapshot and pinned clock as the rest of the review.

    A goal whose projection cannot be computed (non-finite or out-of-range values) is skipped and
    named in the returned notes, so one bad goal never fails the whole review."""
    items, notes = [], []
    for goal in goals:
        try:
            items.append(_goal_item(goal, data, comparison, now))
        except (ArithmeticError, ValueError):
            label = METRIC_METADATA.get(goal["metric"], {}).get("label", goal["metric"])
            notes.append(f"Progress for your {label} goal could not be calculated and was skipped.")
    return items, notes


def _goal_gap(goal: dict) -> float:
    distance = abs(goal["target_value"] - goal["latest_value"])
    return distance / abs(goal["target_value"]) if goal["target_value"] else distance


def _data_follow_up(stale: list[dict], empty: bool, no_daily: bool) -> dict:
    if empty:
        text = "No data was recorded this week. Sync Garmin or log a measurement, then generate this review again."
    elif no_daily:
        text = ("No daily metrics were recorded this week. Sync Garmin or log a daily measurement, "
                "then generate this review again.")
    elif stale:
        labels = ", ".join(item["label"] for item in stale)
        text = f"Sync Garmin or log a measurement for {labels}, then generate this review again."
    else:
        text = "The last Garmin sync did not complete. Try syncing again, then generate this review again."
    return {"kind": "data", "text": text, "metrics": [item["metric"] for item in stale]}


def choose_follow_up(
    coverage: list[dict], findings: list[dict], goals: list[dict], empty: bool, sync: dict, daily_observed: bool = True,
) -> dict:
    """One fixed-priority, non-diagnostic suggestion: data, then finding, then goal, else none."""
    stale = sorted((item for item in coverage if item["freshness"] == "stale"), key=lambda item: item["metric"])
    no_daily = not daily_observed
    if empty or no_daily or stale or sync.get("status") in FAILED_SYNC_STATUSES:
        return _data_follow_up(stale, empty, no_daily)
    if findings:
        top = min(findings, key=_finding_order)
        dates = sorted(top["observation_dates"])
        text = (f"Look back over your {top['label']} entries for {dates[0]} to {dates[-1]} "
                "and keep recording to see whether this pattern continues.")
        return {"kind": "finding", "text": text, "metrics": [top["metric"]]}
    # Goals have no direction, so only a projection that misses the target date counts as behind.
    behind = [g for g in goals if g["on_track"] is False and g["latest_value"] is not None and g.get("stale") is not True]
    if behind:
        top = min(behind, key=lambda g: (-_goal_gap(g), g["metric"], g.get("goal_id", 0)))
        text = f"Review your {top['label']} goal and decide whether the target and date still suit you."
        return {"kind": "goal", "text": text, "metrics": [top["metric"]]}
    return {"kind": "none", "text": "Nothing stands out this week. Keep recording as usual.", "metrics": []}


async def weekly_review(person_id: int, goals: list[dict]) -> dict:
    """Build the review for one authorized profile; `goals` are the caller's own account goals.

    Everything is derived from one profile snapshot read at one pinned `now`."""
    quality, data, _ = await profile_snapshot(person_id, GOAL_SNAPSHOT_DAYS)
    comparison = await comparison_report(person_id, WEEK_DAYS, (quality, data))
    recent = {metric: [row for row in rows if row["date"] >= comparison["findings_period"]["start_date"]]
              for metric, rows in data.items()}
    now = datetime.fromisoformat(quality["generated_at"])
    # Empty means no observation of any kind (snapshot metrics included) in the week; a week with
    # only snapshot-type data is not empty, but it still lacks daily data (see choose_follow_up).
    empty = not any(item["current"]["n"] for item in comparison["metrics"].values())
    daily_observed = any(item["current"]["n"] for item in comparison["metrics"].values()
                         if item["expected_cadence"] == "daily")
    coverage = build_coverage(comparison)
    findings = build_findings(comparison, recent)
    goal_items, goal_notes = build_goals(goals, data, comparison, now)
    sync = {key: quality["sync"][key] for key in ("status", "last_result", "last_attempt_at")}
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": quality["generated_at"],
        "period": comparison["current_period"], "previous_period": comparison["previous_period"],
        "empty": empty, "message": EMPTY_MESSAGE if empty else None, "sync": sync,
        "changes": build_changes(comparison, recent), "stable": build_stable(comparison, recent),
        "findings": findings, "coverage": coverage,
        "goals": goal_items, "follow_up": choose_follow_up(coverage, findings, goal_items, empty, sync, daily_observed),
        "limitations": [*LIMITATIONS, *goal_notes],
    }
