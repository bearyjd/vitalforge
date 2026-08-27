import os
from pathlib import Path

import aiosqlite

DB_PATH = Path(os.getenv("DB_PATH", "/app/data/fitness.db"))

# Additive columns for weight_log's body-composition intake (Track B). Every
# entry here must stay nullable with no non-constant DEFAULT -- a defaulted
# column rewrites the table and reintroduces a real interruption window for
# "container killed during first boot after upgrade" (see
# docs/prp/00-design.md SS5.4).
_WEIGHT_LOG_ADDITIVE_COLUMNS = [
    "body_fat_pct REAL",
    "body_water_pct REAL",
    "muscle_pct REAL",
    "bone_mass_kg REAL",
    "source TEXT",
]

# Additive columns for weight_history's Garmin-sourced composition read path
# (B5). Unit-suffixed per docs/prp/00-design.md SS3.5/SS4.3 -- the B3 live
# checkpoint confirmed Garmin returns boneMass/muscleMass in grams, so these
# must be `_g`, not `_kg` (mixing this up with weight_log's `_kg` convention
# is exactly the silent lbs/kg-style bug the suffix exists to prevent).
_WEIGHT_HISTORY_ADDITIVE_COLUMNS = [
    "body_water REAL",
    "bone_mass_g REAL",
    "muscle_mass_g REAL",
]

# Additive column for the users table -- incremented on password change so
# every previously-issued session cookie for that account (which embeds the
# version at issue time) stops validating immediately, instead of staying
# valid until its 30-day expiry regardless of the password change (fix-review
# finding). `DEFAULT 1` is a constant, not the non-constant-default case the
# comment above warns about -- SQLite's ALTER TABLE ADD COLUMN with a
# constant default is a fast, metadata-only change, not a table rewrite.
_USERS_ADDITIVE_COLUMNS = [
    "session_version INTEGER NOT NULL DEFAULT 1",
]


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


async def init_db():
    """Create all tables if they don't exist."""
    db = await get_db()
    try:
        # Phase 1: weight log
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weight_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                weight_lbs REAL NOT NULL,
                weight_kg REAL NOT NULL,
                weight_grams INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                synced_to_garmin INTEGER DEFAULT 0,
                body_fat_pct REAL,
                body_water_pct REAL,
                muscle_pct REAL,
                bone_mass_kg REAL,
                source TEXT
            )
        """)

        # Additive migration for weight_log on databases that already exist
        # (a fresh DB already has these columns from the CREATE TABLE above).
        await _add_columns(db, "weight_log", _WEIGHT_LOG_ADDITIVE_COLUMNS)

        # Phase 2: metric tables — one per metric type, all keyed by date
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep (
                date TEXT PRIMARY KEY,
                duration_seconds INTEGER,
                deep_seconds INTEGER,
                light_seconds INTEGER,
                rem_seconds INTEGER,
                awake_seconds INTEGER,
                sleep_score INTEGER,
                avg_spo2 REAL,
                avg_respiration REAL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS resting_hr (
                date TEXT PRIMARY KEY,
                value INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS hrv (
                date TEXT PRIMARY KEY,
                last_night_avg REAL,
                last_night_5min_high REAL,
                weekly_avg REAL,
                status TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS body_battery (
                date TEXT PRIMARY KEY,
                charged INTEGER,
                drained INTEGER,
                highest INTEGER,
                lowest INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stress (
                date TEXT PRIMARY KEY,
                avg_level INTEGER,
                max_level INTEGER,
                rest_duration INTEGER,
                low_duration INTEGER,
                medium_duration INTEGER,
                high_duration INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS vo2max (
                date TEXT PRIMARY KEY,
                vo2max_value REAL,
                fitness_age INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS weight_history (
                date TEXT PRIMARY KEY,
                weight_grams INTEGER,
                bmi REAL,
                body_fat REAL,
                body_water REAL,
                bone_mass_g REAL,
                muscle_mass_g REAL
            )
        """)

        # Additive migration for weight_history on databases that already
        # exist (a fresh DB already has these columns from the CREATE TABLE
        # above).
        await _add_columns(db, "weight_history", _WEIGHT_HISTORY_ADDITIVE_COLUMNS)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                created_at TEXT NOT NULL,
                session_version INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Additive migration for users on databases that already have the
        # table from an earlier commit of this same branch (a fresh DB
        # already has the column from the CREATE TABLE above).
        await _add_columns(db, "users", _USERS_ADDITIVE_COLUMNS)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                label TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id)")

        # Durable one-time markers for auth data migrations. A marker is
        # separate from the migrated token row so revoking that token cannot
        # cause a still-present legacy env var to resurrect it on restart.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_migrations (
                name TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            )
        """)

        # Per-user goal / target tracking (vitalforge-dashboard/goals.py).
        # `metric` is validated against METRIC_TABLES.keys() at the API
        # layer, not with a CHECK constraint here -- a DB-level enum would
        # be a second place that list can drift out of sync with the one in
        # vitalforge-dashboard/app.py.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                metric TEXT NOT NULL,
                target_value REAL NOT NULL,
                target_date TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_goals_user_id ON goals(user_id)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS training_load (
                date TEXT PRIMARY KEY,
                acute_load REAL,
                chronic_load REAL,
                load_ratio REAL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS steps (
                date TEXT PRIMARY KEY,
                value INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_calories (
                date TEXT PRIMARY KEY,
                value INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sync_time TEXT,
                last_sync_result TEXT,
                last_sync_days INTEGER
            )
        """)

        # Additive: the dedup lookup (docs/prp/00-design.md SS3.7) filters
        # weight_log by timestamp on every POST. The query uses a sargable
        # `timestamp >= ?` prefilter alongside the authoritative julianday()
        # bounds specifically so this index can prune the scan -- wrapping
        # the column in julianday() directly is not index-friendly.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_weight_log_timestamp ON weight_log(timestamp)")

        # FIT-file activity import (dashboard-only, first slice: FIT only --
        # TCX/GPX deferred). Deliberately its own table rather than reusing
        # any METRIC_TABLES table: sync.py's upsert() does
        # `INSERT OR REPLACE ... WHERE date = ?` and scheduled_sync() re-pulls
        # a 90-day Garmin backfill on every boot plus every
        # SYNC_INTERVAL_HOURS, so anything written into an existing metric
        # table would be silently overwritten within hours. `file_sha256` is
        # UNIQUE so the exact-duplicate check in the import route is
        # enforced by the schema itself, not just application logic.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activities (
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
        """)

        # Supports the near-duplicate (start_time_utc, sport) lookup the
        # import route runs on every upload after the exact file-hash check.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_activities_start_time ON activities(start_time_utc)")

        await db.commit()
    finally:
        await db.close()
