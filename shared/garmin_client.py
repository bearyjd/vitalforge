import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)

GARTH_TOKEN_DIR = Path(os.getenv("GARTH_TOKEN_DIR", "/app/data/.garth"))

_client: Garmin | None = None


def authenticate():
    """Authenticate with Garmin Connect using garminconnect.

    Attempts to load saved tokens first. If that fails, performs a fresh
    login with credentials from environment variables and persists the
    new tokens.
    """
    global _client

    GARTH_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = str(GARTH_TOKEN_DIR)

    # Try resuming from saved tokens first
    try:
        client = Garmin()
        client.login(tokenstore=token_path)
        logger.info("Resumed Garmin session from saved tokens")
        _client = client
        # Re-save tokens to keep them fresh
        _client.garth.dump(token_path)
        return
    except Exception:
        pass

    # Fresh login with credentials
    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    logger.info("Performing fresh Garmin login for %s", email)
    client = Garmin(email=email, password=password)
    client.login()
    client.garth.dump(token_path)
    logger.info("Garmin tokens saved to %s", GARTH_TOKEN_DIR)
    _client = client


def get_client() -> Garmin:
    """Return the authenticated Garmin client, authenticating if needed."""
    if _client is None:
        authenticate()
    return _client


# ---------------------------------------------------------------------------
# Push methods
# ---------------------------------------------------------------------------

def push_weight(weight_grams: int, timestamp: datetime | None = None):
    """Push a weight measurement to Garmin Connect via FIT file upload."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    weight_kg = weight_grams / 1000.0
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Pushing weight to Garmin: %.1f kg (%.0f g) at %s", weight_kg, weight_grams, ts_str)
    result = get_client().add_body_composition(
        timestamp=ts_str,
        weight=weight_kg,
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
