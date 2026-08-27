"""Tests for shared/migrations.py -- the once-only migration runner and
schema-version guard. See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md
section (c) for the full design rationale.
"""

import asyncio

import pytest

import shared.database as database
import shared.migrations as migrations


async def _fresh_db_with_migrations_table(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_db_accepts_isolation_level_none(tmp_path, monkeypatch):
    """get_db() must accept isolation_level as a connect-time parameter, not
    require the caller to set db.isolation_level after connecting -- see
    Task 1's _run_rebuild_then_rollback comment for why the latter raises a
    cross-thread ProgrammingError under aiosqlite."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db(isolation_level=None)
    try:
        assert db.isolation_level is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_db_default_isolation_level_unchanged(tmp_path, monkeypatch):
    """Every existing caller across both services calls get_db() with no
    arguments and must see identical behavior after this change."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        assert db.isolation_level == ""
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_migration_applies_once(tmp_path, monkeypatch):
    await _fresh_db_with_migrations_table(tmp_path, monkeypatch)
    applied = []

    async def apply(db):
        applied.append(1)
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    await migrations.run_migration("test-migration", apply)

    assert applied == [1]
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = 'test-migration'")
        assert await cur.fetchone() is not None
        cur = await db.execute("SELECT name FROM sqlite_master WHERE name = 'probe'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_migration_is_idempotent(tmp_path, monkeypatch):
    await _fresh_db_with_migrations_table(tmp_path, monkeypatch)
    call_count = 0

    async def apply(db):
        nonlocal call_count
        call_count += 1
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    await migrations.run_migration("test-migration", apply)
    await migrations.run_migration("test-migration", apply)  # second call, same name

    assert call_count == 1, "apply() ran twice for the same migration name"


@pytest.mark.asyncio
async def test_run_migration_rolls_back_on_exception(tmp_path, monkeypatch):
    await _fresh_db_with_migrations_table(tmp_path, monkeypatch)

    async def apply(db):
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        raise ValueError("simulated failure mid-migration")

    with pytest.raises(ValueError, match="simulated failure"):
        await migrations.run_migration("failing-migration", apply)

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = 'failing-migration'")
        assert await cur.fetchone() is None, "marker committed despite apply() raising"
        cur = await db.execute("SELECT name FROM sqlite_master WHERE name = 'probe'")
        assert await cur.fetchone() is None, "probe table survived a rolled-back migration"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_migration_concurrent_calls_apply_exactly_once(tmp_path, monkeypatch):
    """Mirrors the existing concurrent-bootstrap test pattern for
    bootstrap_first_admin (shared/auth.py)."""
    await _fresh_db_with_migrations_table(tmp_path, monkeypatch)
    call_count = 0

    async def apply(db):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # widen the race window
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    await asyncio.gather(
        migrations.run_migration("concurrent-migration", apply),
        migrations.run_migration("concurrent-migration", apply),
    )

    assert call_count == 1, f"apply() ran {call_count} times, expected exactly 1"


@pytest.mark.asyncio
async def test_get_db_sets_a_30_second_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        assert row[0] == 30000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_columns_skips_alter_when_column_already_present(tmp_path, monkeypatch):
    """Latency-only behavior: when the shape pre-check sees the column
    already exists, _add_columns must not even attempt the ALTER TABLE
    (which would otherwise hit-and-swallow duplicate_column_name every
    time, wasting a lock wait under contention)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, existing_col TEXT)")
        await db.commit()

        executed = []
        original_execute = db.execute

        async def spy_execute(sql, *args, **kwargs):
            executed.append(sql)
            return await original_execute(sql, *args, **kwargs)

        db.execute = spy_execute
        await database._add_columns(db, "probe", ["existing_col TEXT"])

        assert not any("ALTER TABLE" in sql for sql in executed), (
            "shape pre-check did not prevent a redundant ALTER TABLE attempt"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_columns_still_adds_missing_column(tmp_path, monkeypatch):
    """Correctness is unchanged: a genuinely-missing column is still added."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        await db.commit()

        await database._add_columns(db, "probe", ["new_col TEXT"])

        cur = await db.execute("PRAGMA table_info(probe)")
        columns = {row[1] for row in await cur.fetchall()}
        assert "new_col" in columns
    finally:
        await db.close()
