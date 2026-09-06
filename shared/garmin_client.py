import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)

GARTH_TOKEN_DIR = Path(os.getenv("GARTH_TOKEN_DIR", "/app/data/.garth"))

# Confirmed as the strength WORKOUT sportTypeKey (garminconnect's
# workout.py:293-299), NOT confirmed as an ACTIVITY typeKey -- the library
# bundles no activity-type list and points at Garmin's external
# activity_types.properties instead. One live get_activity_types() call
# settles it; until then this is the best-supported guess and a failed push
# is non-fatal by design (the row keeps garmin_status='failed' and the
# client re-POSTs).
STRENGTH_ACTIVITY_TYPE_KEY = "strength_training"

GRAMS_PER_KG = 1000.0

_client: Garmin | None = None


def authenticate():
    """Authenticate with Garmin Connect using garminconnect."""
    global _client

    GARTH_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = str(GARTH_TOKEN_DIR)

    # garminconnect>=0.3 dropped the `garth` library it used to wrap -- there
    # is no `.garth` attribute anymore, and login(tokenstore=path) now
    # resumes from saved tokens AND persists fresh ones internally in one
    # call (falling back to self.username/self.password when nothing valid
    # is on disk). The 2026-08-22 upgrade to ==0.3.11 (for
    # add_body_composition) kept the old separate resume/`.garth.dump()`
    # code here, which silently broke resume on every request and forced a
    # real credential login every time, triggering a Garmin 429 (see
    # docs/prp/03-live-validation.md's "2026-08-22 incident" section).
    # tests/test_garmin_client_api.py guards this API surface -- re-run it
    # (and read this function against the new source) before ever bumping
    # this pin again.
    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    client = Garmin(email=email, password=password)
    client.login(tokenstore=token_path)
    _client = client
    logger.info("Garmin authenticated; tokens persisted to %s", GARTH_TOKEN_DIR)


def get_client() -> Garmin:
    """Return the authenticated Garmin client, authenticating if needed."""
    if _client is None:
        authenticate()
    return _client


# ---------------------------------------------------------------------------
# Push methods
# ---------------------------------------------------------------------------

def push_weight(
    weight_grams: int,
    timestamp: datetime | None = None,
    *,
    percent_fat: float | None = None,
    percent_hydration: float | None = None,
    muscle_mass_kg: float | None = None,
    bone_mass_kg: float | None = None,
    bmi: float | None = None,
    basal_met: float | None = None,
    active_met: float | None = None,
):
    """Push a weight measurement, and optionally body composition, to Garmin
    Connect via FIT file upload.

    Composition kwargs map straight through to add_body_composition's
    percent_fat/percent_hydration/bone_mass/muscle_mass/bmi/basal_met/active_met
    -- see docs/prp/00-design.md SS3.4 for the FIT field table (row 221/223
    there gives each field's own scale factor). A None value lands as the
    FIT invalid sentinel, the correct encoding for "not measured".
    percent_fat/percent_hydration/bone_mass/muscle_mass are floored to 0.01
    resolution (scale 100); bmi is floored to 0.1 (scale 10, FIT field 13);
    basal_met/active_met are floored to 0.25 kcal (scale 4, FIT fields 7/9)
    -- coarser than the other four, not a uniform 0.01 across every kwarg.
    basal_met/active_met are kcal/day, matching garminconnect's own
    convention -- callers (vitalforge-weight's WeightIn) already validate in
    that unit, not kJ. NOTE: vitalforge-weight's _push_composition
    deliberately never passes bmi (00-design.md SS3.4 already rejects
    sending it -- Garmin derives its own from weight + profile height, and
    vitalforge-dashboard/sync.py reads that value back); the kwarg exists
    here because it's a real, valid add_body_composition parameter a future
    caller might have a legitimate reason to set explicitly, not because
    anything currently calls push_weight with it.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    weight_kg = weight_grams / 1000.0
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Pushing weight to Garmin: %.1f kg (%.0f g) at %s", weight_kg, weight_grams, ts_str)
    result = get_client().add_body_composition(
        timestamp=ts_str,
        weight=weight_kg,
        percent_fat=percent_fat,
        percent_hydration=percent_hydration,
        muscle_mass=muscle_mass_kg,
        bone_mass=bone_mass_kg,
        bmi=bmi,
        basal_met=basal_met,
        active_met=active_met,
    )
    logger.info("add_body_composition response: %s", result)
    logger.info("Weight pushed to Garmin successfully")


def push_activity(
    *,
    start_datetime: str,
    time_zone: str,
    type_key: str,
    distance_km: float,
    duration_min: int,
    activity_name: str,
):
    """Create a completed manual activity on Garmin Connect.

    `start_datetime` must already be a LOCAL WALL-CLOCK string carrying no
    offset ("2026-09-06T10:00:00.000") and `time_zone` the IANA name that
    wall clock belongs to -- garminconnect's own documented contract. This
    function deliberately does NOT do that conversion: the caller
    (vitalforge-weight/app.py's _push_activity) owns it, because a test
    monkeypatches THIS name in the app module's namespace and would
    otherwise be asserting on a conversion the fake performed rather than
    the one the app does.

    Like push_weight, this does not catch -- the caller's never-raise
    wrapper decides what a failure means for the stored row.
    """
    logger.info(
        "Pushing activity to Garmin: %r at %s (%s), %s min",
        activity_name, start_datetime, time_zone, duration_min,
    )
    result = get_client().create_manual_activity(
        start_datetime=start_datetime,
        time_zone=time_zone,
        type_key=type_key,
        distance_km=distance_km,
        duration_min=duration_min,
        activity_name=activity_name,
    )
    logger.info("create_manual_activity response: %s", result)
    return result


def push_activity_sets(activity_id: str, payload: dict):
    """Attach per-exercise sets to an existing activity.

    PUT semantics are REPLACE-ALL: the activity's existing exerciseSets
    array is overwritten wholesale. Only ever called behind the
    VITALFORGE_GARMIN_EXERCISE_SETS flag, which ships off -- see
    build_exercise_sets_payload for why.
    """
    result = get_client().set_activity_exercise_sets(activity_id, payload)
    logger.info("set_activity_exercise_sets response: %s", result)
    return result


def extract_activity_id(response) -> str | None:
    """Pull the new activity's id out of create_manual_activity's response.

    Returns None rather than raising or guessing when the response is not a
    dict, or carries none of the keys Garmin has been observed to use. The
    caller treats that as "the activity was created but we cannot address
    it", NOT as a failure -- the activity really is on Garmin.

    Deliberately does NOT fall back to get_last_activity(): that read is
    racy (any other device syncing at the same moment wins) and would
    silently attach one session's exercise sets to a different activity.
    """
    if not isinstance(response, dict):
        return None
    for key in ("activityId", "activityid", "id"):
        value = response.get(key)
        if value is not None:
            return str(value)
    nested = response.get("activityIds")
    if isinstance(nested, list) and nested:
        return str(nested[0])
    return None


def build_exercise_sets_payload(exercises: list[dict], start_local: datetime) -> dict:
    """Build the set_activity_exercise_sets request body, one entry per set.

    THE FIELD NAMES HERE ARE UNVERIFIED. A grep for `repetitionCount`,
    `setType` and `exerciseSets` across the whole installed garminconnect
    0.3.11 tree returns only the two method definitions -- the JSON keys
    appear nowhere in the package or its metadata. This shape is inferred
    from the set_activity_exercise_sets docstring (which documents
    `exercises[].category` / `exercises[].name` and Garmin's 400 "Invalid
    Sub-Category Passed") and from Garmin's public FIT `set` message.
    Weight is BELIEVED to be grams (matching workout.py:494's kg * 1000.0)
    and `duration` seconds. That is why this whole path sits behind
    VITALFORGE_GARMIN_EXERCISE_SETS, default off, and why the builder is one
    function: JD's live probe against a real account
    (get_activity_exercise_sets on an existing strength activity) settles
    the shape, and changes land here and nowhere else.

    An exercise with no `garmin_category` contributes NO entries. Guessing a
    category to fill the gap would file the wrong movement under a real
    Garmin exercise; the activity itself is created either way, which is the
    part that matters.
    """
    entries: list[dict] = []
    cursor = start_local
    for exercise in exercises:
        category = exercise.get("garmin_category")
        if category is None:
            continue
        seconds = exercise.get("seconds")
        weight_kg = exercise.get("weight_kg")
        for _ in range(int(exercise["sets"])):
            entry = {
                "setType": "ACTIVE",
                "startTime": cursor.strftime("%Y-%m-%dT%H:%M:%S.0"),
                "repetitionCount": int(exercise["reps"]),
                "exercises": [{"category": category, "name": exercise.get("garmin_exercise")}],
            }
            if seconds is not None:
                entry["duration"] = float(seconds)
            if weight_kg is not None:
                entry["weight"] = float(weight_kg) * GRAMS_PER_KG
            entries.append(entry)
            # Sets are laid end to end from the session start. Nothing in the
            # request carries a real per-set clock, and Garmin needs each set
            # to have *a* start time; work + rest is the closest honest
            # approximation available from what Cadence sends.
            cursor += timedelta(seconds=(seconds or 0) + (exercise.get("rest_s") or 0))
    return {"exerciseSets": entries}


# ---------------------------------------------------------------------------
# Pull methods — each returns raw JSON from Garmin Connect
# ---------------------------------------------------------------------------

def get_sleep_data(date: str) -> dict | None:
    """Get daily sleep data. date: YYYY-MM-DD."""
    try:
        return get_client().get_sleep_data(date)
    except Exception as e:
        logger.warning("Failed to get sleep data for %s: %s", date, e)
        return None


def get_user_summary(date: str) -> dict | None:
    """Get daily user summary (steps, calories, RHR, stress, etc.). date: YYYY-MM-DD."""
    try:
        return get_client().get_user_summary(date)
    except Exception as e:
        logger.warning("Failed to get user summary for %s: %s", date, e)
        return None


def get_hrv_data(date: str) -> dict | None:
    """Get HRV data for a given date. date: YYYY-MM-DD."""
    try:
        return get_client().get_hrv_data(date)
    except Exception as e:
        logger.warning("Failed to get HRV data for %s: %s", date, e)
        return None


def get_body_battery(date: str) -> list | None:
    """Get body battery report for a single day. date: YYYY-MM-DD."""
    try:
        return get_client().get_body_battery(date)
    except Exception as e:
        logger.warning("Failed to get body battery for %s: %s", date, e)
        return None


def get_stress_data(date: str) -> dict | None:
    """Get daily stress data. date: YYYY-MM-DD."""
    try:
        return get_client().get_stress_data(date)
    except Exception as e:
        logger.warning("Failed to get stress data for %s: %s", date, e)
        return None


def get_max_metrics(date: str) -> list | None:
    """Get VO2 Max and fitness metrics. date: YYYY-MM-DD."""
    try:
        return get_client().get_max_metrics(date)
    except Exception as e:
        logger.warning("Failed to get max metrics for %s: %s", date, e)
        return None


def get_weight_range(start_date: str, end_date: str) -> dict | None:
    """Get weight history for a date range. Dates: YYYY-MM-DD."""
    try:
        return get_client().get_weigh_ins(start_date, end_date)
    except Exception as e:
        logger.warning("Failed to get weight range %s to %s: %s", start_date, end_date, e)
        return None


def get_training_status(date: str) -> dict | None:
    """Get training status/load. date: YYYY-MM-DD."""
    try:
        return get_client().get_training_status(date)
    except Exception as e:
        logger.warning("Failed to get training status for %s: %s", date, e)
        return None
