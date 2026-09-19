import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)

# Confirmed as the strength WORKOUT sportTypeKey (garminconnect's
# workout.py:293-299), NOT confirmed as an ACTIVITY typeKey -- the library
# bundles no activity-type list and points at Garmin's external
# activity_types.properties instead. One live get_activity_types() call
# settles it; until then this is the best-supported guess and a failed push
# is non-fatal by design (the row keeps garmin_status='failed' and the
# client re-POSTs).
STRENGTH_ACTIVITY_TYPE_KEY = "strength_training"

GRAMS_PER_KG = 1000.0

_clients: dict[tuple[int, int], Garmin] = {}


def _ensure_token_dir(token_dir: Path) -> Path:
    """Create a token-store directory privately without touching what exists above it.

    ``Path.mkdir(parents=True, mode=...)`` only applies ``mode`` to the leaf
    it creates -- any missing intermediate ancestor is made with the process
    umask instead, mimicking POSIX ``mkdir -p``. Each missing ancestor is
    therefore created 0700 explicitly, walking from the filesystem root
    down; an ancestor that already exists is left alone entirely, since on
    the one-time legacy adoption ``token_dir`` is the token root itself and
    its parent is the data volume that also holds the database.
    ``exist_ok`` does not tighten an existing ``token_dir``, so that one
    level is chmod'ed explicitly too.
    """
    for ancestor in reversed(token_dir.parents):
        ancestor.mkdir(mode=0o700, exist_ok=True)
    token_dir.mkdir(mode=0o700, exist_ok=True)
    token_dir.chmod(0o700)
    return token_dir


def authenticate(
    person_id: int,
    generation: int,
    token_dir: Path,
    email: str,
    password: str | None,
) -> Garmin:
    """Authenticate and cache exactly one person's credential generation.

    ``person_id`` and ``generation`` have no defaults: selecting an account
    is a security boundary.  The caller is the registry, which owns link
    lookup, rate admission, and the cross-process person lock.  ``password``
    is only supplied by the link flow; ordinary resume passes ``None``.
    """
    token_dir = _ensure_token_dir(token_dir)
    client = Garmin(email=email, password=password)
    client.login(tokenstore=str(token_dir))
    _clients[(person_id, generation)] = client
    logger.info("Garmin client authenticated for person %s generation %s", person_id, generation)
    return client


def is_authenticated(person_id: int, generation: int) -> bool:
    """Whether this process has the exact durable link generation cached."""
    return (person_id, generation) in _clients


def get_client(person_id: int, generation: int) -> Garmin:
    """Return an already-authenticated, generation-specific client.

    Authentication is deliberately never implicit here: resolving a token
    directory requires async database work, which belongs to the registry.
    """
    try:
        return _clients[(person_id, generation)]
    except KeyError as exc:
        raise RuntimeError("Garmin client is not authenticated") from exc


def forget(person_id: int, generation: int | None = None) -> None:
    """Evict one generation, or every cached generation for a person."""
    if generation is not None:
        _clients.pop((person_id, generation), None)
        return
    for key in tuple(_clients):
        if key[0] == person_id:
            _clients.pop(key, None)


def forget_stale_generations(person_id: int, generation: int) -> None:
    """Retain only the client matching the durable generation just read."""
    for key in tuple(_clients):
        if key[0] == person_id and key[1] != generation:
            _clients.pop(key, None)


# ---------------------------------------------------------------------------
# Push methods
# ---------------------------------------------------------------------------

def push_weight_to_client(
    client: Garmin,
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
    convention -- callers (vitalforge_weight's WeightIn) already validate in
    that unit, not kJ. NOTE: vitalforge_weight's _push_composition
    deliberately never passes bmi (00-design.md SS3.4 already rejects
    sending it -- Garmin derives its own from weight + profile height, and
    vitalforge_dashboard/sync.py reads that value back); the kwarg exists
    here because it's a real, valid add_body_composition parameter a future
    caller might have a legitimate reason to set explicitly, not because
    anything currently calls push_weight with it.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    weight_kg = weight_grams / 1000.0
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info("Pushing weight to Garmin: %.1f kg (%.0f g) at %s", weight_kg, weight_grams, ts_str)
    client.add_body_composition(
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
    logger.info("Weight pushed to Garmin successfully")


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
