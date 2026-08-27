"""Tests for shared/migrations.py -- the once-only migration runner and
schema-version guard. See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md
section (c) for the full design rationale.
"""

import asyncio

import aiosqlite
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


@pytest.mark.asyncio
async def test_snapshot_created_and_verified_when_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    db = await database.get_db()
    try:
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        await db.execute("INSERT INTO probe (id) VALUES (1)")
        await db.commit()
    finally:
        await db.close()

    async def needs_snapshot(db):
        return True

    await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    final = tmp_path / "test.pre-migration.db"
    assert final.exists()
    check = await aiosqlite.connect(str(final))
    try:
        cur = await check.execute("SELECT id FROM probe")
        assert await cur.fetchone() == (1,)
    finally:
        await check.close()


@pytest.mark.asyncio
async def test_snapshot_skipped_when_not_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    db = await database.get_db()
    await db.close()

    async def needs_snapshot(db):
        return False

    await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    assert not (tmp_path / "test.pre-migration.db").exists()


@pytest.mark.asyncio
async def test_snapshot_skipped_when_final_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    tmp_path.mkdir(exist_ok=True)
    final = tmp_path / "test.pre-migration.db"
    final.write_bytes(b"already here")
    call_count = 0

    async def needs_snapshot(db):
        nonlocal call_count
        call_count += 1
        return True

    await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    assert call_count == 0, "needs_snapshot() was called even though the final snapshot already exists"
    assert final.read_bytes() == b"already here", "an existing snapshot was overwritten"


@pytest.mark.asyncio
async def test_snapshot_respects_skip_env_var(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    monkeypatch.setenv("VITALFORGE_SKIP_MIGRATION_SNAPSHOT", "1")
    db = await database.get_db()
    await db.close()

    async def needs_snapshot(db):
        return True

    await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    assert not (tmp_path / "test.pre-migration.db").exists()


@pytest.mark.asyncio
async def test_snapshot_discards_a_corrupt_partial_and_retries(tmp_path, monkeypatch):
    """Simulates a container killed mid-VACUUM INTO: a .partial file exists
    from a prior interrupted attempt. A fresh call must discard it (not
    trust it) and produce a genuinely verified snapshot."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    db = await database.get_db()
    try:
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        await db.commit()
    finally:
        await db.close()

    partial = tmp_path / "test.pre-migration.db.partial"
    partial.write_bytes(b"not a valid sqlite file, simulates a kill mid-VACUUM")

    async def needs_snapshot(db):
        return True

    await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    final = tmp_path / "test.pre-migration.db"
    assert final.exists()
    assert not partial.exists()
    check = await aiosqlite.connect(str(final))
    try:
        cur = await check.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
        assert row[0] == "ok"
    finally:
        await check.close()


@pytest.mark.asyncio
async def test_snapshot_raises_actionable_error_when_integrity_check_itself_raises(tmp_path, monkeypatch):
    """Simulates the realistic corruption shape: VACUUM INTO "succeeds" (the
    .partial file exists) but its contents are not a valid SQLite database
    at all, so PRAGMA integrity_check raises sqlite3.DatabaseError ("file is
    not a database") from *inside* the connection rather than returning a
    not-ok row. Without the except clause this exercises,
    ensure_pre_migration_snapshot would let that exception propagate raw
    instead of the actionable RuntimeError, and the `if not ok` /
    cleanup-and-raise path below it would never run.

    Reaching this naturally would require VACUUM INTO itself to write a
    corrupt file, which it does not do on a healthy filesystem. Instead we
    monkeypatch aiosqlite.connect so that only the connection for the
    verification step (connecting to the tmp `.partial` path) first
    overwrites that file with garbage bytes -- every other aiosqlite.connect
    call (the main db connections used for the CREATE TABLE setup and
    inside ensure_pre_migration_snapshot itself) is passed through
    unchanged. The subsequent real `PRAGMA integrity_check` call then raises
    the genuine sqlite3.DatabaseError on its own, so this test exercises the
    actual production code path, not a hand-thrown substitute.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fitness.db")
    db = await database.get_db()
    try:
        await db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        await db.commit()
    finally:
        await db.close()

    tmp = tmp_path / "test.pre-migration.db.partial"
    final = tmp_path / "test.pre-migration.db"

    real_connect = aiosqlite.connect

    def fake_connect(path, *args, **kwargs):
        if str(path) == str(tmp):
            # Overwrite VACUUM INTO's real output with garbage right before
            # the verification connection opens it, so the real
            # PRAGMA integrity_check call below raises DatabaseError itself.
            tmp.write_bytes(b"not a valid sqlite database at all, simulates corrupted VACUUM output")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(migrations.aiosqlite, "connect", fake_connect)

    async def needs_snapshot(db):
        return True

    with pytest.raises(
        RuntimeError, match="^Pre-migration snapshot failed integrity_check; refusing to migrate$"
    ):
        await migrations.ensure_pre_migration_snapshot("test.pre-migration.db", needs_snapshot)

    assert not tmp.exists(), "the unverifiable .partial file must still be cleaned up"
    assert not final.exists(), "an unverified snapshot must never be promoted to the fixed name"


@pytest.mark.asyncio
async def test_assert_schema_understood_passes_on_empty_migrations_table(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        await db.commit()
    finally:
        await db.close()

    await migrations.assert_schema_understood()  # must not raise


@pytest.mark.asyncio
async def test_assert_schema_understood_passes_on_known_migrations(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        for name in migrations._KNOWN_MIGRATIONS:
            await db.execute(
                "INSERT INTO schema_migrations (name, completed_at) VALUES (?, 'x')", (name,)
            )
        await db.commit()
    finally:
        await db.close()

    await migrations.assert_schema_understood()  # must not raise


@pytest.mark.asyncio
async def test_assert_schema_understood_raises_on_unknown_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        await db.execute(
            "INSERT INTO schema_migrations (name, completed_at) VALUES ('002-from-the-future', 'x')"
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(RuntimeError, match="002-from-the-future"):
        await migrations.assert_schema_understood()


@pytest.mark.asyncio
async def test_assert_schema_understood_passes_against_a_just_migrated_db(tmp_path, monkeypatch):
    """Catches _KNOWN_MIGRATIONS drifting from the literal name run_migration()
    is called with -- this is the exact bug class caught during spec review:
    a typo between the guard's known-names tuple and the actual migration
    name would make an image reject its own migration as unrecognized."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    db = await database.get_db()
    try:
        await db.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        await db.commit()
    finally:
        await db.close()

    for name in migrations._KNOWN_MIGRATIONS:

        async def apply(db):
            pass

        await migrations.run_migration(name, apply)

    await migrations.assert_schema_understood()  # must not raise


@pytest.mark.asyncio
async def test_init_db_creates_schema_migrations_table(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_init_db_calls_the_schema_guard_and_passes_on_a_fresh_db(tmp_path, monkeypatch):
    """A fresh DB has zero migration markers, so the guard must pass --
    if init_db() raised here, EVERY fresh install would fail to boot."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()  # must not raise


@pytest.mark.asyncio
async def test_init_db_fails_lifespan_if_db_has_an_unknown_migration_marker(tmp_path, monkeypatch):
    """Proves the guard is actually wired in, not just importable. Seeds an
    unknown marker directly (bypassing run_migration, which is fine here --
    this test only cares that init_db() calls assert_schema_understood)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()  # first boot: creates schema_migrations, passes

    db = await database.get_db()
    try:
        await db.execute(
            "INSERT INTO schema_migrations (name, completed_at) VALUES ('999-from-the-future', 'x')"
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(RuntimeError, match="999-from-the-future"):
        await database.init_db()
