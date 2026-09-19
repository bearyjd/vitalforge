"""The bounded garmin_error vocabulary must have exactly one definition."""

from shared import garmin_registry_errors, garmin_routes, migrations
from vitalforge_weight import activity_garmin, activity_routes


def test_activity_module_and_migration_004_share_one_code_set():
    assert activity_garmin._SAFE_GARMIN_ERROR_CODES == frozenset(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert migrations._SAFE_STRENGTH_GARMIN_ERRORS is garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES


def test_registry_codes_are_a_subset_of_the_strength_codes():
    assert garmin_registry_errors.REGISTRY_ERROR_CODES <= set(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert garmin_routes._SAFE_AUTH_ERRORS is garmin_registry_errors.REGISTRY_ERROR_CODES


def test_retired_target_code_has_one_definition():
    assert activity_routes._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR
    assert migrations._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR
