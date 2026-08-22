"""Helper script run in a subprocess by
test_database_locked_propagates_and_does_not_swallow (tests/test_migration.py).

Holds an exclusive lock via raw sqlite3, then calls the real
shared.database.init_db() and prints a single marker line with the outcome.
A background aiosqlite thread has been observed to leave the interpreter
unable to exit cleanly after this specific failure path -- reproducible
outside pytest too, with a bare `asyncio.run()` -- unrelated to whether
init_db() behaves correctly, which it does. The parent test kills this
subprocess once the marker line has been read, rather than waiting for a
natural exit.

Usage: python _migration_lock_check.py <db_path> <repo_root>
"""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

db_path = sys.argv[1]
os.environ["DB_PATH"] = db_path
sys.path.insert(0, sys.argv[2])

from shared import database  # noqa: E402

database.DB_PATH = Path(db_path)


async def main():
    lock_conn = sqlite3.connect(db_path, timeout=1)
    lock_conn.execute("CREATE TABLE IF NOT EXISTS placeholder (x INTEGER)")
    lock_conn.commit()
    lock_conn.execute("BEGIN EXCLUSIVE")
    try:
        try:
            await database.init_db()
            print("MARKER:unexpected-success", flush=True)
        except Exception as e:
            print(f"MARKER:{type(e).__module__}.{type(e).__name__}:{e}", flush=True)
    finally:
        lock_conn.rollback()
        lock_conn.close()


asyncio.run(main())
