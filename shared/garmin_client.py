import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)

GARTH_TOKEN_DIR = Path(os.getenv("GARTH_TOKEN_DIR", "/app/data/.garth"))

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
    percent_fat/percent_hydration/muscle_mass/bone_mass/bmi/basal_met/active_met
    -- see docs/prp/00-design.md SS3.4 for the full mapping table and the FIT
    encoder's truncation caveat (values are floored to 0.01 resolution).
    A None value lands as the FIT invalid sentinel, the correct encoding
    for "not measured". basal_met/active_met are kcal/day, matching
    garminconnect's own convention -- callers (vitalforge-weight's WeightIn)
    already validate in that unit, not kJ.
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
