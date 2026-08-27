"""Migration runner and schema-version guard for VitalForge.

See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md section
(c) for the full design rationale -- this module implements it close to
verbatim; deviations from the spec's code samples are noted inline.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiosqlite

from shared.auth import _RESERVED_SLUGS, _slugify
from shared.database import get_db

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        name         TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL
    )
"""

# Every marker name this image knows how to apply. These strings MUST match
# the names passed to run_migration() verbatim -- a typo here makes this
# image's own migration read as one from the future and boot-loops the
# container. "001-person-id-rebuild" does not exist as a real migration
# yet (that is Phase 1's job); it is declared here now, in Phase 0, so the
# guard ships ahead of the migration it will eventually recognize.
_KNOWN_MIGRATIONS = ("001-person-id-rebuild",)

_PERSON_ID_REBUILD_SNAPSHOT_NAME = "fitness.pre-001-person-id.db"

# Table NAMES only -- no column DDL. _rebuild_columns derives the actual
# column list from the live schema (PRAGMA table_info) instead, so there is
# no second copy of any table's shape to drift out of sync with
# shared/database.py. See CLAUDE.md's METRIC_TABLES convention note: a new
# metric table created before a future rebuild must be added HERE by name.
_REBUILD_TABLES = [
    "sleep", "resting_hr", "hrv", "body_battery", "stress",
    "vo2max", "weight_history", "training_load", "steps", "active_calories",
]


def now_iso() -> str:
    # datetime/timezone are already imported at the top of this module.
    return datetime.now(timezone.utc).isoformat()


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info([{table}])")
    return any(row["name"] == column for row in await cur.fetchall())


async def _needs_person_id_rebuild(db) -> bool:
    """Cheap, TOCTOU-racy-by-design pre-check (spec §c.7): the only cost of
    losing this race is a wasted snapshot, because correctness comes
    entirely from the marker check inside run_migration's transaction."""
    return not await _has_column(db, "sleep", "person_id")


async def _first_admin_username(db) -> str | None:
    cur = await db.execute("SELECT username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    row = await cur.fetchone()
    return row["username"] if row else None


async def _ensure_primary_person(db) -> int:
    """Create (or return) the person that owns all pre-multi-tenancy data.

    Idempotent: called on every migration run, including the fresh-DB path
    where no rebuild follows. Runs inside the migration transaction, so the
    check-then-insert is not racy.
    """
    # `os` is already imported at the top of this module (used by
    # ensure_pre_migration_snapshot) -- no new import needed here.
    existing = await (await db.execute(
        "SELECT id FROM persons WHERE is_primary = 1"
    )).fetchone()
    if existing is not None:
        return existing["id"]

    any_person = await (await db.execute("SELECT COUNT(*) FROM persons")).fetchone()
    if any_person[0] != 0:
        raise RuntimeError("persons rows exist but none is_primary; refusing to guess")

    raw = os.environ.get("VITALFORGE_PRIMARY_PERSON", "").strip() \
        or await _first_admin_username(db) or "primary"
    slug = _slugify(raw)
    if not slug or slug in _RESERVED_SLUGS:
        logger.warning("Primary person slug %r is unusable; falling back to 'primary'", raw)
        slug = "primary"
    cursor = await db.execute(
        "INSERT INTO persons (slug, display_name, created_at, is_primary) "
        "VALUES (?, ?, ?, 1)",
        (slug, raw or slug, now_iso()),
    )
    person_id = cursor.lastrowid
    admin = await (await db.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).fetchone()
    if admin is not None:
        await db.execute(
            "INSERT INTO person_grants (person_id, user_id, access, granted_at) "
            "VALUES (?, ?, 'own', ?)",
            (person_id, admin["id"], now_iso()),
        )
        await db.execute(
            "UPDATE users SET default_person_id = ? WHERE id = ?",
            (person_id, admin["id"]),
        )
    return person_id


async def _rebuild_columns(db, table: str) -> list[tuple[str, str]]:
    """Return [(name, declared_type)] for every non-`date` column of `table`,
    read from the live schema. Fails loud on any shape this migration cannot
    faithfully reproduce."""
    rows = await (await db.execute(f"PRAGMA table_info([{table}])")).fetchall()
    columns: list[tuple[str, str]] = []
    for r in rows:
        if r["name"] == "date":
            if r["pk"] != 1:
                raise RuntimeError(f"{table}.date is not the primary key; refusing to rebuild")
            continue
        if r["notnull"] or r["dflt_value"] is not None or r["pk"]:
            raise RuntimeError(
                f"{table}.{r['name']} carries NOT NULL/DEFAULT/PK, which this migration "
                f"does not know how to reproduce -- update _apply_person_id_rebuild"
            )
        columns.append((r["name"], r["type"]))
    if not columns:
        raise RuntimeError(f"{table} has no non-date columns; refusing to rebuild")
    return columns


async def _rebuild_sync_status(db, person_id: int) -> None:
    await db.execute("""
        CREATE TABLE [sync_status__new] (
            person_id        INTEGER PRIMARY KEY,
            last_sync_time   TEXT,
            last_sync_result TEXT,
            last_sync_days   INTEGER,
            backoff_until    TEXT
        )
    """)
    await db.execute(
        "INSERT INTO [sync_status__new] "
        "(person_id, last_sync_time, last_sync_result, last_sync_days, backoff_until) "
        "SELECT ?, last_sync_time, last_sync_result, last_sync_days, NULL FROM sync_status",
        (person_id,),
    )
    await db.execute("DROP TABLE sync_status")
    await db.execute("ALTER TABLE [sync_status__new] RENAME TO sync_status")


async def _apply_person_id_rebuild(db) -> None:
    # Runs inside BEGIN IMMEDIATE (run_migration opens it). Any exception
    # rolls back the ENTIRE rebuild -- all 11 tables plus the marker --
    # leaving the original schema untouched. weight_log.person_id was added
    # and COMMITTED by _add_columns at init_db's step 2, OUTSIDE this
    # transaction -- see spec §c.6 for why that is correct and safe.
    person_id = await _ensure_primary_person(db)

    if await _has_column(db, "sleep", "person_id"):
        return  # fresh DB: tables already correctly shaped by init_db's DDL step.

    for table in _REBUILD_TABLES:
        columns = await _rebuild_columns(db, table)
        col_names = ", ".join(f"[{name}]" for name, _ in columns)
        col_ddl = ", ".join(f"[{name}] {type_}" for name, type_ in columns)
        await db.execute(f"""
            CREATE TABLE [{table}__new] (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                {col_ddl},
                PRIMARY KEY (person_id, date)
            )
        """)
        await db.execute(
            f"INSERT INTO [{table}__new] (person_id, date, {col_names}) "
            f"SELECT ?, date, {col_names} FROM [{table}]",
            (person_id,),
        )
        await db.execute(f"DROP TABLE [{table}]")
        await db.execute(f"ALTER TABLE [{table}__new] RENAME TO [{table}]")

    await _rebuild_sync_status(db, person_id)

    await db.execute("UPDATE weight_log SET person_id = ? WHERE person_id IS NULL", (person_id,))
    await db.execute("DROP INDEX IF EXISTS idx_weight_log_timestamp")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp "
        "ON weight_log(person_id, timestamp)"
    )


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
    # isolation_level=None (autocommit + explicit BEGIN IMMEDIATE), not
    # get_db()'s default legacy isolation_level (""), because this is the
    # mode tests/test_migration_gating_assumptions.py actually verified DDL
    # rollback under. Do not remove this argument on the grounds that "the
    # rest of the codebase doesn't set it": the rest of the codebase only
    # runs DML inside its explicit transactions, never DDL.
    db = await get_db(isolation_level=None)
    try:
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
            # shared/auth.py's bootstrap_migrated_token() is the closest
            # structural model this generalizes: it does an explicit
            # `await db.rollback()` on each known early-return branch inside
            # its own BEGIN IMMEDIATE block, then relies on `finally: await
            # db.close()` to discard the transaction on anything unexpected
            # -- it has no except clause at all. This function adds one:
            # BaseException (not just Exception), so that a cancelled
            # lifespan (asyncio.CancelledError) also gets an explicit
            # rollback here rather than relying on the implicit discard from
            # closing the connection in `finally` below.
            await db.rollback()
            raise
        logger.warning("Applied schema migration %s in %.2fs", name, time.monotonic() - started)
    finally:
        await db.close()


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
    # local, so DB_PATH is resolved per-call -- a module-top binding would
    # freeze the value at import and defeat DB_PATH overrides in tests.
    from shared.database import DB_PATH

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
        # get_db()'s own PRAGMA journal_mode/busy_timeout calls (and any
        # SELECT needs_snapshot() may have issued) leave unfetched cursors
        # open on this connection; sqlite refuses VACUUM while ANY statement
        # on the connection is unfinalized ("cannot VACUUM - SQL statements
        # in progress"), which sqlite3/aiosqlite otherwise never surfaces
        # since journal_mode/busy_timeout results are simply never read. A
        # bare commit() (no writes pending -- nothing above wrote anything)
        # resets/finalizes those statements without touching schema or data.
        # Verified empirically: VACUUM INTO fails 100% of the time on a
        # freshly opened get_db() connection without this.
        await db.commit()
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

    try:
        check = await aiosqlite.connect(str(tmp))
        try:
            cur = await check.execute("PRAGMA integrity_check")
            row = await cur.fetchone()
        finally:
            await check.close()
        ok = row is not None and row[0] == "ok"
    except aiosqlite.DatabaseError as exc:
        # The tmp file cannot be read or verified as a SQLite database at
        # all (e.g. "file is not a database") -- PRAGMA integrity_check
        # raises from inside the connection rather than returning a not-ok
        # row, so without this the `if not ok` branch below would never run
        # and the operator would see a raw DatabaseError instead of the
        # actionable message. Same failure mode as an explicit
        # integrity_check failure, so route it into the same
        # cleanup-and-raise path -- but log the original exception first,
        # since its message (e.g. "file is not a database" vs "database
        # disk image is malformed") is the actual diagnostic detail.
        logger.error("Pre-migration snapshot at %s is not a readable SQLite database", tmp, exc_info=exc)
        ok = False
    if not ok:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Pre-migration snapshot failed integrity_check; refusing to migrate")

    os.rename(tmp, final)
    logger.warning("Pre-migration snapshot written and verified: %s", final)
