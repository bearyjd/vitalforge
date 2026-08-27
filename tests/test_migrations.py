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
