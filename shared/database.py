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


async def get_db() -> aiosqlite.Connection:
    """Open a connection to the SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
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

        await db.commit()
    finally:
        await db.close()
