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
