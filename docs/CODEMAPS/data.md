<!-- Generated: 2026-08-22 | Files scanned: 22 | Token estimate: ~650 -->
# Data

Single SQLite file (`DB_PATH`, default `/app/data/fitness.db`, env-overridable), shared
by both services via a Docker named volume. Schema is `CREATE TABLE IF NOT EXISTS` only —
**no migration tool, no version tracking, no migration history.** All schema changes must
be additive/backward-compatible edits to `shared/database.py::init_db()`.

## Tables (`shared/database.py`)

| Table | Key | Notes |
|---|---|---|
| `weight_log` | `id` (autoincrement) | written by weight service (`POST /api/weight`); one row per manual entry, includes `synced_to_garmin` flag |
| `sleep` | `date` (PK) | duration/deep/light/rem/awake seconds, sleep_score, spo2, respiration |
| `resting_hr` | `date` (PK) | single `value` column |
| `hrv` | `date` (PK) | last_night_avg, last_night_5min_high, weekly_avg, status |
| `body_battery` | `date` (PK) | charged, drained, highest, lowest |
| `stress` | `date` (PK) | avg/max level + rest/low/medium/high duration buckets |
| `vo2max` | `date` (PK) | vo2max_value, fitness_age |
| `weight_history` | `date` (PK) | weight_grams, bmi, body_fat — **from Garmin**, distinct from `weight_log` (manual entries) |
| `training_load` | `date` (PK) | acute_load, chronic_load, load_ratio |
| `steps` | `date` (PK) | single `value` column |
| `active_calories` | `date` (PK) | single `value` column |
| `sync_status` | `id` (CHECK id=1, singleton row) | last_sync_time, last_sync_result, last_sync_days |

## Relationships

No foreign keys — all 9 metric tables are independent, keyed by `date` (`YYYY-MM-DD`
string), populated by `sync.py::upsert()` via `INSERT OR REPLACE`. `weight_log` (manual,
autoincrement PK, ISO timestamp) and `weight_history` (Garmin-sourced, date PK) are two
separate sources of weight data — do not conflate when querying.

## Write paths

- `shared/database.py::init_db()` — schema creation only, called from both services'
  lifespan startup.
- `vitalforge-weight/app.py` — direct SQL against `weight_log` only.
- `vitalforge-dashboard/sync.py::upsert()` — generic `INSERT OR REPLACE INTO [table]`
  helper used for all 9 metric tables + `weight_history`; also writes `sync_status`.
- `scripts/seed_db.py` — synthetic data generator for local testing without Garmin
  (writes the same 9 tables + `weight_history` via ad hoc SQL, mirroring `upsert()`'s
  shape). Refuses to target any path named `fitness.db` or containing `.garth`.

## Test isolation

`tests/conftest.py::tmp_db_path` monkeypatches `shared.database.DB_PATH` to a
`tmp_path` file per test — `get_db()` re-reads the module-level global on every call, so
this fully isolates tests from `/app/data/fitness.db`.
