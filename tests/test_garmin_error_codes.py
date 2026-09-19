"""The bounded garmin_error vocabulary must have exactly one definition."""

import aiosqlite
import pytest

from shared import garmin_registry_errors, garmin_routes, migrations
from shared.database import get_db
from tests.conftest import seed_person
from vitalforge_weight import activity_garmin, activity_routes


def test_activity_module_and_migration_004_share_one_code_set():
    assert activity_garmin._SAFE_GARMIN_ERROR_CODES == frozenset(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert migrations._SAFE_STRENGTH_GARMIN_ERRORS is garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES


def test_registry_codes_are_a_subset_of_the_strength_codes():
    assert garmin_registry_errors.REGISTRY_ERROR_CODES <= set(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert garmin_routes._SAFE_AUTH_ERRORS is garmin_registry_errors.REGISTRY_ERROR_CODES


def test_activity_push_codes_are_a_subset_of_the_strength_codes():
    """The seven activity-push failure codes activity_garmin.py stamps must
    all be members of the append-only strength_sessions vocabulary -- a
    typo'd or renamed constant here would raise the DDL CHECK at write time
    instead of failing this assertion."""
    assert {
        activity_garmin._PREPARATION_FAILED,
        activity_garmin._PUSH_FAILED,
        activity_garmin._PUSH_OUTCOME_UNKNOWN,
        activity_garmin._SETS_UPLOAD_FAILED,
        activity_garmin._RECONCILIATION_FAILED,
        activity_garmin._RECONCILIATION_PENDING,
        activity_garmin._OUTCOME_RECORD_FAILED,
    } <= set(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)


def test_retired_target_code_has_one_definition():
    assert activity_routes._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR
    assert migrations._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR


async def _link(person_id: int, generation: int = 1, state: str = "linked") -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at)
            VALUES (?, ?, ?, ?, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')
            """,
            (person_id, state, f"person-{person_id}@example.test", generation),
        )
        await db.commit()
    finally:
        await db.close()


async def test_last_auth_error_ddl_check_is_bound_to_registry_error_codes(initialized_db):
    """shared/database.py's garmin_links.last_auth_error CHECK hardcodes its
    own fourth copy of the code vocabulary (shared/garmin_registry_errors.py
    is the other three). This pins the DDL against drift: appending a code
    to REGISTRY_ERROR_CODES without updating the CHECK would otherwise pass
    every Python-level test while raising IntegrityError at write time in
    production (shared/garmin_registry_runtime.py:_record_auth_failure)."""
    person_id = await seed_person("ddl-check-error-codes")
    await _link(person_id)

    db = await get_db()
    try:
        for code in garmin_registry_errors.REGISTRY_ERROR_CODES:
            await db.execute(
                "UPDATE garmin_links SET last_auth_error = ?, last_auth_error_at = ? WHERE person_id = ?",
                (code, "2026-09-15T00:00:00Z", person_id),
            )
            await db.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "UPDATE garmin_links SET last_auth_error = ?, last_auth_error_at = ? WHERE person_id = ?",
                ("not_a_registry_error_code", "2026-09-15T00:00:00Z", person_id),
            )
        await db.rollback()
    finally:
        await db.close()
