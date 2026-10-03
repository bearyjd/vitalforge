"""Additive column DDL per table, applied by shared/database.py's init_db().

Pure data, split out of database.py to keep it under the repo's line ceiling.
Each list is handed to `_add_columns` (attempt-and-swallow, safe when both
services run init_db() at once). See the notes on each list for why it is
additive rather than a migration.
"""

# Additive columns for weight_log's body-composition intake (Track B). Every
# entry here must stay nullable with no non-constant DEFAULT -- not because a
# table rewrite is interruption-unsafe (it isn't: SQLite's CREATE/COPY/DROP/
# RENAME sequence rolls back cleanly inside BEGIN IMMEDIATE, verified in
# tests/test_migration_gating_assumptions.py), but because a constant-default
# ADD COLUMN needs no migration runner at all -- it's a fast, metadata-only
# change -- while a genuine schema change (e.g. a non-constant default, or
# changing a PRIMARY KEY) does, and belongs in shared/migrations.py instead
# of here. See docs/prp/00-design.md SS5.4 and
# docs/superpowers/specs/2026-08-25-family-multitenancy-design.md Appendix A
# for the full reasoning and the migration that first needed the runner.
WEIGHT_LOG_ADDITIVE_COLUMNS = [
    "body_fat_pct REAL",
    "body_water_pct REAL",
    "muscle_pct REAL",
    "bone_mass_kg REAL",
    "source TEXT",
    "person_id INTEGER",
    # Client-generated idempotency key (A6, docs/prp/00-design.md SS4.4 in the
    # Bascule repo). NULL for every pre-existing row and for any client that
    # still doesn't send one -- those fall back to the timestamp+weight-window
    # dedup below, unchanged. Enforced unique per person by
    # idx_weight_log_person_client_id (a partial index, so NULLs -- the
    # overwhelming majority of rows -- are excluded).
    "client_id TEXT",
    # Bascule's V2Shaper has sent these three since it was written; WeightIn
    # had nowhere to put them until now (extra="forbid" 422'd the whole
    # request the moment one was ever populated). bmr/amr are kcal/day.
    "bmi REAL",
    "bmr REAL",
    "amr REAL",
    # UTC ISO instant at which some request claimed the right to push this row
    # to Garmin, or NULL when no push is in flight. post_weight decides whether
    # to push INSIDE its BEGIN IMMEDIATE but performs the push after the commit
    # (the push is synchronous and must not be held across the write lock), and
    # it writes synced_to_garmin only once that push returns -- through two
    # awaits that yield. Without a claim, a concurrent identical retry resumes
    # in that gap, reads synced_to_garmin still 0, and files a SECOND weigh-in.
    # The claim is taken inside the same transaction that reads the row, so a
    # blocked request sees it the instant it can see the row. Cleared when the
    # outcome is recorded; a claim older than _GARMIN_CLAIM_TIMEOUT_SECONDS is
    # treated as stale (the claiming process died) so a crash mid-push cannot
    # strand a row unpushable forever. Additive, not a rebuild: weight_log is
    # deployed, keeps its own `id` primary key, and so cannot go in
    # migrations._REBUILD_TABLES.
    "garmin_claimed_at TEXT",
]

# Additive columns for weight_history's Garmin-sourced composition read path
# (B5). Unit-suffixed per docs/prp/00-design.md SS3.5/SS4.3 -- the B3 live
# checkpoint confirmed Garmin returns boneMass/muscleMass in grams, so these
# must be `_g`, not `_kg` (mixing this up with weight_log's `_kg` convention
# is exactly the silent lbs/kg-style bug the suffix exists to prevent).
WEIGHT_HISTORY_ADDITIVE_COLUMNS = [
    "body_water REAL",
    "bone_mass_g REAL",
    "muscle_mass_g REAL",
]

STRENGTH_SESSIONS_ADDITIVE_COLUMNS = [
    # See the column comment in the strength_sessions CREATE TABLE below.
    "garmin_name_prefix TEXT",
]

# Additive column for the users table -- incremented on password change so
# every previously-issued session cookie for that account (which embeds the
# version at issue time) stops validating immediately, instead of staying
# valid until its 30-day expiry regardless of the password change (fix-review
# finding). `DEFAULT 1` is a constant, not the non-constant-default case the
# comment above warns about -- SQLite's ALTER TABLE ADD COLUMN with a
# constant default is a fast, metadata-only change, not a table rewrite.
USERS_ADDITIVE_COLUMNS = [
    "session_version INTEGER NOT NULL DEFAULT 1",
    "default_person_id INTEGER",
]

# Consecutive Garmin 429s for a person (sync_status.backoff_streak): the
# exponent behind backoff_until. Nullable with no DEFAULT on purpose -- NULL
# reads as 0, so an existing row needs no backfill and the older image's
# four-column upserts keep working against the wider table. Added AFTER the
# migrations (see init_db), not with the other add-column calls: migration
# 001 drops and recreates sync_status with its original five columns.
SYNC_STATUS_ADDITIVE_COLUMNS = [
    "backoff_streak INTEGER",
]
