"""SQLite connection primitives shared by shared.database and shared.migrations.

Neither of those modules imports the other: both depend on this one.
"""

from pathlib import Path

import aiosqlite

from shared.env_paths import path_from_env

DB_PATH = Path(path_from_env("DB_PATH", "/app/data/fitness.db"))


async def get_db(isolation_level: str | None = "") -> aiosqlite.Connection:
    """Open a connection to the SQLite database.

    isolation_level defaults to "" (aiosqlite/sqlite3's own legacy default),
    so every existing caller's behavior is unchanged. Pass None for
    autocommit mode with explicit BEGIN/COMMIT/ROLLBACK control -- e.g.
    shared/migrations.py's run_migration(), which issues DDL inside a
    transaction it must be able to roll back as a unit. isolation_level is
    set here, at connect() time -- setting it as a post-connect attribute
    on the returned connection instead raises a cross-thread
    ProgrammingError under aiosqlite (verified directly; this is a genuine
    aiosqlite API constraint, not an artifact of any particular caller's
    async setup).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH), isolation_level=isolation_level)
    db.row_factory = aiosqlite.Row
    # 30s, not aiosqlite's 5s default: a migration (shared/migrations.py's
    # run_migration) can legitimately hold the write lock longer than 5s on
    # a database with years of history, and every connection that might
    # race it -- not just the migration's own -- needs to wait that out
    # rather than surface "database is locked" as a request-path 500 or a
    # boot-loop in the other service. See the multi-tenancy design spec's
    # section (c) for the full reasoning.
    #
    # This must precede journal_mode=WAL: enabling WAL can itself need an
    # exclusive lock when both containers cold-start against the same file.
    # Setting it afterward leaves that first lock-taking operation at
    # sqlite's short default timeout.
    # Both PRAGMAs return a row.  Close their cursors before handing the
    # connection to a caller: leaving journal_mode's result pending keeps a
    # schema statement active, so an immediate DROP/ALTER on this same
    # connection fails with "database table is locked".
    cursor = await db.execute("PRAGMA busy_timeout = 30000")
    await cursor.close()
    cursor = await db.execute("PRAGMA journal_mode=WAL")
    await cursor.close()
    return db
