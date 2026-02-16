import os
from pathlib import Path

import aiosqlite

DB_PATH = Path(os.getenv("DB_PATH", "/app/data/fitness.db"))


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
                synced_to_garmin INTEGER DEFAULT 0
            )
        """)

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
                body_fat REAL
            )
        """)

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

        await db.commit()
    finally:
        await db.close()
