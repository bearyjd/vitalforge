"""The metric-key -> table mapping.

Its own module because several modules need it and app.py imports the route
modules -- so anything they share cannot live in app.py without a cycle.

Every module that imports this name binds the same dict object. A test that
needs to retire a metric must use `monkeypatch.setitem`/`delitem` here, which
every consumer sees; rebinding the name on one importer reaches only that
module's validation and silently leaves the rest on the full mapping. That is
the opposite of the rule for imported *functions*, which are rebound per
namespace and must be patched on each calling module -- one mutable object
shared by identity behaves the other way round.
"""

METRIC_TABLES = {
    "sleep_duration": ("sleep", "duration_seconds"),
    "sleep_score": ("sleep", "sleep_score"),
    "resting_hr": ("resting_hr", "value"),
    "hrv": ("hrv", "last_night_avg"),
    "body_battery": ("body_battery", "highest"),
    "body_battery_low": ("body_battery", "lowest"),
    "stress": ("stress", "avg_level"),
    "vo2max": ("vo2max", "vo2max_value"),
    "weight": ("weight_history", "weight_grams"),
    "body_fat": ("weight_history", "body_fat"),
    "body_water": ("weight_history", "body_water"),
    "bone_mass": ("weight_history", "bone_mass_g"),
    "muscle_mass": ("weight_history", "muscle_mass_g"),
    "training_load": ("training_load", "acute_load"),
    "steps": ("steps", "value"),
    "active_calories": ("active_calories", "value"),
}
