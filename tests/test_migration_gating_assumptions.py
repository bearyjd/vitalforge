"""Gating tests for the migration-runner design (spec §c.8, run before any
other code in this plan). These test SQLite's and aiosqlite's own DDL/
transaction semantics, not this repo's code — if either fails, the
single-transaction rebuild design in the multi-tenancy spec is wrong and
must be revisited before continuing, per spec Appendix A footnote 1.
"""

import aiosqlite
import pytest


async def _create_scratch_schema(db):
    """A synthetic two-table schema, deliberately unrelated to any real
    VitalForge table, so this test needs zero knowledge of production shape.
    """
    await db.execute("CREATE TABLE scratch_a (id INTEGER PRIMARY KEY, val TEXT)")
    await db.execute("INSERT INTO scratch_a (id, val) VALUES (1, 'original')")
    await db.execute("CREATE TABLE scratch_b (id INTEGER PRIMARY KEY, val TEXT)")
    await db.execute("INSERT INTO scratch_b (id, val) VALUES (1, 'original')")
    await db.commit()


async def _sqlite_master_snapshot(db, table_names):
    rows = {}
    for name in table_names:
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,))
        row = await cur.fetchone()
        rows[name] = row[0] if row else None
    return rows


async def _run_rebuild_then_rollback(db):
    # isolation_level is NOT set here as a post-connect attribute assignment
    # (db.isolation_level = ...): aiosqlite proxies the real sqlite3
    # connection to a background worker thread, and a bare attribute set on
    # the returned object touches that underlying object from the calling
    # (wrong) thread, raising `sqlite3.ProgrammingError: SQLite objects
    # created in a thread can only be used in that same thread` -- verified
    # directly, this is NOT specific to pytest-asyncio, it reproduces in a
    # bare asyncio script too. isolation_level must be passed to
    # aiosqlite.connect(..., isolation_level=...) instead, which is why the
    # caller passes an already-correctly-opened `db` here.
    await db.execute("PRAGMA busy_timeout = 30000")
    await db.execute("BEGIN IMMEDIATE")
    # The full create/copy/drop/rename sequence, on scratch_a only (scratch_b
    # stays untouched as a control — it proves the rollback didn't just
    # "happen to" restore the one table under test).
    await db.execute("CREATE TABLE scratch_a__new (id INTEGER PRIMARY KEY, val TEXT, extra TEXT)")
    await db.execute("INSERT INTO scratch_a__new (id, val, extra) SELECT id, val, 'added' FROM scratch_a")
    await db.execute("DROP TABLE scratch_a")
    await db.execute("ALTER TABLE scratch_a__new RENAME TO scratch_a")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, completed_at TEXT NOT NULL)"
    )
    await db.execute(
        "INSERT INTO schema_migrations (name, completed_at) VALUES ('gating-test', 'x')"
    )
    await db.rollback()


@pytest.mark.asyncio
async def test_ddl_rebuild_rolls_back_cleanly_with_isolation_level_none(tmp_path):
    """The mode run_migration() actually uses. If this fails, the entire
    single-transaction rebuild design (spec §c.3, §c.6) is wrong."""
    db_path = tmp_path / "gating.db"
    setup = await aiosqlite.connect(str(db_path))
    try:
        await _create_scratch_schema(setup)
        before = await _sqlite_master_snapshot(setup, ["scratch_a", "scratch_b"])
    finally:
        await setup.close()

    db = await aiosqlite.connect(str(db_path), isolation_level=None)
    try:
        await _run_rebuild_then_rollback(db)
    finally:
        await db.close()

    verify = await aiosqlite.connect(str(db_path))
    try:
        after = await _sqlite_master_snapshot(verify, ["scratch_a", "scratch_b"])
        assert after == before, "sqlite_master.sql changed across a rolled-back rebuild"

        cur = await verify.execute("SELECT id, val FROM scratch_a")
        rows = await cur.fetchall()
        assert rows == [(1, "original")], "scratch_a row content changed"

        cur = await verify.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scratch_a__new'"
        )
        assert await cur.fetchone() is None, "a __new table survived the rollback"

        cur = await verify.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        # schema_migrations' CREATE TABLE was inside the rolled-back transaction too.
        assert await cur.fetchone() is None, "schema_migrations table survived the rollback"
    finally:
        await verify.close()


@pytest.mark.asyncio
async def test_ddl_rebuild_rolls_back_cleanly_with_legacy_isolation_level(tmp_path):
    """Same test, legacy isolation_level (''). Spec §c.8: 'record the result
    either way' -- if this also passes, run_migration()'s explicit
    isolation_level = None is belt-and-braces rather than load-bearing,
    which is worth knowing but does not change the implementation."""
    db_path = tmp_path / "gating_legacy.db"
    setup = await aiosqlite.connect(str(db_path))
    try:
        await _create_scratch_schema(setup)
        before = await _sqlite_master_snapshot(setup, ["scratch_a", "scratch_b"])
    finally:
        await setup.close()

    db = await aiosqlite.connect(str(db_path), isolation_level="")
    try:
        await _run_rebuild_then_rollback(db)
    finally:
        await db.close()

    verify = await aiosqlite.connect(str(db_path))
    try:
        after = await _sqlite_master_snapshot(verify, ["scratch_a", "scratch_b"])
        assert after == before, (
            "legacy isolation_level does NOT roll back DDL cleanly -- "
            "isolation_level=None in run_migration() is load-bearing, not optional"
        )
    finally:
        await verify.close()


@pytest.mark.asyncio
async def test_second_connection_can_write_while_first_connection_open_no_transaction(tmp_path):
    """Connection 1 stays open (unclosed, no active transaction) while
    connection 2 takes a write lock. Must NOT block or raise -- this is
    what makes it safe for run_migration() to open its own connection
    after init_db()'s connection has merely gone out of scope but not
    necessarily been garbage-collected yet. (init_db() explicitly closes
    its connection before calling anything from shared/migrations.py --
    see spec §c.4 -- this test proves why that close() matters.)
    """
    db_path = tmp_path / "visibility.db"
    conn1 = await aiosqlite.connect(str(db_path))
    await conn1.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY)")
    await conn1.commit()

    conn2 = await aiosqlite.connect(str(db_path), isolation_level=None)
    try:
        # isolation_level is passed to connect() above, not set as a
        # post-connect attribute -- see Task 1's _run_rebuild_then_rollback
        # comment for why the latter raises a cross-thread ProgrammingError.
        await conn2.execute("PRAGMA busy_timeout = 30000")
        await conn2.execute("BEGIN IMMEDIATE")
        await conn2.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
        await conn2.commit()
    finally:
        await conn2.close()
        await conn1.close()


@pytest.mark.asyncio
async def test_second_connection_write_with_uncommitted_seed_insert_on_first(tmp_path):
    """Same as above, but connection 1 has an uncommitted write pending.
    Documents (does not assert a specific outcome beyond 'no hang') what
    actually happens -- this is the scenario that would exist if init_db()
    ever grew a seed INSERT before closing its connection.
    """
    db_path = tmp_path / "visibility_seed.db"
    conn1 = await aiosqlite.connect(str(db_path))
    await conn1.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY)")
    await conn1.execute("INSERT INTO t1 (id) VALUES (1)")
    # Deliberately NOT committed yet -- conn1 holds an open write transaction.

    conn2 = await aiosqlite.connect(str(db_path), isolation_level=None)
    await conn2.execute("PRAGMA busy_timeout = 2000")  # short timeout, this should time out fast
    try:
        with pytest.raises(aiosqlite.OperationalError, match="database is locked"):
            await conn2.execute("BEGIN IMMEDIATE")
            await conn2.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
    finally:
        await conn2.close()
        await conn1.commit()
        await conn1.close()
