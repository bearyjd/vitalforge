"""Meanings of the dashboard's stored values, without claiming original provenance."""

SOURCE = "Garmin Connect via local sync; original measurement origin is unknown (including manual entry)."


def _metric(label: str, unit: str, description: str, cadence: str = "daily") -> dict:
    return {
        "label": label,
        "unit": unit,
        "description": description,
        "source": SOURCE,
        "expected_cadence": cadence,
    }


METRIC_METADATA = {
    "sleep_duration": _metric("Sleep duration", "seconds", "Total sleep duration reported for the source date."),
    "sleep_score": _metric("Sleep score", "provider score", "Garmin sleep score on its provider scale; not a percentage."),
    "resting_hr": _metric("Resting heart rate", "bpm", "Resting heart rate reported for the source date."),
    "hrv": _metric("Overnight HRV", "ms", "Last night's average heart-rate variability reported by Garmin."),
    "body_battery": _metric(
        "Body Battery high", "provider score", "Highest daily Body Battery on Garmin's scale; not a percentage."
    ),
    "body_battery_low": _metric(
        "Body Battery low", "provider score", "Lowest daily Body Battery on Garmin's scale; not a percentage."
    ),
    "stress": _metric("Stress", "provider score", "Average Garmin stress level on its provider scale; not a percentage."),
    "vo2max": _metric(
        "VO2 max estimate", "mL/kg/min",
        "Garmin's derived VO2 max estimate; stored snapshots may repeat an older estimate, not a new measurement.",
        "event_or_snapshot",
    ),
    "weight": _metric("Weight", "g", "Body weight in grams; original measurement method is unknown.", "event_or_snapshot"),
    "body_fat": _metric("Body fat", "%", "Body-fat percentage; availability depends on the original source.", "event_or_snapshot"),
    "body_water": _metric("Body water", "%", "Body-water percentage; availability depends on the original source.", "event_or_snapshot"),
    "bone_mass": _metric("Bone mass", "g", "Reported bone mass in grams; original measurement method is unknown.", "event_or_snapshot"),
    "muscle_mass": _metric("Muscle mass", "g", "Reported muscle mass in grams; original measurement method is unknown.", "event_or_snapshot"),
    "training_load": _metric(
        "Training load", "provider load units",
        "Derived Garmin load: sum of monthly aerobic-low, aerobic-high and anaerobic load categories, "
        "or legacy acute-load fallback. These are not a uniform acute-load series. Stored snapshots "
        "are not independent measurements or percentages.",
        "event_or_snapshot",
    ),
    "steps": _metric("Steps", "count", "Daily step count reported by Garmin."),
    "active_calories": _metric("Active calories", "kcal", "Active energy expenditure reported by Garmin for the source date."),
}
