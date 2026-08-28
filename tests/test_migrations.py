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
