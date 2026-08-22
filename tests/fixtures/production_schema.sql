-- Schema dump from the live production fitness.db (host: knowledge, 192.168.1.21,
-- vitalforge_vitalforge-data Docker volume), captured 2026-08-22 via a read-only
-- sqlite3 connection (`file:...?mode=ro`) run inside the running weight-service
-- container. Structure only — no row data was read or copied, per CLAUDE.md.
--
-- SQLite version in production: 3.46.1.
--
-- Confirmed: matches shared/database.py's init_db() exactly — no drift between
-- code and the live schema at capture time. Row counts at capture time (for
-- sizing a representative fixture, not reproduced here):
--   active_calories: 277   body_battery: 277   hrv: 268   resting_hr: 277
--   sleep: 268             steps: 277          stress: 277
--   sync_status: 1         training_load: 227  vo2max: 237
--   weight_history: 34     weight_log: 17

CREATE TABLE active_calories (
                date TEXT PRIMARY KEY,
                value INTEGER
            );

CREATE TABLE body_battery (
                date TEXT PRIMARY KEY,
                charged INTEGER,
                drained INTEGER,
                highest INTEGER,
                lowest INTEGER
            );

CREATE TABLE hrv (
                date TEXT PRIMARY KEY,
                last_night_avg REAL,
                last_night_5min_high REAL,
                weekly_avg REAL,
                status TEXT
            );

CREATE TABLE resting_hr (
                date TEXT PRIMARY KEY,
                value INTEGER
            );

CREATE TABLE sleep (
                date TEXT PRIMARY KEY,
                duration_seconds INTEGER,
                deep_seconds INTEGER,
                light_seconds INTEGER,
                rem_seconds INTEGER,
                awake_seconds INTEGER,
                sleep_score INTEGER,
                avg_spo2 REAL,
                avg_respiration REAL
            );

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE steps (
                date TEXT PRIMARY KEY,
                value INTEGER
            );

CREATE TABLE stress (
                date TEXT PRIMARY KEY,
                avg_level INTEGER,
                max_level INTEGER,
                rest_duration INTEGER,
                low_duration INTEGER,
                medium_duration INTEGER,
                high_duration INTEGER
            );

CREATE TABLE sync_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sync_time TEXT,
                last_sync_result TEXT,
                last_sync_days INTEGER
            );

CREATE TABLE training_load (
                date TEXT PRIMARY KEY,
                acute_load REAL,
                chronic_load REAL,
                load_ratio REAL
            );

CREATE TABLE vo2max (
                date TEXT PRIMARY KEY,
                vo2max_value REAL,
                fitness_age INTEGER
            );

CREATE TABLE weight_history (
                date TEXT PRIMARY KEY,
                weight_grams INTEGER,
                bmi REAL,
                body_fat REAL
            );

CREATE TABLE weight_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                weight_lbs REAL NOT NULL,
                weight_kg REAL NOT NULL,
                weight_grams INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                synced_to_garmin INTEGER DEFAULT 0
            );
