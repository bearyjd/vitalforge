"""Regression guard for the 2026-08-22 garminconnect 0.2.38 -> 0.3.11 upgrade.

That bump (commit 49fa674, for add_body_composition) silently broke
`shared.garmin_client.authenticate()`: the new garminconnect dropped its
`garth` dependency entirely (no `.garth` attribute, different token file
format), but `authenticate()` still called `.garth.dump(...)`. Every other
test in this suite monkeypatches `authenticate`/`garmin_client._client` to a
`FakeGarminClient` (see conftest.py) that never touches the real
`garminconnect` package, so nothing here would have caught an API mismatch
like that. These tests import the REAL `garminconnect.Garmin` class (no
network I/O -- `Garmin()` construction and `inspect.signature` are both
local) specifically to catch a future version bump that changes this shape
again.
"""

import inspect

from garminconnect import Garmin


def test_garmin_constructor_accepts_email_and_password():
    """authenticate() calls Garmin(email=..., password=...)."""
    sig = inspect.signature(Garmin.__init__)
    assert "email" in sig.parameters
    assert "password" in sig.parameters


def test_garmin_login_accepts_tokenstore_kwarg():
    """authenticate() calls client.login(tokenstore=path)."""
    sig = inspect.signature(Garmin.login)
    assert "tokenstore" in sig.parameters


def test_garmin_client_has_no_garth_attribute():
    """authenticate() must not depend on `.garth` -- that's the garth-era API
    this version of garminconnect no longer has. If this starts failing
    because garminconnect brought `.garth` back, that's fine; it means
    verify the rest of authenticate() still matches before relying on it
    again."""
    client = Garmin(email="test@example.com", password="x")
    assert not hasattr(client, "garth")


def test_create_manual_activity_signature():
    """push_activity() calls create_manual_activity with these six names as
    keywords. Every other test in this suite fakes that call, so a version
    bump that renamed or reordered a parameter would be invisible until a
    real push failed in production."""
    sig = inspect.signature(Garmin.create_manual_activity)
    for name in ("start_datetime", "time_zone", "type_key", "distance_km", "duration_min", "activity_name"):
        assert name in sig.parameters, f"create_manual_activity lost {name!r}"


def test_set_activity_exercise_sets_signature():
    """push_activity_sets() calls this positionally as (activity_id, payload)."""
    sig = inspect.signature(Garmin.set_activity_exercise_sets)
    assert list(sig.parameters) == ["self", "activity_id", "payload"]


def test_get_activity_exercise_sets_exists():
    """JD's probe 1 -- the one live call that settles the exerciseSets
    payload's field names -- goes through this method. If a version bump
    removes it, the enhancement path behind VITALFORGE_GARMIN_EXERCISE_SETS
    has no way left to be verified before it ships."""
    sig = inspect.signature(Garmin.get_activity_exercise_sets)
    assert "activity_id" in sig.parameters


def test_exercise_categories_are_available_for_model_validation():
    """ActivityExerciseIn validates garmin_category against this list at the
    model layer, so an unknown category is a local 422 rather than a Garmin
    400 discovered after the activity has already been created."""
    from garminconnect.exercises import CATEGORIES

    assert isinstance(CATEGORIES, list)
    # A representative spread of the categories Cadence actually emits.
    for category in ("BENCH_PRESS", "SQUAT", "DEADLIFT", "PLANK", "CARRY", "PULL_UP"):
        assert category in CATEGORIES
    # UNKNOWN is deliberately NOT a category in this catalog -- the correct
    # "I don't know the variant" encoding is a known category with name=None.
    assert "UNKNOWN" not in CATEGORIES
