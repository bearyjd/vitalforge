# Multi-Tenancy Phase 0 — Migration Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and prove a generic, race-safe, interruption-safe migration runner (`shared/migrations.py`) plus a schema-version boot guard — the foundation every later multi-tenancy phase depends on — with **zero schema change** to any existing table.

**Architecture:** A new `shared/migrations.py` module provides `run_migration()` (an `auth_migrations`-style once-only marker pattern, generalized, atomic with the schema change it guards), `ensure_pre_migration_snapshot()` (a generic `VACUUM INTO` + `integrity_check` + atomic-rename snapshot utility, parameterized so it carries no migration-specific knowledge), and `assert_schema_understood()` (refuses to serve a DB carrying a migration marker this image doesn't recognize). `shared/database.py` gets three small, independent changes: `get_db()` raises `busy_timeout` to 30s, `_add_columns()` gains a latency-only shape pre-check, and `init_db()` calls `assert_schema_understood()` as its final step. Two prose corrections land in `docs/prp/00-design.md` and `shared/database.py`'s own comments, because this phase disproves a claim they currently make.

**Tech Stack:** Python 3.12+, `aiosqlite`, `pytest` + `pytest-asyncio` (existing conventions in `tests/conftest.py`), stdlib `sqlite3` (for one test that intentionally bypasses `aiosqlite` — see Task 1).

**Spec:** `docs/superpowers/specs/2026-08-25-family-multitenancy-design.md` — this plan implements §(c) (`c.3` the runner, `c.4` where it fits, `c.7` mitigation 1 and 2, `c.8` the two gating tests plus the runner/guard/snapshot unit tests) and §(h)'s Phase 0 scope. Appendix B's phase-0 row is the authoritative file list; this plan matches it exactly. Read the spec sections cited in each task before implementing — this plan quotes the load-bearing parts, not all of the reasoning behind them.

## Global Constraints

- **No schema change to any existing table in this phase.** The only new persistent state is the `schema_migrations` table itself (additive — a brand-new `CREATE TABLE IF NOT EXISTS`, not an alteration of anything). §(h): "Lands alone, is independently valuable, and is reviewable in isolation."
- **`ensure_pre_migration_snapshot()` and `run_migration()` are written and tested in this phase but are NOT wired into `init_db()` yet.** Only `assert_schema_understood()` gets called from `init_db()` in Phase 0 (Appendix B: "`init_db` calls the guard as step 5"). Wiring in the actual `001-person-id-rebuild` call is Phase 1's job, once `_apply_person_id_rebuild` and `_needs_person_id_rebuild` exist. Do not pull that work into this phase — it needs the rebuild logic this phase deliberately does not build.
- **`_KNOWN_MIGRATIONS = ("001-person-id-rebuild",)` is a forward reference.** This exact string must match, character for character, whatever name Phase 1's `run_migration()` call eventually passes. Get this wrong and the guard reads its own image's migration as one from the future and boot-loops the container (this exact bug was caught and fixed during spec finalization — see Appendix C, finding on Q12). Every place this plan writes that string, it is deliberate and must stay in sync.
- **Every new async DB helper follows the existing `get_db()` / `try` / `finally: await db.close()` pattern** already used throughout `shared/database.py` and `shared/auth.py`. Do not introduce connection pooling.
- **`BaseException`, not `Exception`, in the migration runner's rollback handler** — matching `shared/auth.py:1186-1197` and `:1226-1237` — so a cancelled lifespan still rolls back rather than leaving a held write lock.
- Run `ruff check .` and the full non-playwright `pytest -q` (per `CLAUDE.md`'s verified commands) after every task, not just at the end.

---

## File Structure

- **Create `shared/migrations.py`** — the entire migration-runner surface: `run_migration()`, `ensure_pre_migration_snapshot()`, `assert_schema_understood()`, the `SCHEMA_MIGRATIONS_TABLE_SQL` DDL constant, `_KNOWN_MIGRATIONS`. One file, one responsibility (mirrors `shared/auth.py`'s pattern of owning a whole concern), keeps `shared/database.py` under this repo's 800-line convention as the schema grows in later phases.
- **Create `tests/test_migrations.py`** — every test in this plan except the two gating tests, which get their own file (see below) because they test SQLite/aiosqlite's own behavior, not this repo's code, and are meant to be read and understood independently of the rest of the suite.
- **Create `tests/test_migration_gating_assumptions.py`** — the two gating tests from §c.8. Deliberately separated from `test_migrations.py`: these tests exist to validate an assumption the whole design depends on, not to regression-test a function. If either ever fails (e.g. a future SQLite/aiosqlite upgrade changes DDL-rollback semantics), it should be obviously distinct from "a feature broke."
- **Modify `shared/database.py`** — `get_db()` (busy_timeout), `_add_columns()` (shape pre-check), `init_db()` (create `schema_migrations`, call `assert_schema_understood()` as the final step), the `weight_log`-additive-columns comment (lines 8-19, amended per Appendix A).
- **Modify `docs/prp/00-design.md`** — the imprecise "any future migration that adds a defaulted column rewrites the table" claim at lines ~1596-1598.

---

### Task 1: Gating test — DDL rollback through `aiosqlite`

**Files:**
- Create: `tests/test_migration_gating_assumptions.py`

**Interfaces:**
- Consumes: nothing from this repo's code — tests `aiosqlite`'s and stdlib `sqlite3`'s own transaction semantics directly.
- Produces: nothing later tasks import. This is a standalone assumption check. **If it fails, stop the whole plan** — per spec §c.6/Appendix A, a negative result here means the single-transaction rebuild design is wrong and the spec itself needs rewriting before any implementation continues.

This is the test the spec (§c.8) says to write **first**, before any other code: "Through `aiosqlite`, on a connection built exactly as `get_db()` builds one plus `isolation_level = None`: open `BEGIN IMMEDIATE`, run the full create/copy/drop/rename sequence plus a marker `INSERT`, then `rollback()`. Assert the original `sqlite_master.sql` for every touched table is byte-identical, all rows intact, no marker, and no `__new` tables surviving." It uses a synthetic scratch table, not any real production table — this test verifies SQLite's/aiosqlite's own engine behavior, which is table-shape-independent, and doing it this way means Task 1 needs zero knowledge of Phase 1's actual schema.

The spec also asks for the same test against legacy `isolation_level` (`""`) "and record the result either way" — both variants are written below.

- [ ] **Step 1: Write the gating test**

```python
"""Gating tests for the migration-runner design (spec §c.8, run before any
other code in this plan). These test SQLite's and aiosqlite's own DDL/
transaction semantics, not this repo's code — if either fails, the
single-transaction rebuild design in the multi-tenancy spec is wrong and
must be revisited before continuing, per spec Appendix A footnote 1.
"""

import aiosqlite
import pytest

import shared.database as database


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


async def _run_rebuild_then_rollback(db, isolation_level):
    db.isolation_level = isolation_level
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

    db = await aiosqlite.connect(str(db_path))
    try:
        await _run_rebuild_then_rollback(db, isolation_level=None)
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

    db = await aiosqlite.connect(str(db_path))
    try:
        await _run_rebuild_then_rollback(db, isolation_level="")
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
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_migration_gating_assumptions.py -v`
Expected: both PASS. **If `test_ddl_rebuild_rolls_back_cleanly_with_isolation_level_none` fails, STOP.** Do not proceed to Task 2 or any later task — report back immediately, because the spec's core design assumption is wrong and the spec itself needs revisiting before more code is written on top of it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_migration_gating_assumptions.py
git commit -m "test: gate the multi-tenancy migration design on DDL rollback semantics"
```

---

### Task 2: Gating test — cross-connection DDL visibility

**Files:**
- Modify: `tests/test_migration_gating_assumptions.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing later tasks import. Standalone assumption check, same file as Task 1.

Spec §c.8: "Open connection 1 and run `init_db()`'s DDL body on it; while it is still open, open connection 2 and issue `BEGIN IMMEDIATE` + a trivial `CREATE TABLE`. Assert it does not block or raise. Then repeat with a seed `INSERT` added to connection 1's body and assert what happens." This is the verifiable half of risk 3 in §c.2 — it's what stops a future change from moving `run_migration()`'s connection back inside `init_db()`'s connection lifetime (which would deadlock, per the docstring in Task 3).

- [ ] **Step 1: Write the test**

```python
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

    conn2 = await aiosqlite.connect(str(db_path))
    try:
        conn2.isolation_level = None
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

    conn2 = await aiosqlite.connect(str(db_path))
    conn2.isolation_level = None
    await conn2.execute("PRAGMA busy_timeout = 2000")  # short timeout, this should time out fast
    try:
        with pytest.raises(aiosqlite.OperationalError, match="database is locked"):
            await conn2.execute("BEGIN IMMEDIATE")
            await conn2.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
    finally:
        await conn2.close()
        await conn1.commit()
        await conn1.close()
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_migration_gating_assumptions.py -v`
Expected: all 4 tests PASS. The second test documents (via the `pytest.raises`) that an uncommitted write on connection 1 DOES block/lock out connection 2 — this is exactly why `init_db()` must close its connection before `shared/migrations.py` opens a new one (Task 8, spec §c.4).

- [ ] **Step 3: Commit**

```bash
git add tests/test_migration_gating_assumptions.py
git commit -m "test: gate cross-connection DDL visibility assumption"
```

---

### Task 3: `run_migration()` — the once-only migration runner

**Files:**
- Create: `shared/migrations.py`
- Create: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `shared.database.get_db` (existing).
- Produces: `shared.migrations.SCHEMA_MIGRATIONS_TABLE_SQL: str` (the `CREATE TABLE IF NOT EXISTS` statement, exported so Task 8 doesn't duplicate it), `shared.migrations.run_migration(name: str, apply: Callable[[aiosqlite.Connection], Awaitable[None]]) -> None` (async).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.migrations'` (or `ImportError`).

- [ ] **Step 3: Write `shared/migrations.py`**

```python
"""Migration runner and schema-version guard for VitalForge.

See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md section
(c) for the full design rationale -- this module implements it close to
verbatim; deviations from the spec's code samples are noted inline.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiosqlite

from shared.database import get_db

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        name         TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL
    )
"""


async def run_migration(name: str, apply: Callable[[aiosqlite.Connection], Awaitable[None]]) -> None:
    """Run one migration exactly once, atomically, across both services.

    MUST be called with no other connection from this process open against
    the same file -- see shared/database.py's init_db() ordering comment.
    This function opens its own connection and takes the write lock; if a
    caller's connection were still open AND holding a write transaction,
    BEGIN IMMEDIATE below would block on a lock held by the same coroutine
    that will never yield, wait out the busy_timeout, and raise
    "database is locked" -- which under `restart: unless-stopped` becomes a
    permanent boot loop.

    The marker is committed in the SAME transaction as the schema change,
    so the two can never disagree (verified in
    tests/test_migration_gating_assumptions.py).

    Concurrency: multiple callers may invoke this during startup against the
    same file with no ordering between them. BEGIN IMMEDIATE serializes
    them -- the loser blocks until the winner commits, then observes the
    marker and no-ops. This is why the marker check must be INSIDE the
    transaction: a pre-check would be TOCTOU-racy in exactly the way
    shared/database.py's _add_columns docstring describes.

    "database is locked" is NOT swallowed, matching _add_columns' policy --
    a container that cannot migrate must fail its lifespan and be restarted
    rather than serve traffic against a schema it did not verify.
    """
    db = await get_db()
    try:
        # autocommit + an explicit BEGIN IMMEDIATE, rather than get_db()'s
        # inherited legacy isolation_level (""), because this is the mode
        # tests/test_migration_gating_assumptions.py actually verified DDL
        # rollback under. Do not remove this line on the grounds that "the
        # rest of the codebase doesn't set it": the rest of the codebase
        # only runs DML inside its explicit transactions, never DDL.
        db.isolation_level = None
        # 30s, not the sqlite3 default of 5s: the loser of a migration race
        # waits for the winner's entire migration, which can exceed 5s on a
        # database with years of history.
        await db.execute("PRAGMA busy_timeout = 30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,))
            done = await cur.fetchone()
            if done is not None:
                await db.rollback()
                return
            started = time.monotonic()
            await apply(db)
            await db.execute(
                "INSERT INTO schema_migrations (name, completed_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        except BaseException:
            # Explicit, matching shared/auth.py's rollback pattern around its
            # own BEGIN IMMEDIATE blocks. Closing the connection in `finally`
            # would also discard the transaction, but relying on that is the
            # kind of implicit behavior this module exists to avoid.
            # BaseException, not Exception, so a cancelled lifespan also
            # rolls back rather than leaving a held lock.
            await db.rollback()
            raise
        logger.warning("Applied schema migration %s in %.2fs", name, time.monotonic() - started)
    finally:
        await db.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/migrations.py tests/test_migrations.py
git commit -m "feat: add run_migration(), a once-only atomic migration runner"
```

---

### Task 4: `get_db()` raises `busy_timeout` to 30s

**Files:**
- Modify: `shared/database.py:62-68`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: none new.
- Produces: nothing new exported; changes existing `get_db()` behavior. Every existing caller of `get_db()` across both services picks this up automatically.

Spec §c.3: without this, service B can be at `init_db()`'s `_add_columns` step on the stdlib 5s default while service A holds the write lock for a long migration — B's `ALTER TABLE ADD COLUMN` blocks, times out at 5s, and `database is locked` propagates, restarting B in a loop for as long as A's migration takes. This is a strict improvement for the request path too (today a 5s stall becomes a 500).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migrations.py::test_get_db_sets_a_30_second_busy_timeout -v`
Expected: FAIL, `assert 5000 == 30000` (aiosqlite's default) or similar.

- [ ] **Step 3: Update `get_db()`**

In `shared/database.py`, change:

```python
async def get_db() -> aiosqlite.Connection:
    """Open a connection to the SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db
```

to:

```python
async def get_db() -> aiosqlite.Connection:
    """Open a connection to the SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    # 30s, not aiosqlite's 5s default: a migration (shared/migrations.py's
    # run_migration) can legitimately hold the write lock longer than 5s on
    # a database with years of history, and every connection that might
    # race it -- not just the migration's own -- needs to wait that out
    # rather than surface "database is locked" as a request-path 500 or a
    # boot-loop in the other service. See the multi-tenancy design spec's
    # section (c) for the full reasoning.
    await db.execute("PRAGMA busy_timeout = 30000")
    return db
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migrations.py::test_get_db_sets_a_30_second_busy_timeout -v`
Expected: PASS.

- [ ] **Step 5: Run the full existing suite to check for regressions**

Run: `pytest -q`
Expected: all existing tests still pass (this change only raises a timeout ceiling; it does not change any success-path behavior).

- [ ] **Step 6: Commit**

```bash
git add shared/database.py tests/test_migrations.py
git commit -m "fix: raise get_db()'s busy_timeout to 30s to match migration hold times"
```

---

### Task 5: `_add_columns()` shape pre-check (latency only, not correctness)

**Files:**
- Modify: `shared/database.py:44-59`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: none new.
- Produces: nothing new exported; changes `_add_columns()`'s internal behavior only. Its external contract (idempotent, safe under concurrent callers, still relies on attempt-and-swallow for correctness) is unchanged.

Spec §c.3: today, the loser of a race between two services calling `_add_columns` for the same column blocks on an `ALTER TABLE ADD COLUMN` that is going to fail `duplicate column name` and be swallowed anyway. A `PRAGMA table_info` read costs nothing and, in WAL mode, does not block behind a writer — so the loser can skip the wait entirely when the column is already present. **This does NOT change where correctness comes from**: correctness still comes entirely from attempt-and-swallow. The pre-check is a pure latency optimization that is allowed to be wrong (i.e., allowed to occasionally still attempt an `ALTER TABLE` that turns out to be a no-op) — it must never be relied on to *decide* whether to add the column.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `pytest tests/test_migrations.py::test_add_columns_skips_alter_when_column_already_present -v`
Expected: FAIL (the current implementation always attempts `ALTER TABLE`, so `executed` contains it).

- [ ] **Step 3: Update `_add_columns()`**

In `shared/database.py`, change:

```python
async def _add_columns(db, table: str, column_ddls: list[str]):
    """Attempt-and-swallow, not PRAGMA-table_info-then-act: both services run
    init_db() against the same file and docker-compose starts them together,
    so a pre-check would be TOCTOU-racy -- both could observe "absent" and
    both then attempt the ADD COLUMN. Only the duplicate-column error is
    swallowed; `database is locked` must propagate so a container that
    cannot migrate fails its lifespan and is restarted rather than serving
    traffic against a half-migrated schema.
    """
    for column_ddl in column_ddls:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
            await db.commit()
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
```

to:

```python
async def _add_columns(db, table: str, column_ddls: list[str]):
    """Attempt-and-swallow, not PRAGMA-table_info-then-act, for CORRECTNESS:
    both services run init_db() against the same file and docker-compose
    starts them together, so a pre-check used to DECIDE whether to add a
    column would be TOCTOU-racy -- both could observe "absent" and both then
    attempt the ADD COLUMN. Only the duplicate-column error is swallowed;
    `database is locked` must propagate so a container that cannot migrate
    fails its lifespan and is restarted rather than serving traffic against
    a half-migrated schema.

    The PRAGMA table_info read below is a LATENCY-ONLY pre-check, not a
    correctness pre-check: it is allowed to be wrong (e.g. under a genuine
    race, both callers can still see "absent" and both attempt the ALTER,
    which is exactly the attempt-and-swallow path this docstring's first
    paragraph describes). What it buys is that the common case -- a second
    service starting up after the first one already added every column --
    skips a lock wait on an ALTER TABLE that was only ever going to hit
    "duplicate column name" and be swallowed anyway.
    """
    for column_ddl in column_ddls:
        column_name = column_ddl.split()[0]
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column_name in existing:
            continue
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
            await db.commit()
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py::test_add_columns_skips_alter_when_column_already_present tests/test_migrations.py::test_add_columns_still_adds_missing_column -v`
Expected: both PASS.

- [ ] **Step 5: Run the full existing suite to check for regressions**

Run: `pytest -q`
Expected: all existing tests still pass, **including `tests/test_migration.py`'s existing `_add_columns` concurrency tests** (`test_concurrent_init_db_both_succeed`, `test_duplicate_column_error_swallowed_but_others_propagate`) — these are the tests that would catch a correctness regression if the pre-check were ever wired in as a decision-maker instead of a latency optimization.

- [ ] **Step 6: Commit**

```bash
git add shared/database.py tests/test_migrations.py
git commit -m "perf: add a latency-only shape pre-check to _add_columns"
```

---

### Task 6: `ensure_pre_migration_snapshot()` — generic pre-migration snapshot utility

**Files:**
- Modify: `shared/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `shared.database.get_db`, `shared.database.DB_PATH`.
- Produces: `shared.migrations.ensure_pre_migration_snapshot(snapshot_name: str, needs_snapshot: Callable[[aiosqlite.Connection], Awaitable[bool]]) -> None` (async).

**Design decision, stated explicitly:** the spec's code sample (§c.7) hardcodes this function to `001-person-id-rebuild` (fixed snapshot filename `fitness.pre-001-person-id.db`, calls a `_needs_person_id_rebuild()` predicate). That coupling is Phase-1-specific and does not belong in Phase 0, which has zero knowledge of the rebuild's actual table shapes. This task implements the exact same mechanism — temp-name `VACUUM INTO`, `PRAGMA integrity_check`, atomic `os.rename`, the `VITALFORGE_SKIP_MIGRATION_SNAPSHOT` escape hatch, the same error messages — but parameterized: the caller supplies the snapshot filename and the "does this DB need it" predicate. **Phase 1 is what calls this with `"fitness.pre-001-person-id.db"` and a real `_needs_person_id_rebuild` predicate.** This keeps Phase 0 self-contained and testable without any Phase 1 code existing yet, and is a strict generalization of the spec's sample — nothing about the mechanism changes.

- [ ] **Step 1: Write the failing tests**

```python
import os


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -k snapshot -v`
Expected: FAIL with `AttributeError: module 'shared.migrations' has no attribute 'ensure_pre_migration_snapshot'`.

- [ ] **Step 3: Add `ensure_pre_migration_snapshot()` to `shared/migrations.py`**

Add these imports at the top of `shared/migrations.py`:

```python
import os
from pathlib import Path
```

Add the function:

```python
async def ensure_pre_migration_snapshot(
    snapshot_name: str,
    needs_snapshot: Callable[[aiosqlite.Connection], Awaitable[bool]],
) -> None:
    """VACUUM INTO a temp name, verify it, then atomically rename into place.

    Generic and migration-agnostic: the caller supplies the snapshot's final
    filename and a predicate deciding whether this database actually needs
    one. See the multi-tenancy design spec section (c.7) for the specific
    001-person-id-rebuild snapshot this will be used for in a later phase.

    Never VACUUM INTO the fixed name directly and treat its refusal to
    overwrite as the idempotence guard -- a container killed mid-VACUUM
    would leave a PARTIAL file at that name, and the next boot would see
    exists() == True, skip the snapshot, and proceed to migrate with the
    operator believing a good backup exists when it does not.

    Temp-name + integrity_check + os.rename fixes this: the fixed name is
    only ever produced by a rename of a file that already passed
    integrity_check, so exists() on the fixed name really does mean "a good
    snapshot exists." os.rename is atomic within a filesystem, and both
    paths are on the same data volume by construction (DB_PATH.parent).
    """
    from shared.database import DB_PATH, get_db

    final = DB_PATH.parent / snapshot_name
    if final.exists():
        return  # only ever produced by the verified rename below

    if os.getenv("VITALFORGE_SKIP_MIGRATION_SNAPSHOT", "").strip() == "1":
        logger.warning(
            "VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1 -- skipping the pre-migration "
            "snapshot. This is a one-way door; take a volume-level backup first."
        )
        return

    db = await get_db()
    try:
        if not await needs_snapshot(db):
            return
        tmp = DB_PATH.parent / f"{snapshot_name}.partial"
        tmp.unlink(missing_ok=True)  # a previous kill can leave one; it is worthless
        try:
            await db.execute("VACUUM INTO ?", (str(tmp),))
        except Exception:
            # Scoped to the VACUUM only: a failure in needs_snapshot() is not
            # a disk-space problem and must not be reported as one.
            logger.error(
                "Pre-migration snapshot failed. The most likely cause is insufficient "
                "free space on the data volume: VACUUM INTO needs room for a full "
                "second copy of the database. Free space and restart, or -- after "
                "taking a volume-level backup by other means -- set "
                "VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1 to proceed without it. Until one "
                "of those happens this container will restart-loop "
                "(restart: unless-stopped), which is deliberate: migrating without a "
                "backup is worse."
            )
            raise
    finally:
        await db.close()

    check = await aiosqlite.connect(str(tmp))
    try:
        cur = await check.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
    finally:
        await check.close()
    if row is None or row[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Pre-migration snapshot failed integrity_check; refusing to migrate")

    os.rename(tmp, final)
    logger.warning("Pre-migration snapshot written and verified: %s", final)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py -k snapshot -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/migrations.py tests/test_migrations.py
git commit -m "feat: add ensure_pre_migration_snapshot(), a generic pre-migration backup utility"
```

---

### Task 7: `assert_schema_understood()` — the schema-version boot guard

**Files:**
- Modify: `shared/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `shared.database.get_db`.
- Produces: `shared.migrations.assert_schema_understood() -> None` (async, raises `RuntimeError`), `shared.migrations._KNOWN_MIGRATIONS: tuple[str, ...]`.

Per spec §i Q12 (decided: build this now). It protects every migration *after* 001, not 001 itself (the pre-001 image predates the guard by definition). Costs one table read per boot.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -k assert_schema -v`
Expected: FAIL with `AttributeError: module 'shared.migrations' has no attribute 'assert_schema_understood'`.

- [ ] **Step 3: Add `assert_schema_understood()` to `shared/migrations.py`**

```python
# Every marker name this image knows how to apply. These strings MUST match
# the names passed to run_migration() verbatim -- a typo here makes this
# image's own migration read as one from the future and boot-loops the
# container. "001-person-id-rebuild" does not exist as a real migration
# yet (that is Phase 1's job); it is declared here now, in Phase 0, so the
# guard ships ahead of the migration it will eventually recognize.
_KNOWN_MIGRATIONS = ("001-person-id-rebuild",)


async def assert_schema_understood() -> None:
    """Refuse to serve a database that is newer than this image understands.

    Called at the end of shared/database.py's init_db(), on its own
    connection, after any migrations have run -- so both services get it
    without either app.py changing.

    An applied marker whose name is not in _KNOWN_MIGRATIONS means some
    newer image migrated this file. This image would then read the result
    WITHOUT erroring and could return quietly wrong data. Fail the lifespan
    instead: a documented boot loop beats silently merging data across
    people (or any other future non-additive change) into the wrong shape.

    A fresh or pre-runner database has zero markers, which is an empty set
    and therefore passes. The guard only ever fires on names from the
    future -- it cannot protect against migration 001 itself, because any
    image that predates this guard predates its check.
    """
    db = await get_db()
    try:
        cur = await db.execute("SELECT name FROM schema_migrations")
        rows = await cur.fetchall()
    finally:
        await db.close()
    unknown = sorted({row[0] for row in rows} - set(_KNOWN_MIGRATIONS))
    if unknown:
        raise RuntimeError(
            f"Database has migrations this image does not know: {unknown}. "
            "Redeploy the newer image, or restore the pre-migration snapshot."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py -k assert_schema -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/migrations.py tests/test_migrations.py
git commit -m "feat: add assert_schema_understood(), a schema-version boot guard"
```

---

### Task 8: Wire the guard into `init_db()`

**Files:**
- Modify: `shared/database.py` (imports, and the end of `init_db()`, currently lines 297-302)
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `shared.migrations.SCHEMA_MIGRATIONS_TABLE_SQL`, `shared.migrations.assert_schema_understood`.
- Produces: `init_db()` now creates the `schema_migrations` table and calls the guard as its final step. No new public interface.

Per spec §c.4: the guard runs on its **own connection**, after `init_db()`'s main connection has already closed — matching the ordering `tests/test_migration_gating_assumptions.py`'s Task 2 tests prove is necessary. `run_migration()` and `ensure_pre_migration_snapshot()` are NOT called from `init_db()` in this task — that is Phase 1's job, once `_apply_person_id_rebuild` exists.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrations.py -k init_db -v`
Expected: first test fails (`schema_migrations` doesn't exist yet), third test fails (no `RuntimeError` raised, since nothing calls the guard yet).

- [ ] **Step 3: Update `shared/database.py`**

At the top of the file, add the import:

```python
from shared.migrations import SCHEMA_MIGRATIONS_TABLE_SQL, assert_schema_understood
```

Inside `init_db()`, before the final `await db.commit()` (i.e. as one more statement alongside the other `CREATE TABLE IF NOT EXISTS` calls, anywhere in that sequence — table creation order doesn't matter since none of these tables reference each other yet), add:

```python
        await db.execute(SCHEMA_MIGRATIONS_TABLE_SQL)
```

Then, after the existing `try`/`finally` block's `await db.close()` (i.e. genuinely after `init_db()`'s own connection is closed — do not add this inside the existing `try` block), add:

```python
    # Own connection, opened and closed after init_db()'s connection is
    # fully closed -- see shared/migrations.py's run_migration() docstring
    # and tests/test_migration_gating_assumptions.py for why that ordering
    # matters. No migrations are actually run here yet (that starts in a
    # later phase); this only refuses to serve a database migrated by a
    # newer image than this one.
    await assert_schema_understood()
```

So the tail of `init_db()` reads, in order: ... existing table creations ..., `await db.execute(SCHEMA_MIGRATIONS_TABLE_SQL)`, `await db.commit()`, `finally: await db.close()`, then (outside the function's existing try/finally, at the same indentation as the `try:`) `await assert_schema_understood()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrations.py -k init_db -v`
Expected: all 3 PASS.

- [ ] **Step 5: Run the full existing suite to check for regressions**

Run: `pytest -q`
Expected: all existing tests pass — `init_db()` is called by every test fixture in `tests/conftest.py`, so this is the broadest regression check in this plan.

- [ ] **Step 6: Commit**

```bash
git add shared/database.py tests/test_migrations.py
git commit -m "feat: wire the schema-version guard into init_db()"
```

---

### Task 9: Documentation corrections

**Files:**
- Modify: `docs/prp/00-design.md` (around line 1596-1598)
- Modify: `shared/database.py:8-19`

Per spec Appendix A: two corrections, both because this phase disproves something these files currently assert as fact. Not a TDD task (there is no code to test) — read, edit, verify by re-reading, commit.

- [ ] **Step 1: Fix `docs/prp/00-design.md`**

Find the paragraph (around line 1595-1599):

```
Residual risk, stated honestly: `ADD COLUMN` here is O(1) because none of the new
columns has a non-constant `DEFAULT`. Any future migration that adds a defaulted
column rewrites the table and reintroduces a real interruption window. That
constraint should be written into `shared/database.py` as a comment at
implementation time.
```

Replace the second sentence — imprecise per Appendix A ("that is imprecise: `shared/database.py:36-38` states the correct rule — a constant default is metadata-only and fast; a non-constant one is the rewrite case") — with:

```
Residual risk, stated honestly: `ADD COLUMN` here is O(1) because none of the new
columns has a non-constant `DEFAULT`. Any future migration that adds a column with
a **non-constant** default (e.g. a value computed at migration time, not a fixed
literal) rewrites the table and reintroduces a real interruption window; a
**constant** default (like `session_version INTEGER NOT NULL DEFAULT 1`, already
in production in `shared/database.py`) is metadata-only and carries none of that
risk. That distinction is written into `shared/database.py`'s comment, and,
starting with the multi-tenancy work, into `shared/migrations.py` for the cases
that genuinely need more than an additive column.
```

- [ ] **Step 2: Amend `shared/database.py`'s comment (lines 8-19)**

Find:

```python
# Additive columns for weight_log's body-composition intake (Track B). Every
# entry here must stay nullable with no non-constant DEFAULT -- a defaulted
# column rewrites the table and reintroduces a real interruption window for
# "container killed during first boot after upgrade" (see
# docs/prp/00-design.md SS5.4).
```

Replace with (per Appendix A: "amended rather than deleted: additive-only remains the default and the burden of proof stays on anything else, but the stated reason should be corrected to 'no runner needed, no rollback hazard' rather than 'rewrites are interruption-unsafe,' which measurement does not support"):

```python
# Additive columns for weight_log's body-composition intake (Track B). Every
# entry here must stay nullable with no non-constant DEFAULT -- not because a
# table rewrite is interruption-unsafe (it isn't: SQLite's CREATE/COPY/DROP/
# RENAME sequence rolls back cleanly inside BEGIN IMMEDIATE, verified in
# tests/test_migration_gating_assumptions.py), but because a constant-default
# ADD COLUMN needs no migration runner at all -- it's a fast, metadata-only
# change -- while a genuine schema change (e.g. a non-constant default, or
# changing a PRIMARY KEY) does, and belongs in shared/migrations.py instead
# of here. See docs/prp/00-design.md SS5.4 and
# docs/superpowers/specs/2026-08-25-family-multitenancy-design.md Appendix A
# for the full reasoning and the migration that first needed the runner.
```

- [ ] **Step 3: Verify by re-reading both files**

Read both changed sections back and confirm: neither still claims a table rewrite is inherently interruption-unsafe; both point at the actual current reasons (no-runner-needed-for-additive vs. correctness-not-safety for non-additive).

- [ ] **Step 4: Run ruff (comments don't affect it, but confirm the file still parses cleanly) and the full suite**

Run: `ruff check . && pytest -q`
Expected: `All checks passed!`, all tests pass (comment-only changes plus one prose doc change).

- [ ] **Step 5: Commit**

```bash
git add docs/prp/00-design.md shared/database.py
git commit -m "docs: correct the additive-only migration rationale (spec Appendix A)"
```

---

## Self-Review

**1. Spec coverage.** §c.3 (runner) → Task 3. §c.3's schema-version guard → Task 7. §c.4 (ordering, wiring) → Task 8. §c.7 mitigation 1 (snapshot) → Task 6. §c.7 mitigation 2 (guard, same as Task 7). §c.8's two gating tests → Tasks 1-2. The `get_db()` busy_timeout raise and `_add_columns` pre-check called out in §c.3 → Tasks 4-5. Appendix A's two doc corrections → Task 9. Appendix B's phase-0 file list (`shared/migrations.py`, `shared/database.py`, `tests/test_migrations.py`, `docs/prp/00-design.md`) → matches this plan's File Structure exactly, plus one file the spec's file list doesn't itemize separately (`tests/test_migration_gating_assumptions.py`) which this plan splits out of `test_migrations.py` for the stated readability reason. §c.8's remaining tests (fixture DB, schema parity, interruption on `_apply_person_id_rebuild`, cross-person isolation, weight dedup isolation, person_id-on-INSERT) are **Phase 1 tests**, not Phase 0 — they test code that does not exist until Phase 1 (`_apply_person_id_rebuild`, `_needs_person_id_rebuild`, the `persons`/`person_grants` tables) and are correctly out of scope here; they belong in Phase 1's own plan.

**2. Placeholder scan.** No "TBD"/"implement later"/"similar to Task N" found. Every code step contains complete, real code — either lifted near-verbatim from the spec (Tasks 1-2, 3, 7) or a deliberate, explicitly-justified generalization of the spec's sample (Task 6's parameterization).

**3. Type consistency.** `run_migration(name: str, apply: Callable[[aiosqlite.Connection], Awaitable[None]])` (Task 3) is called identically in Task 7's test and referenced identically in Task 8's comment. `ensure_pre_migration_snapshot(snapshot_name: str, needs_snapshot: Callable[[aiosqlite.Connection], Awaitable[bool]])` (Task 6) matches its test calls. `assert_schema_understood()` (Task 7) takes no arguments everywhere it's referenced, including Task 8's wiring. `SCHEMA_MIGRATIONS_TABLE_SQL` is defined once in Task 3 and reused (not redefined) in Task 8.

---

Plan complete and saved to `docs/superpowers/plans/2026-08-26-multitenancy-phase0-migration-runner.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
