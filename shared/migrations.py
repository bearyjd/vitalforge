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
