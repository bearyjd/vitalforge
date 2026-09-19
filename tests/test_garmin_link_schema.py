"""DDL contract for the additive per-person Garmin-linking schema.

All four tables are created in init_db()'s pre-migration DDL block.  They
therefore need no schema_migrations marker: older images ignore unknown tables
on rollback, whereas an unknown marker deliberately prevents startup.
"""

import pytest

from shared import database, migrations
from shared.database import get_db, get_primary_person_id
from tests.conftest import seed_user


async def _table_sql(name: str) -> str | None:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        ).fetchone()
    finally:
        await db.close()
    return row["sql"] if row is not None else None


async def _columns(name: str) -> dict[str, object]:
    db = await get_db()
    try:
        return {
            row["name"]: row
            for row in await (await db.execute(f"PRAGMA table_info({name})")).fetchall()
        }
    finally:
        await db.close()


@pytest.mark.parametrize(
    "table",
    ("garmin_links", "garmin_call_budget", "garmin_link_attempts", "garmin_link_generations"),
)
async def test_additive_garmin_tables_are_created_on_a_fresh_database(initialized_db, table):
    assert await _table_sql(table) is not None


@pytest.mark.parametrize(
    "table",
    ("garmin_links", "garmin_call_budget", "garmin_link_attempts", "garmin_link_generations"),
)
async def test_additive_garmin_tables_are_created_on_an_existing_database(
    production_schema_db, monkeypatch, table
):
    """An old database converges without adding a migration marker."""
    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    assert await _table_sql(table) is not None


async def test_garmin_link_schema_has_no_secret_storage(initialized_db):
    columns = await _columns("garmin_links")
    assert set(columns) == {
        "person_id",
        "state",
        "garmin_email",
        "generation",
        "linked_at",
        "linked_by",
        "updated_at",
        "last_auth_ok",
        "last_auth_error",
        "last_auth_error_at",
    }
    assert columns["state"]["notnull"] == 1
    assert columns["generation"]["notnull"] == 1
    assert columns["updated_at"]["notnull"] == 1


async def test_garmin_link_constraints_enforce_lifecycle_and_privacy(initialized_db):
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO garmin_links "
            "(person_id, state, garmin_email, generation, linked_at, updated_at) "
            "VALUES (?, 'linked', 'owner@example.test', 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')",
            (person_id,),
        )
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            await db.execute(
                "INSERT INTO garmin_links "
                "(person_id, state, garmin_email, generation, linked_at, updated_at) "
                "VALUES (?, 'linked', 'owner@example.test', 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')",
                (person_id + 1,),
            )
        await db.rollback()

        for state, email, linked_at in (
            ("linked", None, "2026-09-14T00:00:00Z"),
            ("legacy_bound", "owner@example.test", None),
            ("legacy_disabled", "owner@example.test", None),
        ):
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await db.execute(
                    "INSERT INTO garmin_links "
                    "(person_id, state, garmin_email, generation, linked_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, '2026-09-14T00:00:00Z')",
                    (person_id, state, email, linked_at),
                )
            await db.rollback()

        # Canonicalization is deliberately application-owned: SQLite's
        # lower()/NOCASE are ASCII-only and would mishandle Unicode casefold
        # equivalence. The DDL only rejects empty or space-padded values.
        for noncanonical_email in ("owner@example.test ", "", "   "):
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await db.execute(
                    "INSERT INTO garmin_links "
                    "(person_id, state, garmin_email, generation, linked_at, updated_at) "
                    "VALUES (?, 'linked', ?, 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')",
                    (person_id, noncanonical_email),
                )
            await db.rollback()

        await db.execute(
            "INSERT INTO garmin_links "
            "(person_id, state, garmin_email, generation, linked_at, updated_at) "
            "VALUES (?, 'linked', 'Owner@example.test', 1, '2026-09-14T00:00:00Z', "
            "'2026-09-14T00:00:00Z')",
            (person_id,),
        )
        await db.rollback()

        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute(
                "INSERT INTO garmin_links "
                "(person_id, state, garmin_email, generation, linked_at, updated_at) "
                "VALUES (?, 'linked', 'owner@example.test', 0, '2026-09-14T00:00:00Z', "
                "'2026-09-14T00:00:00Z')",
                (person_id,),
            )
        await db.rollback()

        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute(
                "INSERT INTO garmin_links "
                "(person_id, state, garmin_email, generation, linked_at, updated_at, last_auth_error) "
                "VALUES (?, 'linked', 'owner@example.test', 1, '2026-09-14T00:00:00Z', "
                "'2026-09-14T00:00:00Z', 'raw third-party exception')",
                (person_id,),
            )
        await db.rollback()

        for code, error_at in (("auth_failed", None), (None, "2026-09-14T00:00:00Z")):
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await db.execute(
                    "INSERT INTO garmin_links "
                    "(person_id, state, garmin_email, generation, linked_at, updated_at, "
                    "last_auth_error, last_auth_error_at) "
                    "VALUES (?, 'linked', 'owner@example.test', 1, '2026-09-14T00:00:00Z', "
                    "'2026-09-14T00:00:00Z', ?, ?)",
                    (person_id, code, error_at),
                )
            await db.rollback()
    finally:
        await db.close()


async def test_garmin_link_email_uses_binary_uniqueness_not_sqlite_canonicalization(initialized_db):
    """Unicode email canonicalization is a lifecycle concern, not SQLite collation."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        # These values become the same value under Python str.casefold(), but
        # are distinct BINARY SQLite values. Application code must canonicalize
        # before insertion so this direct-SQL probe remains a schema property,
        # not an application behavior test.
        first = "Straße@example.test"
        second = "strasse@example.test"
        for offset, email in enumerate((first, second)):
            await db.execute(
                "INSERT INTO garmin_links "
                "(person_id, state, garmin_email, generation, linked_at, updated_at) "
                "VALUES (?, 'linked', ?, 1, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z')",
                (person_id + offset, email),
            )
        rows = await (
            await db.execute("SELECT garmin_email FROM garmin_links ORDER BY person_id")
        ).fetchall()
    finally:
        await db.close()

    assert [row["garmin_email"] for row in rows] == [first, second]
    assert first.casefold() == second.casefold()


async def test_garmin_link_schema_does_not_use_sqlite_ascii_case_canonicalization(initialized_db):
    sql = await _table_sql("garmin_links")
    assert sql is not None
    assert "COLLATE NOCASE" not in sql.upper()
    assert "COLLATE BINARY" in sql.upper()
    assert "lower(" not in sql.lower()


async def test_call_budget_is_seeded_as_a_singleton(initialized_db):
    db = await get_db()
    try:
        rows = await (await db.execute("SELECT singleton, next_allowed_at FROM garmin_call_budget")).fetchall()
        assert [(row["singleton"], row["next_allowed_at"]) for row in rows] == [(1, 0.0)]
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute("INSERT INTO garmin_call_budget VALUES (2, 0)")
    finally:
        await db.close()


async def test_link_attempt_slots_are_a_dense_ordered_rolling_window(initialized_db):
    """The durable row retains all three timestamps needed for a rolling limit."""
    valid_user_id = await seed_user("link-attempt-window-valid")
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO garmin_link_attempts
                (user_id, window_started_at, attempt_count, attempted_at_1, attempted_at_2, attempted_at_3)
            VALUES (?, 10, 3, 10, 20, 30)
            """,
            (valid_user_id,),
        )
        await db.commit()

        for index, values in enumerate((
            # The compatibility field is the first (oldest) active attempt.
            "11, 1, 10, NULL, NULL",
            # Slots cannot be sparse or exceed the count.
            "10, 1, 10, 20, NULL",
            "10, 2, 10, NULL, NULL",
            "10, 2, 10, 20, 30",
            # A rolling window is stored oldest-to-newest.
            "20, 2, 20, 10, NULL",
            "10, 3, 10, 30, 20",
            # The limiter never needs to retain a fourth active attempt.
            "10, 4, 10, 20, 30",
        )):
            invalid_user_id = await seed_user(f"link-attempt-window-invalid-{index}")
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await db.execute(
                    """
                    INSERT INTO garmin_link_attempts
                        (user_id, window_started_at, attempt_count,
                         attempted_at_1, attempted_at_2, attempted_at_3)
                    VALUES (?, """
                    + values
                    + ")",
                    (invalid_user_id,),
                )
            await db.rollback()
    finally:
        await db.close()


@pytest.mark.parametrize(
    "table, columns, values",
    (
        (
            "garmin_link_attempts",
            "user_id, window_started_at, attempt_count, attempted_at_1",
            "1, 0, 0, 0",
        ),
        ("garmin_link_generations", "person_id, generation", "1, 0"),
    ),
)
async def test_nonpositive_attempt_counts_and_generations_are_rejected(initialized_db, table, columns, values):
    db = await get_db()
    try:
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})")
    finally:
        await db.close()


async def test_garmin_schema_indexes_and_known_migration_markers(initialized_db):
    """The link tables need only implicit indexes; activity cleanup has markers."""
    assert migrations._KNOWN_MIGRATIONS == (
        "001-person-id-rebuild",
        "002-activities-person-id",
        "003-strength-sessions-remove-garmin-target",
        "004-strength-sessions-redact-garmin-errors",
    )
    db = await get_db()
    try:
        for table in ("garmin_links", "garmin_call_budget", "garmin_link_attempts", "garmin_link_generations"):
            rows = await (
                await db.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,))
            ).fetchall()
            assert all(row["sql"] is None for row in rows)
        rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    finally:
        await db.close()
    assert [row["name"] for row in rows if "garmin" in row["name"]] == [
        "003-strength-sessions-remove-garmin-target",
        "004-strength-sessions-redact-garmin-errors",
    ]


@pytest.mark.asyncio
async def test_garmin_link_schema_parity_fresh_vs_existing_database(tmp_path, monkeypatch, production_schema_db):
    """The additive DDL has identical shapes on a new and an old image DB."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    fresh_db = await database.get_db()

    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    existing_db = await database.get_db()

    try:
        for table in ("garmin_links", "garmin_call_budget", "garmin_link_attempts", "garmin_link_generations"):
            fresh_columns = await (await fresh_db.execute(f"PRAGMA table_info({table})")).fetchall()
            existing_columns = await (await existing_db.execute(f"PRAGMA table_info({table})")).fetchall()
            fresh_shape = sorted(
                (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in fresh_columns
            )
            existing_shape = sorted(
                (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in existing_columns
            )
            assert fresh_shape == existing_shape, f"{table} columns diverged"
    finally:
        await fresh_db.close()
        await existing_db.close()


async def test_core_schema_consumers_can_read_after_garmin_migrations(initialized_db):
    """Provider-cleanup migrations leave unrelated table reads intact.

    This is intentionally a compatibility probe rather than importing old
    application code into the test suite. The strength-session cleanup is
    data-only: it must not disturb core multitenancy tables.
    """
    db = await get_db()
    try:
        for table in ("users", "persons", "person_grants", "weight_log", "sync_status"):
            await (await db.execute(f"SELECT * FROM [{table}] LIMIT 0")).fetchall()
        markers = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    finally:
        await db.close()

    assert "004-strength-sessions-redact-garmin-errors" in {row["name"] for row in markers}
