"""Data for the printable clinician report.

One `profile_snapshot` read (2 * days of history, so the previous period is covered) feeds the
existing comparison report and the weekly-review finding builders. Display units are
explicit and independent of the dashboard's unit toggle. The report is descriptive only.
"""

import re
from datetime import datetime

from shared.database import get_db
from vitalforge_dashboard.comparisons import comparison_report
from vitalforge_dashboard.data_quality import profile_snapshot
from vitalforge_dashboard.report_svg import sparkline
from vitalforge_dashboard.weekly_review import LIMITATIONS, build_findings, build_stable

ALLOWED_DAYS = (7, 30, 90)
MIN_SNAPSHOT_DAYS = 30  # comparison_report reads the last 30 dates for findings whatever the period
# Stored unit -> (display unit, divisor). Everything else keeps its stored unit label.
DISPLAY_UNITS = {"seconds": ("h", 3600), "g": ("kg", 1000)}
NOT_A_DIAGNOSIS_MARKER = "not a diagnosis"
APPENDIX_COLUMNS = 3  # date/value pairs per appendix row, so 90 days stay compact on paper
# Neutral wording: the rules' own severities must not read as clinical alarms.
SEVERITY_LABELS = {"alert": "notable", "warning": "worth noting", "info": "for information"}
UNKNOWN_SEVERITY_LABEL = "noted"
SYNC_LABELS = {
    "success": "Successful", "partial": "Completed with errors", "failed": "Failed",
    "backoff": "Waiting to retry", "never_attempted": "Never attempted", "unknown": "Unknown",
    "link_required": "Garmin link required", "auth_failed": "Garmin sign-in failed",
    "rate_limited": "Garmin request limit reached", "network": "Network error", "error": "Error",
}
NEVER_RECORDED = "Never recorded"
# The metadata descriptions of converted metrics name the stored unit ("in grams"); the report shows
# display units, so it must not print a description that contradicts them.
DESCRIPTION_OVERRIDES = {
    "sleep_duration": "Total sleep duration reported for the source date.",
    "weight": "Body weight; original measurement method is unknown.",
    "bone_mass": "Reported bone mass; original measurement method is unknown.",
    "muscle_mass": "Reported muscle mass; original measurement method is unknown.",
}
# Rule prose can say "alert flag"; the report keeps the same neutral vocabulary as its labels.
ALARM_PATTERN = re.compile(r"\b(?:(?P<article>an?)\s+)?(?P<word>alert flag|alert|warning|flag)(?P<suffix>s|ed|ged)?\b", re.I)
NEUTRAL_NOUNS = {"alert flag": "notable marker", "alert": "notice", "warning": "note", "flag": "marker"}
NEUTRAL_PAST = {"alert flag": "marked", "alert": "noted", "warning": "noted", "flag": "marked"}
_DIAGNOSIS_LINES = tuple(line for line in LIMITATIONS if NOT_A_DIAGNOSIS_MARKER in line)
if not _DIAGNOSIS_LINES:  # fail at import, not per request, if the shared wording ever drifts
    raise RuntimeError("weekly review limitations no longer state that the output is not a diagnosis")


def format_number(value: float, signed: bool = False) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return f"+{text}" if signed and not text.startswith("-") and text != "0" else text


def _shown(value: float | None, divisor: int, signed: bool = False) -> str | None:
    return None if value is None else format_number(value / divisor, signed)


def completeness(observed: int, days: int) -> str:
    return "complete" if observed == days else "partial" if observed else "empty"


def _coverage(observed: int, days: int) -> dict:
    return {
        "observed_days": observed, "missing_days": days - observed, "days": days,
        "coverage_percent": round(observed / days * 100, 1), "completeness": completeness(observed, days),
    }


def _interpretation(name: str, item: dict, stable: set[str], divisor: int, unit: str) -> dict:
    status = item["status"]
    if status == "available" and name in stable:
        return {"kind": "stable", "text": "Stable: the current and previous period means are equal.",
                "difference": None, "percent": None}
    if status == "available" and _shown(item["difference"], divisor, True) == "0":
        return _negligible()
    if status == "available":
        percent = item["percent_change"]
        return {"kind": "changed", "text": "Means differ between the two periods; this does not establish a cause.",
                "difference": f"{_shown(item['difference'], divisor, True)} {unit}",
                "percent": None if percent is None else f"{format_number(percent, True)}%"}
    needed = item["minimum_samples"]
    texts = {
        "insufficient_data": f"Insufficient data: at least {needed} observations are needed in each period; change not interpreted.",
        "stale": f"Stale data: latest observation {item['latest_observation_date']}; change not interpreted.",
        "snapshot_only": "Snapshot metric: stored values can repeat an older measurement; change not interpreted.",
    }
    return {"kind": status, "text": texts.get(status, texts["insufficient_data"]), "difference": None, "percent": None}


def _negligible() -> dict:
    return {"kind": "negligible", "difference": None, "percent": None,
            "text": "No meaningful change at the displayed precision: the means differ by less than the last digit shown."}


def _baseline(item: dict, divisor: int, unit: str) -> dict | None:
    band = item["baseline"]
    if band is None:
        return None
    return {"lower": _shown(band["lower"], divisor), "upper": _shown(band["upper"], divisor),
            "n": band["n"], "unit": unit}


def _metric_view(name: str, item: dict, metadata: dict, stable: set[str], days: int) -> dict:
    unit, divisor = DISPLAY_UNITS.get(metadata["unit"], (metadata["unit"], 1))
    values = [None if row["value"] is None else row["value"] / divisor for row in item["data"]]
    return {
        "name": name, "label": metadata["label"], "unit": unit, "source": metadata["source"],
        "description": DESCRIPTION_OVERRIDES.get(name, metadata["description"]), "cadence": item["expected_cadence"],
        "latest_observation_date": item["latest_observation_date"],
        "coverage": _coverage(item["current"]["n"], days),
        "current_mean": _shown(item["current"]["mean"], divisor), "current_n": item["current"]["n"],
        "previous_mean": _shown(item["previous"]["mean"], divisor), "previous_n": item["previous"]["n"],
        "interpretation": _interpretation(name, item, stable, divisor, unit),
        "baseline": _baseline(item, divisor, unit),
        "spark": sparkline(values), "scale": _scale(values, unit),
        "appendix": _appendix_rows([{"date": row["date"], "value": format_number(value)}
                                    for row, value in zip(item["data"], values) if value is not None]),
    }


def _scale(values: list[float | None], unit: str) -> str | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return f"Plot range: {format_number(min(present))} to {format_number(max(present))} {unit} (each plot spans its own minimum to maximum)"


def _appendix_rows(cells: list[dict]) -> list[list[dict]]:
    return [cells[i:i + APPENDIX_COLUMNS] for i in range(0, len(cells), APPENDIX_COLUMNS)]


def _sync_view(sync: dict) -> dict:
    return {
        "last_attempt_at": sync["last_attempt_at"] or NEVER_RECORDED,
        "result": SYNC_LABELS.get(sync["last_result"], "Unknown"),
        "state": SYNC_LABELS.get(sync["status"], "Unknown"),
        "backoff_until": sync["backoff_until"],
    }


def _swap_alarm_word(match: re.Match) -> str:
    word, suffix = match["word"].lower(), (match["suffix"] or "").lower()
    if suffix in ("ed", "ged"):
        replacement = NEUTRAL_PAST[word]
    else:
        replacement = NEUTRAL_NOUNS[word] + ("s" if suffix else "")
    if match["article"]:  # re-pick a/an for the new first letter, keeping the original capitalization
        article = "an" if replacement[0] in "aeiou" else "a"
        return f"{article.capitalize() if match['article'][0].isupper() else article} {replacement}"
    return replacement.capitalize() if match["word"][0].isupper() else replacement


def _neutral(message: str) -> str:
    return ALARM_PATTERN.sub(_swap_alarm_word, message)


def _findings_view(comparison: dict, data: dict) -> list[dict]:
    start = comparison["findings_period"]["start_date"]
    recent = {metric: [row for row in rows if row["date"] >= start] for metric, rows in data.items()}
    return [{"label": item["label"], "severity": SEVERITY_LABELS.get(item["severity"], UNKNOWN_SEVERITY_LABEL), "message": _neutral(item["message"]),
             "dates": sorted(item["observation_dates"]),
             "baseline_dates": sorted(item.get("evidence", {}).get("baseline_dates") or [])} for item in build_findings(comparison, recent)]


def _context_view(context: dict | None, period: dict) -> list[dict]:
    """Logged days inside the selected period that carry a note or tags (the read covers 2 * days)."""
    entries = (context or {}).get("entries", [])
    return [{"date": entry["date"], "note": entry["note"], "tags": list(entry["tags"])}
            for entry in entries
            if period["start_date"] <= entry["date"] <= period["end_date"] and (entry["note"] or entry["tags"])]


def _limitations(comparison: dict) -> list[str]:
    # The report shows no readiness score, so the comparison note that mentions it is left out.
    return [*(note for note in comparison["notes"] if "readiness" not in note.lower()), *_DIAGNOSIS_LINES]


async def profile_display_name(person_id: int) -> str | None:
    """The profile's display name, or None when the profile is archived or does not exist."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT display_name FROM persons WHERE id = ? AND archived_at IS NULL", (person_id,))
        row = await cursor.fetchone()
        await cursor.close()
    finally:
        await db.close()
    return row["display_name"] if row else None


async def build_report(person_id: int, days: int, *, include_identity: bool = False,
                       include_context: bool = False) -> dict:
    if days not in ALLOWED_DAYS:
        raise ValueError(f"days must be one of {ALLOWED_DAYS}")
    quality, data, context = await profile_snapshot(
        person_id, max(2 * days, MIN_SNAPSHOT_DAYS), include_context=include_context)
    comparison = await comparison_report(person_id, days, (quality, data))
    stable = {item["metric"] for item in build_stable(comparison, data)}
    views = [_metric_view(name, item, quality["metrics"][name], stable, days)
             for name, item in comparison["metrics"].items()]
    shown = [view for view in views if view["coverage"]["observed_days"]]
    return {
        "days": days, "generated_at": datetime.fromisoformat(quality["generated_at"]).strftime("%Y-%m-%d %H:%M UTC"),
        "period": comparison["current_period"], "previous_period": comparison["previous_period"],
        "window_timezone": quality["period"]["window_timezone"],
        "date_semantics": quality["period"]["observation_date_semantics"],
        "appendix_columns": APPENDIX_COLUMNS, "sync": _sync_view(quality["sync"]), "empty": not shown, "metrics": shown,
        "unrecorded": [{"label": view["label"], "latest": view["latest_observation_date"]}
                       for view in views if not view["coverage"]["observed_days"]],
        "findings_period": comparison["findings_period"], "findings": _findings_view(comparison, data),
        "limitations": _limitations(comparison),
        "display_name": await profile_display_name(person_id) if include_identity else None,
        "context": _context_view(context, comparison["current_period"]) if include_context else None,
    }
