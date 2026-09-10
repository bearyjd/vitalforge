"""Request models shared by the weight and activity route modules.

The unit constants and CAPTURED_AT_FUTURE_TOLERANCE_SECONDS live here rather than
with either domain because BOTH WeightIn and ActivityIn need them; putting them in
weight_routes would make models and weight_routes import each other.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from garminconnect.exercises import CATEGORIES
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.

logger = logging.getLogger(__name__)


LBS_PER_KG = 2.20462


GRAMS_PER_KG = 1000


# How far ahead of receipt time a client-supplied `captured_at` may be before
# WeightIn._validate_captured_at rejects it -- ordinary clock skew tolerance,
# not a real allowance for a future weigh-in. Defined ahead of WeightIn,
# unlike DEDUP_WINDOW_SECONDS below (which the route, not the model, needs).
CAPTURED_AT_FUTURE_TOLERANCE_SECONDS = 60


class WeightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: float
    unit: str = "lbs"  # kept as a plain str, not a Literal -- see docs/prp/00-design.md SS3.1
    body_fat_pct: float | None = Field(default=None, ge=3.0, le=75.0)
    body_water_pct: float | None = Field(default=None, ge=30.0, le=80.0)
    muscle_pct: float | None = Field(default=None, ge=10.0, le=90.0)
    bone_mass_kg: float | None = Field(default=None, ge=0.5, le=10.0)
    # Bascule's V2Shaper (network/ReadingPayloadShaper.kt) has sent these
    # three since it was written, but this model had nowhere to put them --
    # extra="forbid" meant any reading carrying one would 422 the *whole*
    # request the moment V2 was ever selected, not just drop the extra
    # field. Same failure shape client_id/captured_at had before A6.
    # bmr/amr are kcal/day (Bascule converts from the SIG profile's raw kJ at
    # persistence, docs/prp/02-interface-revision.md in the Bascule repo) --
    # the same unit garminconnect's basal_met/active_met already expect.
    # bmi is stored/echoed but deliberately never pushed to Garmin -- see the
    # ENRICHABLE_FIELDS comment further down this file for why.
    bmi: float | None = Field(default=None, ge=10.0, le=100.0)
    # The bound below is a plausibility gate, not a full unit guard -- like
    # bone_mass_kg's kg-vs-grams gap (test_bone_mass_in_grams_rejected_422),
    # it cannot reliably catch every kJ-instead-of-kcal mixup: a true BMR in
    # the ~1000-1195 kcal range submitted as raw kJ (~4184-5000) still lands
    # inside this bound and is silently accepted. Not fixed with an extra
    # heuristic (e.g. "amr must exceed bmr") since that assumes a relationship
    # between the two the API never asserts -- same precedent as bone_mass_kg.
    bmr: float | None = Field(default=None, ge=500.0, le=5000.0)
    amr: float | None = Field(default=None, ge=500.0, le=10000.0)
    source: Literal["pwa", "bascule", "bridge", "tasker"] | None = None
    # A6 (Bascule docs/prp/00-design.md SS4.4): client-generated idempotency
    # key. Bounded, not free-form -- this is an opaque token, not user text.
    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    # The other half of A6: the client's own capture time, distinct from this
    # request's receipt time. Lets a delayed replay -- receipt time months
    # after the original weigh-in -- still dedup-match against a row VitalForge
    # already stored near its true capture time, instead of only ever matching
    # within DEDUP_WINDOW_SECONDS of *this* request's arrival. Optional: a
    # client that never sends it (pwa/tasker/legacy bascule) keeps today's
    # receipt-time-anchored behavior exactly as-is.
    captured_at: datetime | None = None

    @field_validator(
        "weight", "body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "bmi", "bmr", "amr", mode="before",
    )
    @classmethod
    def _reject_bool(cls, value):
        # bool is a subclass of int in Python, so Pydantic's lax float mode
        # otherwise silently coerces JSON true/false to 1.0/0.0 -- which
        # bone_mass_kg's 0.5-10.0 kg bound doesn't exclude (Phase 4
        # adversarial review finding: `bone_mass_kg: true` reached the DB
        # and the Garmin FIT payload as a measured 1kg bone mass).
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value

    @model_validator(mode="after")
    def _validate_weight_bounds(self):
        unit = self.unit.lower()
        if unit not in ("lbs", "kg"):
            return self  # the route's own check produces the legacy 400
        weight_kg = self.weight if unit == "kg" else self.weight / LBS_PER_KG
        if not (2.0 <= weight_kg <= 500.0):
            raise ValueError("weight must be between 2 and 500 kg after unit conversion")
        return self

    @model_validator(mode="after")
    def _validate_captured_at(self):
        if self.captured_at is None:
            return self
        # Naive datetimes are ambiguous (server-local? UTC? the phone's own
        # zone?) -- exactly the kind of guess this codebase's dedup-precision
        # tests exist to avoid making silently. Require an explicit offset.
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must include a UTC offset")
        # Unlike the past (arbitrarily old, by design -- that's what makes a
        # months-later replay's dedup match work at all), the future is
        # bounded: nothing legitimate reports a capture time ahead of when it
        # was sent, beyond ordinary clock skew.
        skew = self.captured_at - datetime.now(timezone.utc)
        if skew > timedelta(seconds=CAPTURED_AT_FUTURE_TOLERANCE_SECONDS):
            raise ValueError("captured_at must not be in the future")
        return self


# The 47 parent exercise categories garminconnect 0.3.11 ships. Validated
# HERE, at the model layer, rather than discovered as a Garmin 400 "Invalid
# Sub-Category Passed" halfway through a push that has already created the
# activity. Frozen into a set at import time: CATEGORIES is a plain list in
# the library, and this is a per-exercise membership test on every POST.
#
# NOTE for a reader coming from Cadence's D-018, which speaks of "the 26
# confirmed names": that number was how many of the categories Cadence asked
# about exist here, not a whitelist. The library's own list is the authority,
# so `UNKNOWN` is rejected (it genuinely is not a category) while the other
# 21 names Cadence never asked about are accepted.
GARMIN_EXERCISE_CATEGORIES = frozenset(CATEGORIES)


class ActivityExerciseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=100)
    garmin_category: str | None = None
    # Sub-category. None is always accepted under a known parent, per
    # set_activity_exercise_sets' own docstring, so this is deliberately NOT
    # validated against a list -- the 1527-entry catalog is keyed by parent
    # and Garmin is the authority on which child belongs to which.
    garmin_exercise: str | None = Field(default=None, max_length=100)
    sets: int = Field(ge=1, le=100)
    reps: int = Field(ge=1, le=100)
    # Time-measured work: planks, dead hangs, carries, and the whole mobility
    # prelude have no meaningful rep count, but `reps` is required ge=1, so
    # those rows arrive as reps=1 plus the hold in `seconds`.
    # create_manual_activity ignores this; it feeds the exerciseSets
    # `duration` when the flag is on.
    seconds: int | None = Field(default=None, ge=1, le=3600)
    weight_kg: float | None = Field(default=None, ge=0, le=500)
    rest_s: int | None = Field(default=None, ge=0, le=3600)

    @field_validator("sets", "reps", "seconds", "weight_kg", "rest_s", mode="before")
    @classmethod
    def _reject_bool(cls, value):
        # Same reason as WeightIn._reject_bool: bool subclasses int, so
        # Pydantic's lax mode silently coerces JSON true to 1 -- which every
        # bound here (sets ge=1, reps ge=1, seconds ge=1) happily accepts.
        # `true` would land as a real one-rep set.
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value

    @field_validator("garmin_category")
    @classmethod
    def _known_garmin_category(cls, value):
        if value is not None and value not in GARMIN_EXERCISE_CATEGORIES:
            raise ValueError(f"unknown Garmin exercise category {value!r}")
        return value


class ActivityIn(BaseModel):
    """A completed strength session. THE PATH CARRIES THE PERSON -- there is
    deliberately no `profile`/`person` field here, and extra="forbid" turns an
    attempt at one into a 422 rather than a silently ignored key."""

    model_config = ConfigDict(extra="forbid")

    # Client-generated idempotency key, same bounds as WeightIn.client_id.
    session_id: str = Field(min_length=1, max_length=128)
    # Becomes the Garmin activity name's suffix ("Cadence — Lower A").
    session_label: str | None = Field(default=None, max_length=60)
    start: datetime
    duration_min: int = Field(ge=1, le=600)
    exercises: list[ActivityExerciseIn] = Field(min_length=1, max_length=50)
    notes: str | None = Field(default=None, max_length=1000)
    source: Literal["cadence", "pwa", "manual"] | None = None
    # Defaults to False deliberately: the safe default is to store, and a
    # destructive-side-effect default should never be implicit.
    push_to_garmin: bool = False
    # D-015 override. Inert on its own -- consulted only when push_to_garmin
    # is true AND the target person is not the Garmin-credential person.
    garmin_target: Literal["credential_person"] | None = None

    @field_validator("duration_min", mode="before")
    @classmethod
    def _reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value

    @model_validator(mode="after")
    def _validate_start(self):
        # WeightIn._validate_captured_at's rules, on this model's own field.
        # Naive datetimes are ambiguous (server-local? UTC? the phone's own
        # zone?) and this route converts to a Garmin wall clock later, where
        # guessing wrong misfiles the activity by the offset amount.
        if self.start.tzinfo is None:
            raise ValueError("start must include a UTC offset")
        skew = self.start - datetime.now(timezone.utc)
        if skew > timedelta(seconds=CAPTURED_AT_FUTURE_TOLERANCE_SECONDS):
            raise ValueError("start must not be in the future")
        return self
