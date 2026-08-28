"""Tests for shared/migrations.py -- the once-only migration runner and
schema-version guard. See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md
section (c) for the full design rationale.
"""

import asyncio
from datetime import datetime, timezone

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


# 001-person-id-rebuild tests appended below the Phase 0 runner tests
# above. See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md
# §c.8 for the full required-test list this section implements.


@pytest.mark.asyncio
async def test_fresh_db_gets_new_shape_directly_and_one_primary_person(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(sleep)")
        cols = {row["name"]: row for row in await cur.fetchall()}
        assert "person_id" in cols
        pk_cols = sorted(name for name, row in cols.items() if row["pk"])
        assert pk_cols == ["date", "person_id"]

        cur = await db.execute("SELECT COUNT(*) FROM persons WHERE is_primary = 1")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_preserves_row_counts_and_backfills_person_id(production_schema_db, monkeypatch):
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == 17
        cur = await db.execute("SELECT COUNT(*) FROM weight_history")
        assert (await cur.fetchone())[0] == 34
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("SELECT COUNT(*) FROM weight_history WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0

        cur = await db.execute("SELECT id FROM persons WHERE is_primary = 1")
        primary_id = (await cur.fetchone())["id"]
        cur = await db.execute("SELECT DISTINCT person_id FROM weight_history")
        assert [row["person_id"] for row in await cur.fetchall()] == [primary_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_parity_fresh_vs_migrated(tmp_path, monkeypatch, production_schema_db):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    fresh_db = await database.get_db()

    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    migrated_db = await database.get_db()

    try:
        for table in migrations._REBUILD_TABLES + ["sync_status", "weight_log"]:
            fresh_info = await (await fresh_db.execute(f"PRAGMA table_info([{table}])")).fetchall()
            migrated_info = await (await migrated_db.execute(f"PRAGMA table_info([{table}])")).fetchall()
            fresh_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in fresh_info)
            migrated_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in migrated_info)
            assert fresh_shape == migrated_shape, f"{table} shape diverged between fresh and migrated"

            fresh_idx = sorted(r["name"] for r in await (await fresh_db.execute(f"PRAGMA index_list([{table}])")).fetchall())
            migrated_idx = sorted(r["name"] for r in await (await migrated_db.execute(f"PRAGMA index_list([{table}])")).fetchall())
            assert fresh_idx == migrated_idx, f"{table} index set diverged between fresh and migrated"
    finally:
        await fresh_db.close()
        await migrated_db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(production_schema_db):
    await database.init_db()
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        first_count = (await cur.fetchone())[0]
    finally:
        await db.close()

    await database.init_db()  # second boot against the already-migrated DB
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == first_count
        cur = await db.execute("SELECT COUNT(*) FROM persons")
        assert (await cur.fetchone())[0] == 1, "a second run created a second primary person"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_interruption_rolls_back_rebuild_but_weight_log_person_id_stays_committed(
    production_schema_db, monkeypatch
):
    """Spec §c.6: the *rebuild* (11 tables + marker) is fully-old-or-fully-new,
    but weight_log.person_id is added by a separate, already-committed
    _add_columns step at init_db's step 2. A test that checks only
    byte-identity on the rebuilt tables would pass in both failure modes and
    prove nothing -- this test asserts both halves explicitly."""
    call_count = 0
    real_apply = migrations._apply_person_id_rebuild

    async def failing_apply(db):
        nonlocal call_count
        call_count += 1
        person_id = await migrations._ensure_primary_person(db)
        if await migrations._has_column(db, "sleep", "person_id"):
            return
        for i, table in enumerate(migrations._REBUILD_TABLES):
            if i == 5:
                raise RuntimeError("simulated crash mid-rebuild")
            columns = await migrations._rebuild_columns(db, table)
            col_names = ", ".join(f"[{name}]" for name, _ in columns)
            col_ddl = ", ".join(f"[{name}] {type_}" for name, type_ in columns)
            await db.execute(
                f"CREATE TABLE [{table}__new] (person_id INTEGER NOT NULL, date TEXT NOT NULL, "
                f"{col_ddl}, PRIMARY KEY (person_id, date))"
            )
            await db.execute(
                f"INSERT INTO [{table}__new] (person_id, date, {col_names}) "
                f"SELECT ?, date, {col_names} FROM [{table}]",
                (person_id,),
            )
            await db.execute(f"DROP TABLE [{table}]")
            await db.execute(f"ALTER TABLE [{table}__new] RENAME TO [{table}]")

    monkeypatch.setattr(migrations, "_apply_person_id_rebuild", failing_apply)

    pre_shapes = {}
    db = await database.get_db()
    try:
        for table in migrations._REBUILD_TABLES + ["sync_status"]:
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
            pre_shapes[table] = (await cur.fetchone())["sql"]
    finally:
        await db.close()

    with pytest.raises(RuntimeError, match="simulated crash"):
        # Calling init_db() itself is correct here, not a shortcut: its
        # local `from shared.migrations import _apply_person_id_rebuild`
        # resolves the monkeypatched attribute at call time, so this run
        # uses failing_apply while still exercising the real init_db/
        # run_migration wiring end to end.
        await database.init_db()

    db = await database.get_db()
    try:
        for table in migrations._REBUILD_TABLES + ["sync_status"]:
            if table == "weight_history":
                # weight_history's Track-B additive columns (body_water,
                # bone_mass_g, muscle_mass_g) are added by a separate,
                # already-committed _add_columns step at init_db's step 1 --
                # same mechanism as weight_log.person_id -- so they land
                # regardless of whether the rebuild itself succeeds. A
                # byte-exact sql comparison against pre_shapes would fail
                # here even on a correct rollback; assert on the
                # rebuild-specific shape instead.
                cols = await (await db.execute("PRAGMA table_info(weight_history)")).fetchall()
                assert not any(c["name"] == "person_id" for c in cols), "weight_history rebuild was not rolled back"
                assert [c["name"] for c in cols if c["pk"]] == ["date"], "weight_history PK was rebuilt"
                continue
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
            assert (await cur.fetchone())["sql"] == pre_shapes[table], f"{table} was not rolled back"
        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = '001-person-id-rebuild'")
        assert await cur.fetchone() is None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%__new'"
        )
        assert await cur.fetchall() == [], "a __new table survived the rollback"

        cur = await db.execute("PRAGMA table_info(weight_log)")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "person_id" in cols, "weight_log.person_id must survive -- it committed independently"
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NOT NULL")
        assert (await cur.fetchone())[0] == 0, "person_id must be all-NULL before the rebuild completes"
    finally:
        await db.close()

    monkeypatch.setattr(migrations, "_apply_person_id_rebuild", real_apply)
    await database.init_db()  # run again cleanly
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0, "did not converge on a clean re-run"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_snapshot_is_created_before_the_rebuild_runs(production_schema_db):
    await database.init_db()
    snapshot = production_schema_db.parent / "fitness.pre-001-person-id.db"
    assert snapshot.exists()

    check = await aiosqlite.connect(str(snapshot))
    try:
        cur = await check.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == 17
        cur = await check.execute("PRAGMA table_info(sleep)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "person_id" not in cols, "snapshot must predate the rebuild (weight_log.person_id may exist, sleep must not)"
    finally:
        await check.close()


@pytest.mark.asyncio
async def test_snapshot_not_taken_on_a_fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    assert not (tmp_path / "fitness.pre-001-person-id.db").exists()


@pytest.mark.asyncio
async def test_cross_person_isolation_after_second_person_exists(production_schema_db):
    """Regression test for the bug this whole design exists to prevent.
    Phase 1 itself never creates a second person, but the rebuilt PK must
    already make this safe -- Phase 2 relies on that, not on any code it
    will add."""
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT id FROM persons WHERE is_primary = 1")
        person_a = (await cur.fetchone())["id"]
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", datetime.now(timezone.utc).isoformat()),
        )
        person_b = cursor.lastrowid
        await db.commit()

        await db.execute(
            "INSERT INTO sleep (person_id, date, duration_seconds) VALUES (?, '2026-01-01', 100)",
            (person_a,),
        )
        await db.execute(
            "INSERT INTO sleep (person_id, date, duration_seconds) VALUES (?, '2026-01-01', 200)",
            (person_b,),
        )
        await db.commit()

        cur = await db.execute("SELECT COUNT(*) FROM sleep WHERE date = '2026-01-01'")
        assert (await cur.fetchone())[0] == 2, "same date, different persons must NOT overwrite each other"
    finally:
        await db.close()


# --- primary-person ownership --------------------------------------------------
# init_db() runs migration 001 before either service's lifespan reaches
# bootstrap_first_admin(), so on a fresh database _ensure_primary_person()
# finds no admin to grant the person to and the committed marker stops it
# ever running again. shared/database.py's ensure_primary_person_grant() is
# the lifespan-side repair for that; these tests pin both halves.


async def _seed_admin(username: str = "the-admin") -> int:
    from tests.conftest import seed_user

    return await seed_user(username, role="admin")


async def _grant_rows(db) -> list[tuple]:
    """As plain tuples -- sqlite3.Row never compares equal to a tuple."""
    cur = await db.execute("SELECT person_id, user_id, access FROM person_grants")
    return [(r["person_id"], r["user_id"], r["access"]) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_migration_alone_leaves_a_fresh_db_primary_person_unowned(tmp_path, monkeypatch):
    """Characterization test for the gap ensure_primary_person_grant() closes.
    If this ever starts failing because the grant appears here, the lifespan
    call below is redundant and should be reconsidered -- not deleted
    silently."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()

    db = await database.get_db()
    try:
        assert await _grant_rows(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_install_lifespan_order_gives_the_admin_an_own_grant(tmp_path, monkeypatch):
    """The real lifespan sequence: init_db() -> bootstrap_first_admin() ->
    ensure_primary_person_grant() (see both services' app.py)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()
    admin_id = await _seed_admin()
    await database.ensure_primary_person_grant()

    db = await database.get_db()
    try:
        person_id = await database.get_primary_person_id()
        assert await _grant_rows(db) == [(person_id, admin_id, "own")]
        cur = await db.execute("SELECT default_person_id FROM users WHERE id = ?", (admin_id,))
        assert (await cur.fetchone())["default_person_id"] == person_id
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ensure_primary_person_grant_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()
    await _seed_admin()
    await database.ensure_primary_person_grant()
    await database.ensure_primary_person_grant()
    await database.ensure_primary_person_grant()

    db = await database.get_db()
    try:
        assert len(await _grant_rows(db)) == 1, "repeated boots duplicated the grant"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ensure_primary_person_grant_noops_without_an_admin(tmp_path, monkeypatch):
    """A fresh install with VITALFORGE_PASS unset has an empty users table;
    the grant must simply wait for the boot after an admin is seeded rather
    than crash the lifespan."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()
    await database.ensure_primary_person_grant()  # must not raise

    db = await database.get_db()
    try:
        assert await _grant_rows(db) == []
    finally:
        await db.close()

    admin_id = await _seed_admin()
    await database.ensure_primary_person_grant()
    db = await database.get_db()
    try:
        assert [row[1] for row in await _grant_rows(db)] == [admin_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ensure_primary_person_grant_never_overwrites_a_chosen_default(tmp_path, monkeypatch):
    """Phase 2 lets an account pick its own default person. A later boot must
    not silently drag it back to the primary."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()
    admin_id = await _seed_admin()
    await database.ensure_primary_person_grant()

    db = await database.get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("someone-else", "Someone Else", datetime.now(timezone.utc).isoformat()),
        )
        other_person = cursor.lastrowid
        await db.execute(
            "UPDATE users SET default_person_id = ? WHERE id = ?", (other_person, admin_id)
        )
        await db.commit()
    finally:
        await db.close()

    await database.ensure_primary_person_grant()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT default_person_id FROM users WHERE id = ?", (admin_id,))
        assert (await cur.fetchone())["default_person_id"] == other_person
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_with_an_existing_admin_grants_inside_the_migration(tmp_path, monkeypatch):
    """The other half: when an admin DOES already exist, the grant is created
    by _ensure_primary_person() during the migration itself, with no lifespan
    help. Exercised directly on a connection because run_migration() has
    already recorded its marker by the time a test could observe it."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()
    admin_id = await _seed_admin()

    db = await database.get_db()
    try:
        await db.execute("DELETE FROM person_grants")
        await db.execute("DELETE FROM persons")
        await db.commit()

        person_id = await migrations._ensure_primary_person(db)
        await db.commit()

        assert await _grant_rows(db) == [(person_id, admin_id, "own")]
        cur = await db.execute("SELECT default_person_id FROM users WHERE id = ?", (admin_id,))
        assert (await cur.fetchone())["default_person_id"] == person_id
    finally:
        await db.close()


# --- activities re-key (M1) ----------------------------------------------------
# activities is not in _REBUILD_TABLES: it keeps its AUTOINCREMENT id and only
# moves its UNIQUE from file_sha256 to (person_id, file_sha256), so
# _rebuild_columns cannot generate it and _rebuild_activities writes it out
# longhand. That means the generic parity test above does not cover it.

_LEGACY_ACTIVITIES_DDL = """
    CREATE TABLE activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time_utc TEXT NOT NULL,
        sport TEXT,
        duration_seconds INTEGER,
        distance_m REAL,
        calories INTEGER,
        avg_hr INTEGER,
        max_hr INTEGER,
        elevation_gain_m REAL,
        source_format TEXT NOT NULL CHECK (source_format IN ('fit')),
        file_sha256 TEXT NOT NULL UNIQUE,
        imported_at TEXT NOT NULL,
        raw_summary_json TEXT
    )
"""


async def _seed_legacy_activities(db_path, rows: int = 2) -> None:
    """Add a PRE-rebuild activities table to a legacy database.

    tests/fixtures/production_schema.sql predates FIT import, so without this
    the migrated database would get its activities table fresh from init_db's
    DDL and the rebuild would never actually run.
    """
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.execute(_LEGACY_ACTIVITIES_DDL)
        await conn.execute("CREATE INDEX idx_activities_start_time ON activities(start_time_utc)")
        for i in range(rows):
            await conn.execute(
                "INSERT INTO activities (start_time_utc, sport, source_format, file_sha256, imported_at) "
                "VALUES (?, 'running', 'fit', ?, ?)",
                (f"2026-01-0{i + 1}T00:00:00Z", f"hash-{i}", "2026-01-01T00:00:00Z"),
            )
        await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_activities_rebuild_preserves_rows_and_ids(production_schema_db):
    await _seed_legacy_activities(production_schema_db, rows=2)
    await database.init_db()

    db = await database.get_db()
    try:
        person_id = await database.get_primary_person_id()
        cur = await db.execute("SELECT id, person_id, file_sha256 FROM activities ORDER BY id")
        rows = [(r["id"], r["person_id"], r["file_sha256"]) for r in await cur.fetchall()]
        assert rows == [(1, person_id, "hash-0"), (2, person_id, "hash-1")], (
            "activities rows, their AUTOINCREMENT ids, or the person_id backfill did not survive"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_activities_schema_parity_fresh_vs_migrated(tmp_path, monkeypatch, production_schema_db):
    await _seed_legacy_activities(production_schema_db)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    fresh_db = await database.get_db()

    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    migrated_db = await database.get_db()

    try:
        for conn_pair in ("table_info", "index_list"):
            fresh = await (await fresh_db.execute(f"PRAGMA {conn_pair}(activities)")).fetchall()
            migrated = await (await migrated_db.execute(f"PRAGMA {conn_pair}(activities)")).fetchall()
            if conn_pair == "table_info":
                fresh_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in fresh)
                migrated_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in migrated)
            else:
                # Includes sqlite_autoindex_activities_1 -- the UNIQUE's own
                # index, which must survive the __new table's RENAME under the
                # same name it has on a fresh database.
                fresh_shape = sorted(r["name"] for r in fresh)
                migrated_shape = sorted(r["name"] for r in migrated)
            assert fresh_shape == migrated_shape, f"activities {conn_pair} diverged"
    finally:
        await fresh_db.close()
        await migrated_db.close()


@pytest.mark.asyncio
async def test_two_persons_can_import_the_same_file_after_the_rebuild(production_schema_db):
    """The reason activities is a rebuild and not an additive column: the old
    global UNIQUE(file_sha256) made one person's import silently reject
    another person's identical FIT file."""
    await _seed_legacy_activities(production_schema_db, rows=1)
    await database.init_db()

    db = await database.get_db()
    try:
        person_a = await database.get_primary_person_id()
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", datetime.now(timezone.utc).isoformat()),
        )
        person_b = cursor.lastrowid
        await db.execute(
            "INSERT INTO activities (person_id, start_time_utc, sport, source_format, file_sha256, imported_at) "
            "VALUES (?, '2026-01-01T00:00:00Z', 'running', 'fit', 'hash-0', '2026-01-01T00:00:00Z')",
            (person_b,),
        )
        await db.commit()

        cur = await db.execute("SELECT COUNT(*) FROM activities WHERE file_sha256 = 'hash-0'")
        assert (await cur.fetchone())[0] == 2, "same file must be importable by two different persons"

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO activities (person_id, start_time_utc, sport, source_format, file_sha256, imported_at) "
                "VALUES (?, '2026-01-01T00:00:00Z', 'running', 'fit', 'hash-0', '2026-01-01T00:00:00Z')",
                (person_a,),
            )
        await db.rollback()
    finally:
        await db.close()


# --- rebuild pre-flight (L3) ---------------------------------------------------


@pytest.mark.asyncio
async def test_migration_refuses_a_null_date_instead_of_failing_mid_rebuild(production_schema_db):
    """SQLite lets `date TEXT PRIMARY KEY` hold NULL; the rebuilt
    `date TEXT NOT NULL` does not. Without the pre-flight this surfaces as an
    opaque IntegrityError partway through a one-way upgrade."""
    conn = await aiosqlite.connect(str(production_schema_db))
    try:
        await conn.execute("INSERT INTO steps (date, value) VALUES (NULL, 123)")
        await conn.commit()
    finally:
        await conn.close()

    with pytest.raises(RuntimeError, match="steps has 1 row\\(s\\) with a NULL date"):
        await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = '001-person-id-rebuild'")
        assert await cur.fetchone() is None, "marker committed despite the pre-flight raising"
        cur = await db.execute("PRAGMA table_info(sleep)")
        assert not any(r["name"] == "person_id" for r in await cur.fetchall()), (
            "rebuild was not rolled back"
        )
    finally:
        await db.close()


# --- schema guard ordering (M3) ------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_marker_is_refused_before_any_snapshot_is_written(production_schema_db):
    """assert_schema_understood() runs BEFORE the migrations, so a database
    from a newer image is refused while this image has still changed nothing
    -- no snapshot file, no write transaction."""
    conn = await aiosqlite.connect(str(production_schema_db))
    try:
        await conn.execute(migrations.SCHEMA_MIGRATIONS_TABLE_SQL)
        await conn.execute(
            "INSERT INTO schema_migrations (name, completed_at) VALUES ('999-from-the-future', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await conn.commit()
    finally:
        await conn.close()

    with pytest.raises(RuntimeError, match="999-from-the-future"):
        await database.init_db()

    snapshot = production_schema_db.parent / migrations._PERSON_ID_REBUILD_SNAPSHOT_NAME
    assert not snapshot.exists(), "a downgrade-boot wrote a snapshot before refusing to serve"


@pytest.mark.asyncio
async def test_002_rekeys_activities_on_a_db_that_already_applied_001(tmp_path, monkeypatch):
    """The state no other test in this file occupies: 001's marker already
    committed, activities still on its pre-rebuild shape.

    This is what a development database created from an earlier commit of
    this branch looks like. Folding the activities re-key into 001 instead of
    giving it its own marker would leave exactly these databases un-migrated
    -- run_migration skips 001 wholesale once its marker exists -- while every
    /api/activities route queried a person_id column that was never added.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "mid.db")
    await database.init_db()

    db = await database.get_db()
    try:
        await db.execute("DROP TABLE activities")
        await db.execute(_LEGACY_ACTIVITIES_DDL)
        await db.execute(
            "INSERT INTO activities (start_time_utc, sport, source_format, file_sha256, imported_at) "
            "VALUES ('2026-01-01T00:00:00Z', 'running', 'fit', 'hash-0', '2026-01-01T00:00:00Z')"
        )
        await db.execute("DELETE FROM schema_migrations WHERE name = '002-activities-person-id'")
        await db.commit()

        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = '001-person-id-rebuild'")
        assert await cur.fetchone() is not None, "fixture must keep 001 applied"
    finally:
        await db.close()

    await database.init_db()  # reboot on the new code

    db = await database.get_db()
    try:
        cols = {r["name"] for r in await (await db.execute("PRAGMA table_info(activities)")).fetchall()}
        assert "person_id" in cols, "002 did not re-key activities on a database that already ran 001"
        cur = await db.execute("SELECT person_id FROM activities")
        assert (await cur.fetchone())["person_id"] == await database.get_primary_person_id()
        idx = {r["name"] for r in await (await db.execute("PRAGMA index_list(activities)")).fetchall()}
        assert "idx_activities_person_start_time" in idx
    finally:
        await db.close()


async def _delete_legacy_activity(db_path, activity_id: int) -> None:
    """Delete a row from the PRE-rebuild activities table, leaving
    sqlite_sequence.seq above MAX(id) exactly as a real deletion would."""
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_activities_rebuild_preserves_the_autoincrement_high_water_mark(production_schema_db):
    """DROP TABLE deletes the table's sqlite_sequence row, and the copy leaves
    the new counter at MAX(id) of the rows that SURVIVED. Any id above that --
    belonging to a row deleted before the migration -- would be handed out a
    second time, and "AUTOINCREMENT never reuses rowids" is load-bearing here
    (it is the stated reason person_grants' decorative REFERENCES carry no
    privilege-inheritance path).

    The deletion must happen BEFORE init_db(): deleting afterwards only
    exercises ordinary post-migration AUTOINCREMENT behavior, which passes
    whether or not the rebuild preserves anything.
    """
    await _seed_legacy_activities(production_schema_db, rows=3)
    await _delete_legacy_activity(production_schema_db, 3)

    await database.init_db()

    db = await database.get_db()
    try:
        person_id = await database.get_primary_person_id()
        cur = await db.execute("SELECT MAX(id) FROM activities")
        assert (await cur.fetchone())[0] == 2, "the pre-migration delete did not take"

        cursor = await db.execute(
            "INSERT INTO activities (person_id, start_time_utc, sport, source_format, file_sha256, imported_at) "
            "VALUES (?, '2026-02-01T00:00:00Z', 'running', 'fit', 'fresh-hash', '2026-02-01T00:00:00Z')",
            (person_id,),
        )
        await db.commit()
        assert cursor.lastrowid == 4, (
            f"rowid {cursor.lastrowid} reused id 3, deleted before the migration -- "
            "the rebuild reset sqlite_sequence instead of carrying it across"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_activities_rebuild_carries_the_high_water_mark_when_every_row_was_deleted(
    production_schema_db,
):
    """The zero-row copy leaves activities__new with no sqlite_sequence row at
    all, so the UPDATE that carries the counter across matches nothing. This is
    the case the INSERT fallback exists for -- without it the counter restarts
    at 1 and re-issues every id the table ever had."""
    await _seed_legacy_activities(production_schema_db, rows=2)
    await _delete_legacy_activity(production_schema_db, 1)
    await _delete_legacy_activity(production_schema_db, 2)

    await database.init_db()

    db = await database.get_db()
    try:
        person_id = await database.get_primary_person_id()
        cur = await db.execute("SELECT COUNT(*) FROM activities")
        assert (await cur.fetchone())[0] == 0

        cursor = await db.execute(
            "INSERT INTO activities (person_id, start_time_utc, sport, source_format, file_sha256, imported_at) "
            "VALUES (?, '2026-02-01T00:00:00Z', 'running', 'fit', 'fresh-hash', '2026-02-01T00:00:00Z')",
            (person_id,),
        )
        await db.commit()
        assert cursor.lastrowid == 3, (
            f"rowid {cursor.lastrowid} restarted the sequence over ids 1-2, both of which "
            "existed before the migration"
        )
    finally:
        await db.close()


# --- weight_log orphan self-heal (L5) -------------------------------------------


@pytest.mark.asyncio
async def test_boot_reattributes_a_weight_log_row_left_unattributed_after_001(
    production_schema_db,
):
    """A weight_log row written with a NULL person_id AFTER 001 has committed
    is never repaired by the migration -- 001 skips itself forever once its
    marker is in. weight_log.person_id cannot be NOT NULL, so the schema will
    not refuse the row either, and every read path filters `person_id = ?`,
    which makes it invisible rather than mis-attributed.

    Reachable by leaving an old weight-service container running against the
    rebuilt schema, which README's Upgrading step 1 forbids. The next boot
    must repair it.
    """
    await database.init_db()
    person_id = await database.get_primary_person_id()

    # Exactly what a pre-multi-tenancy weight service INSERT looks like: no
    # person_id column at all.
    db = await database.get_db()
    try:
        await db.execute(
            "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp) "
            "VALUES (180.0, 81.65, 81650, '2026-03-01T12:00:00+00:00')"
        )
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 1, "test setup failed to create an orphan row"
    finally:
        await db.close()

    await database.init_db()  # the next container boot

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0, (
            "an unattributed weight_log row survived a boot -- it stays invisible to "
            "/recent, /trend and DELETE forever"
        )
        cur = await db.execute(
            "SELECT COUNT(*) FROM weight_log WHERE person_id = ? AND weight_grams = 81650",
            (person_id,),
        )
        assert (await cur.fetchone())[0] == 1, "the repaired row went to the wrong person"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reattribution_leaves_correctly_attributed_rows_alone(production_schema_db):
    """The self-heal must be a no-op on a healthy database -- it runs on every
    boot, so it must never touch a row that already has an owner."""
    await database.init_db()
    person_id = await database.get_primary_person_id()

    db = await database.get_db()
    try:
        await db.execute(
            "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp) "
            "VALUES (?, 200.0, 90.72, 90720, '2026-03-02T12:00:00+00:00')",
            (person_id + 1,),  # some other person, not the primary
        )
        await db.commit()
    finally:
        await db.close()

    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute(
            "SELECT person_id FROM weight_log WHERE weight_grams = 90720"
        )
        assert (await cur.fetchone())["person_id"] == person_id + 1, (
            "the self-heal reassigned a row that already had an owner"
        )
    finally:
        await db.close()
