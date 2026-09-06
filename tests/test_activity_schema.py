"""How `strength_sessions` is wired into the schema, and how it is NOT.

The table is created unguarded in `init_db()`'s DDL block with no migration
marker, like the other 19 tables there. That is not tidiness, it is the
correct reading of this repo's own rule: the DDL block runs BEFORE any
`run_migration()` call and `CREATE TABLE IF NOT EXISTS` is already right on a
fresh database and an upgrade alike, so a `003` apply-function would be a
no-op on every path.

Adding the marker anyway would be actively harmful, which is what
test_no_migration_marker_was_added exists to prevent:
`assert_schema_understood()` boot-loops any image that finds a marker outside
its own `_KNOWN_MIGRATIONS`, so a rollback to a pre-Cadence image would refuse
to start. A bare extra table it does not recognise is ignored harmlessly.
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


async def test_no_migration_marker_was_added(initialized_db):
    """Pins _KNOWN_MIGRATIONS to exactly the two rebuilds that legitimately
    need a marker. A `003` added "for tidiness" would make a rollback to any
    earlier image boot-loop."""
    assert migrations._KNOWN_MIGRATIONS == ("001-person-id-rebuild", "002-activities-person-id")
    assert "strength_sessions" not in migrations._REBUILD_TABLES

    db = await get_db()
    try:
        rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    finally:
        await db.close()
    assert not [r["name"] for r in rows if "strength" in r["name"]]


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
