import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "/app/data/fitness.db"))

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
_WEIGHT_LOG_ADDITIVE_COLUMNS = [
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
    "default_person_id INTEGER",
]


async def _add_columns(db, table: str, column_ddls: list[str]):
    """Attempt-and-swallow, not PRAGMA-table_info-then-act, for CORRECTNESS:
    both services run init_db() against the same file and docker-compose
    starts them together, so a pre-check used to DECIDE whether to add a
    column would be TOCTOU-racy -- both could observe "absent" and both then
    attempt the ADD COLUMN. Only the duplicate-column error is swallowed;
    `database is locked` must propagate so a container that cannot migrate
    fails its lifespan and is restarted rather than serving traffic against
    a half-migrated schema.

    The PRAGMA table_info read below is a LATENCY-ONLY pre-check, not a
    correctness pre-check: it is allowed to be wrong (e.g. under a genuine
    race, both callers can still see "absent" and both attempt the ALTER,
    which is exactly the attempt-and-swallow path this docstring's first
    paragraph describes). What it buys is that the common case -- a second
    service starting up after the first one already added every column --
    skips a lock wait on an ALTER TABLE that was only ever going to hit
    "duplicate column name" and be swallowed anyway.
    """
    for column_ddl in column_ddls:
        column_name = column_ddl.split()[0]
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column_name in existing:
            continue
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
    """Create all tables if they don't exist, then run any pending schema
    migrations."""
    from shared.migrations import (
        _PERSON_ID_REBUILD_SNAPSHOT_NAME,
        SCHEMA_MIGRATIONS_TABLE_SQL,
        _apply_activities_person_id,
        _apply_person_id_rebuild,
        _needs_person_id_rebuild,
        assert_schema_understood,
        ensure_pre_migration_snapshot,
        run_migration,
    )

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

        # Belt-and-suspenders: the request-path BEGIN IMMEDIATE transaction
        # (vitalforge-weight/app.py post_weight) already serializes the
        # check-then-insert on client_id, so this index isn't load-bearing for
        # the race -- it's a hard DB-level guarantee of the same invariant, in
        # case a future write path (a script, a different endpoint) ever
        # bypasses that transaction. Unguarded, like idx_weight_log_person_timestamp
        # below: client_id exists on every row by this point in this same
        # init_db() call, added by _add_columns above, not by the heavier
        # migrations.py runner (contrast the guarded activities index, whose
        # person_id column only migrations.py adds, later than this point).
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_log_person_client_id "
            "ON weight_log(person_id, client_id) WHERE client_id IS NOT NULL"
        )

        # Phase 2: metric tables — one per metric type, all keyed by date
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                duration_seconds INTEGER,
                deep_seconds INTEGER,
                light_seconds INTEGER,
                rem_seconds INTEGER,
                awake_seconds INTEGER,
                sleep_score INTEGER,
                avg_spo2 REAL,
                avg_respiration REAL,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS resting_hr (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                value INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS hrv (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                last_night_avg REAL,
                last_night_5min_high REAL,
                weekly_avg REAL,
                status TEXT,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS body_battery (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                charged INTEGER,
                drained INTEGER,
                highest INTEGER,
                lowest INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stress (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                avg_level INTEGER,
                max_level INTEGER,
                rest_duration INTEGER,
                low_duration INTEGER,
                medium_duration INTEGER,
                high_duration INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS vo2max (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                vo2max_value REAL,
                fitness_age INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS weight_history (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                weight_grams INTEGER,
                bmi REAL,
                body_fat REAL,
                body_water REAL,
                bone_mass_g REAL,
                muscle_mass_g REAL,
                PRIMARY KEY (person_id, date)
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
            CREATE TABLE IF NOT EXISTS persons (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                archived_at  TEXT,
                is_primary   INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_primary "
            "ON persons(is_primary) WHERE is_primary = 1"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS person_grants (
                person_id  INTEGER NOT NULL REFERENCES persons(id),
                user_id    INTEGER NOT NULL REFERENCES users(id),
                access     TEXT NOT NULL CHECK (access IN ('view', 'manage', 'own')),
                granted_at TEXT NOT NULL,
                granted_by INTEGER,
                PRIMARY KEY (person_id, user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_person_grants_user ON person_grants(user_id)")

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
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                acute_load REAL,
                chronic_load REAL,
                load_ratio REAL,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS steps (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                value INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_calories (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                value INTEGER,
                PRIMARY KEY (person_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                person_id      INTEGER PRIMARY KEY,
                last_sync_time TEXT,
                last_sync_result TEXT,
                last_sync_days INTEGER,
                backoff_until  TEXT
            )
        """)

        # Additive: the dedup lookup (docs/prp/00-design.md SS3.7) filters
        # weight_log by timestamp on every POST. The query uses a sargable
        # `timestamp >= ?` prefilter alongside the authoritative julianday()
        # bounds specifically so this index can prune the scan -- wrapping
        # the column in julianday() directly is not index-friendly.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp "
            "ON weight_log(person_id, timestamp)"
        )

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
                person_id INTEGER NOT NULL,
                start_time_utc TEXT NOT NULL,
                sport TEXT,
                duration_seconds INTEGER,
                distance_m REAL,
                calories INTEGER,
                avg_hr INTEGER,
                max_hr INTEGER,
                elevation_gain_m REAL,
                source_format TEXT NOT NULL CHECK (source_format IN ('fit')),
                file_sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                raw_summary_json TEXT,
                -- Scoped, not global: two people may legitimately import the
                -- same FIT file (same ride, same device). A global UNIQUE
                -- here would reject the second one with an IntegrityError
                -- instead of the clean duplicate response the route returns.
                UNIQUE (person_id, file_sha256)
            )
        """)

        # Supports the near-duplicate (start_time_utc, sport) lookup the
        # import route runs on every upload after the exact file-hash check.
        # Guarded, unlike every other CREATE INDEX here, because
        # activities.person_id does not exist yet on an UPGRADE at this
        # point: activities is re-keyed by migration 001, which runs after
        # this whole DDL block, so an unguarded CREATE INDEX would fail with
        # "no such column: person_id" and break the lifespan. On a fresh
        # database the column comes from the CREATE TABLE above and this is
        # what creates the index; on an upgrade _rebuild_activities creates
        # it as part of the rebuild. The read is a latency/ordering check,
        # not a correctness one -- IF NOT EXISTS still carries the race.
        cur = await db.execute("PRAGMA table_info(activities)")
        if any(row[1] == "person_id" for row in await cur.fetchall()):
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_person_start_time "
                "ON activities(person_id, start_time_utc)"
            )

        # Durable one-time markers for shared/migrations.py's run_migration().
        await db.execute(SCHEMA_MIGRATIONS_TABLE_SQL)

        await db.commit()
    finally:
        await db.close()          # <-- connection closed BEFORE anything below

    # BEFORE the migrations, not after: a database carrying a marker from a
    # newer image must be refused while this image has still changed nothing.
    # Running the guard afterwards would let a downgrade-boot write a
    # snapshot file and open a write transaction against a schema it has
    # already admitted it does not understand.
    await assert_schema_understood()

    await ensure_pre_migration_snapshot(_PERSON_ID_REBUILD_SNAPSHOT_NAME, _needs_person_id_rebuild)
    await run_migration("001-person-id-rebuild", _apply_person_id_rebuild)
    await run_migration("002-activities-person-id", _apply_activities_person_id)

    # After the migrations, because it needs the primary person 001 creates.
    await _attribute_orphaned_weight_log_rows()


async def _attribute_orphaned_weight_log_rows() -> None:
    """Give any weight_log row with a NULL person_id to the primary person.

    Migration 001 runs this same backfill, but its marker commits in the same
    transaction -- so a row written with a NULL person_id AFTER 001 commits is
    never repaired by the migration, which skips itself forever. Nothing else
    repairs it either: weight_log.person_id cannot be NOT NULL (SQLite cannot
    add a NOT NULL column without a constant default, which is why it is an
    additive column rather than a rebuild), so the schema will not refuse such
    a row on the way in.

    That row is reachable by doing what README's Upgrading step 1 forbids:
    leaving an old weight-service container running against the newly rebuilt
    schema. Its INSERT predates person_id and simply omits it. Every read path
    now filters `person_id = ?`, so the result is worse than mis-attribution
    -- the entry is invisible in /recent, /trend and DELETE, and the user sees
    a weight they logged silently missing rather than merely misfiled.

    Running the backfill on every boot makes that self-healing instead of
    permanent. It is idempotent and matches zero rows on a healthy database.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE weight_log SET person_id = (SELECT id FROM persons WHERE is_primary = 1) "
            "WHERE person_id IS NULL"
        )
        await db.commit()
    finally:
        await db.close()
    if cursor.rowcount:
        # warning, not info: reaching this means an unsupported upgrade
        # happened and the operator should know their data was repaired.
        logger.warning(
            "Attributed %d unattributed weight_log row(s) to the primary person. "
            "This means a pre-multi-tenancy weight service wrote to this database "
            "after the person-id migration -- see README's Upgrading section.",
            cursor.rowcount,
        )


async def get_primary_person_id() -> int:
    """Return the id of the durable primary person (persons.is_primary = 1).

    Phase 1 has no per-request identity yet -- every route and background
    task resolves "the" person through this helper until Phase 2's
    require_person dependency exists. Each call site is deliberately
    explicit (see this plan's Global Constraints) so Phase 2 can replace
    them one at a time.
    """
    db = await get_db()
    try:
        cur = await db.execute("SELECT id FROM persons WHERE is_primary = 1")
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        raise RuntimeError("No primary person found -- has init_db() run?")
    return row["id"]


async def garmin_credential_person_id() -> int:
    """The person the deployment's single Garmin account actually describes.

    shared/garmin_client.py holds ONE module-level client authenticated from
    the deployment-wide GARMIN_EMAIL/GARMIN_PASSWORD. Whatever it returns is
    that one human's sleep, HRV and heart rate, no matter which person_id the
    caller asks to write it under. Until Phase 3 gives each person their own
    credentials and token store, that human is the primary person.

    require_person() authorizes a caller FOR A TARGET PERSON. It cannot
    authorize them for a DATA SOURCE, and nothing else did either -- so a
    caller holding `manage` on their own person could trigger a pull that
    wrote the primary person's Garmin data under theirs, then read it back.
    Every SQL statement involved was correctly person-scoped; the source was
    not. Callers that pull from Garmin must compare their target against this.

    PHASE 3 REPLACES THIS, and the RETURN TYPE changes with it. Today this
    delegates to get_primary_person_id(), which RAISES when no primary row
    exists -- acceptable because that state means init_db() has not completed,
    so it is noise rather than a case. Once garmin_links exists, "this person
    has no linked account" becomes a normal, expected answer, and the
    signature should become `int | None` with callers handling None rather
    than an exception escaping onto the request path as a 500.
    """
    return await get_primary_person_id()


async def _grant_primary_person_to_first_admin(db, person_id: int) -> None:
    """Give the first admin an 'own' grant on `person_id` and make it their
    default. Runs on the caller's connection and does not commit.

    Idempotent by constraint rather than by pre-check: person_grants'
    PRIMARY KEY (person_id, user_id) turns a re-run into a no-op INSERT, and
    the UPDATE is guarded on `default_person_id IS NULL` so it can never
    overwrite a default chosen later. Both services run this concurrently at
    startup with no ordering between them -- the same race, answered the
    same constraint-based way, as bootstrap_first_admin().

    No-ops while no admin exists: a fresh install with VITALFORGE_PASS unset
    has an empty users table, and the grant lands on the first boot after an
    admin is finally seeded.
    """
    admin = await (await db.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).fetchone()
    if admin is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO person_grants (person_id, user_id, access, granted_at) "
        "VALUES (?, ?, 'own', ?)",
        (person_id, admin["id"], now),
    )
    await db.execute(
        "UPDATE users SET default_person_id = ? WHERE id = ? AND default_person_id IS NULL",
        (person_id, admin["id"]),
    )


async def ensure_primary_person_grant() -> None:
    """Make sure the primary person actually has an owner.

    init_db() runs migration 001 before either service's lifespan reaches
    bootstrap_first_admin(), so on a FRESH database the users table is still
    empty when migrations._ensure_primary_person() runs: it creates the
    person, finds no admin to grant it to, and the migration marker it
    commits in the same transaction means it never runs again. Upgraded
    databases never hit this -- their users table is already populated -- so
    without this call a fresh install would be the only kind of deployment
    whose admin owns nothing, permanently, and Phase 2's require_person
    would inherit that.

    Called from both services' lifespans AFTER bootstrap_first_admin(), and
    safe to call on every boot.
    """
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await _grant_primary_person_to_first_admin(db, person_id)
        await db.commit()
    finally:
        await db.close()
