"""Descriptive same-date associations; no causal or treatment interpretation."""

import math
from collections import Counter
from datetime import date

from vitalforge_dashboard.context_models import DAILY_METRICS
from vitalforge_dashboard.data_quality import STALE_AFTER_DAYS
from vitalforge_dashboard.metric_metadata import METRIC_METADATA

NOTES = [
    "These are same-date context/metric pairs, not matched or randomized participants. Associations do not establish cause.",
    "Only explicitly logged days enter either group. An unlogged date is unknown, not an untagged day.",
    "A saved plan describes intent, not confirmed adherence. Other tags and unrecorded factors may explain a difference.",
    "Self-selection, changes over time and unknown source timezones limit interpretation. Today may be incomplete.",
    "Historical observations do not establish current health. No treatment advice or automatic interventions are generated.",
    "Differences need at least three metric/context pairs in each group; small groups remain uncertain.",
    "Difference is tagged mean minus untagged mean; percent uses the absolute untagged mean and is unavailable at zero.",
]


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _group(entries: list[dict], observations: dict) -> dict:
    paired = [entry["date"] for entry in entries if entry["date"] in observations]
    missing = [entry["date"] for entry in entries if entry["date"] not in observations]
    mean = None
    if paired:
        try:
            mean = _finite(math.fsum(observations[day] / len(paired) for day in paired))
        except OverflowError:
            pass
    return {
        "logged_days": len(entries), "paired_days": len(paired), "missing_metric_days": len(missing),
        "logged_dates": [entry["date"] for entry in entries], "dates": paired, "missing_metric_dates": missing,
        "mean": mean,
    }


def context_associations(quality: dict, data: dict, context: dict) -> dict:
    experiment = context["experiment"]
    period = dict(quality["period"])
    logged = len(context["entries"])
    result = {
        "status": "no_experiment", "period": period, "experiment": experiment, "metric": None,
        "coverage": {"calendar_days": period["days"], "logged_days": logged,
                     "unlogged_days": period["days"] - logged, "metric_missing_days": 0,
                     "logged_percent": round(logged / period["days"] * 100, 1)},
        "groups": {"tagged": _group([], {}), "untagged": _group([], {})},
        "difference": None, "cooccurring_tags": [],
        "flags": ["unknown_timezone", "self_selection", "time_trend"], "notes": NOTES,
    }
    if not experiment:
        return result
    # Existing saved rows are not allowed to turn a metric identifier into SQL.
    if experiment["metric"] not in DAILY_METRICS:
        result["status"] = "insufficient_data"
        return result
    result["metric"] = METRIC_METADATA[experiment["metric"]]
    start = max(period["start_date"], experiment["start_date"])
    end = min(period["end_date"], experiment["end_date"])
    count = max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    result["period"] = {**period, "start_date": start, "end_date": end, "days": count}
    entries = [entry for entry in context["entries"] if start <= entry["date"] <= end]
    observations = {row["date"]: row["value"] for row in data[experiment["metric"]]
                    if start <= row["date"] <= end}
    tag = experiment["tag"]
    tagged = [entry for entry in entries if tag in entry["tags"]]
    untagged = [entry for entry in entries if tag not in entry["tags"]]
    groups = {"tagged": _group(tagged, observations), "untagged": _group(untagged, observations)}
    result["groups"] = groups
    missing = sum(group["missing_metric_days"] for group in groups.values())
    result["coverage"] = {
        "calendar_days": count, "logged_days": len(entries), "unlogged_days": count - len(entries),
        "metric_missing_days": missing, "logged_percent": round(len(entries) / count * 100, 1) if count else 0,
    }
    tag_counts = [Counter(other for entry in group for other in entry["tags"] if other != tag)
                  for group in (tagged, untagged)]
    result["cooccurring_tags"] = [
        {"tag": other, "tagged_days": tag_counts[0][other], "untagged_days": tag_counts[1][other]}
        for other in sorted(set(tag_counts[0]) | set(tag_counts[1]))
    ]
    flags = result["flags"]
    if min(group["paired_days"] for group in groups.values()) < 7:
        flags.append("small_groups")
    if count > len(entries):
        flags.append("unlogged_days")
    if missing:
        flags.append("missing_metric")
    if result["cooccurring_tags"]:
        flags.append("cooccurring_tags")
    paired_dates = groups["tagged"]["dates"] + groups["untagged"]["dates"]
    if paired_dates and (date.fromisoformat(period["end_date"]) - date.fromisoformat(max(paired_dates))).days > STALE_AFTER_DAYS:
        flags.append("historical_pairs")
    enough = all(group["paired_days"] >= 3 and group["mean"] is not None for group in groups.values())
    result["status"] = "outside_period" if not count else "available" if enough else "insufficient_data"
    if enough:
        difference = _finite(groups["tagged"]["mean"] - groups["untagged"]["mean"])
        baseline = groups["untagged"]["mean"]
        if difference is not None:
            percent = _finite(difference / abs(baseline) * 100) if baseline else None
            result["difference"] = {"absolute": difference, "percent": percent}
        else:
            result["status"] = "insufficient_data"
            flags.append("nonfinite_summary")
    elif any(group["paired_days"] and group["mean"] is None for group in groups.values()):
        flags.append("nonfinite_summary")
    return result
