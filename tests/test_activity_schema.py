"""How `strength_sessions` is wired into the schema, and how it is NOT.

The current table is created unguarded in `init_db()`'s DDL block. Migration
003 is the narrow exception: it rebuilds already-deployed tables to remove a
retired column. Fresh databases create the final shape and the migration is a
no-op for them.
"""

import pytest

from shared import database, migrations
from shared.database import get_db


async def table_sql(name: str) -> str | None:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        ).fetchone()
    finally:
        await db.close()
    return row["sql"] if row is not None else None


async def test_table_created_on_a_fresh_database(initialized_db):
    assert await table_sql("strength_sessions") is not None


@pytest.mark.asyncio
async def test_table_created_on_an_existing_database(production_schema_db, monkeypatch):
    """The upgrade path: a database dumped from production, which predates
    this table entirely. `CREATE TABLE IF NOT EXISTS` in the DDL block is the
    whole mechanism -- no marker, no rebuild."""
    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    assert await table_sql("strength_sessions") is not None


async def test_strength_sessions_removal_migration_is_registered(initialized_db):
    assert migrations._KNOWN_MIGRATIONS == (
        "001-person-id-rebuild",
        "002-activities-person-id",
        "003-strength-sessions-remove-garmin-target",
        "004-strength-sessions-redact-garmin-errors",
    )
    assert "strength_sessions" not in migrations._REBUILD_TABLES

    db = await get_db()
    try:
        rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    finally:
        await db.close()
    assert [r["name"] for r in rows if "strength" in r["name"]] == [
        "003-strength-sessions-remove-garmin-target",
        "004-strength-sessions-redact-garmin-errors",
    ]


async def test_redaction_migration_replaces_historic_provider_error_text(initialized_db):
    """The once-only migration cleans existing rows; route filtering is a backstop."""
    from datetime import datetime, timezone

    person_id = await database.get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    raw_error = "Bearer migration-sentinel-abcdefghijklmnopqrstuvwxyz"
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
            "exercises_json, garmin_status, garmin_error, created_at, updated_at) "
            "VALUES (?, 'historic-error', ?, 60, '[]', 'failed', ?, ?, ?)",
            (person_id, now, raw_error, now, now),
        )
        await db.execute(
            "DELETE FROM schema_migrations WHERE name = '004-strength-sessions-redact-garmin-errors'"
        )
        await db.commit()
    finally:
        await db.close()

    await database.init_db()

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_error FROM strength_sessions WHERE session_id = 'historic-error'")
        ).fetchone()
    finally:
        await db.close()
    assert row["garmin_error"] == "unknown"


async def test_unique_constraint_is_not_partial(initialized_db):
    """Deliberately unlike idx_weight_log_person_client_id, which is partial
    ONLY because client_id is nullable. session_id is NOT NULL here, so there
    are no NULLs to exclude -- and a partial predicate copied across by
    analogy would silently stop enforcing anything for the rows it excluded."""
    sql = await table_sql("strength_sessions")
    assert "UNIQUE (person_id, session_id)" in sql
    assert "WHERE" not in sql.split("UNIQUE (person_id, session_id)")[1]


async def test_person_id_is_not_null(initialized_db):
    """Unlike the pre-existing tables, which could not be: this table is new,
    so every row carries a person from the start and no orphan backfill is
    ever needed for it."""
    db = await get_db()
    try:
        columns = {
            row["name"]: row
            for row in await (await db.execute("PRAGMA table_info(strength_sessions)")).fetchall()
        }
    finally:
        await db.close()
    assert columns["person_id"]["notnull"] == 1
    assert columns["session_id"]["notnull"] == 1
    assert columns["garmin_status"]["dflt_value"] == "'skipped'"
    assert columns["garmin_sets_status"]["dflt_value"] == "'not_attempted'"


@pytest.mark.parametrize("column,value", [("garmin_status", "bogus"), ("garmin_sets_status", "bogus")])
async def test_check_constraints_reject_unknown_status(initialized_db, column, value):
    from datetime import datetime, timezone

    from shared.database import get_primary_person_id

    person_id = await get_primary_person_id()
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute(
                f"INSERT INTO strength_sessions (person_id, session_id, start_time_utc, duration_seconds, "
                f"exercises_json, {column}, created_at, updated_at) VALUES (?, 'x', ?, 60, '[]', ?, ?, ?)",
                (person_id, now, value, now, now),
            )
    finally:
        await db.close()


async def test_person_start_index_exists(initialized_db):
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("idx_strength_sessions_person_start",),
            )
        ).fetchone()
    finally:
        await db.close()
    assert row is not None


async def test_garmin_claimed_at_column_exists(initialized_db):
    """The mutual-exclusion column that stops two concurrent requests both
    deciding to push. Nullable and with no default: NULL means "no push in
    flight", which is the correct state for the overwhelming majority of rows
    and for every row that never pushes at all."""
    db = await get_db()
    try:
        columns = {
            row["name"]: row
            for row in await (await db.execute("PRAGMA table_info(strength_sessions)")).fetchall()
        }
    finally:
        await db.close()
    assert "garmin_claimed_at" in columns
    assert columns["garmin_claimed_at"]["notnull"] == 0
    assert columns["garmin_claimed_at"]["dflt_value"] is None


@pytest.mark.asyncio
async def test_garmin_claimed_at_present_on_an_upgraded_database(production_schema_db, monkeypatch):
    """The column is part of the CREATE TABLE rather than an _add_columns
    shim, which is only safe because strength_sessions is new in this same
    unmerged branch -- no deployed database has the table at all, so there is
    no database that could have it WITHOUT this column. Pinned here so that
    stops being an assumption."""
    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    db = await get_db()
    try:
        columns = {
            row["name"] for row in await (await db.execute("PRAGMA table_info(strength_sessions)")).fetchall()
        }
    finally:
        await db.close()
    assert "garmin_claimed_at" in columns
