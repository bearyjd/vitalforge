# Multi-Tenancy Phase 1 — Person/Grant Schema Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the `persons`/`person_grants` data model and rebuild all 11 date/singleton-keyed tables plus `weight_log` around `person_id`, using the Phase 0 migration runner, with **user-visible behavior identical to today** — exactly one person exists after this phase, every existing query returns exactly what it returned before.

**Architecture:** `shared/database.py` gains the new-shape DDL for `persons`, `person_grants`, the rebuilt tables, and two additive columns (`users.default_person_id`, `weight_log.person_id`); `init_db()` is restructured to close its own connection before invoking the Phase 0 runner (`shared/migrations.py`) for a new one-shot migration, `001-person-id-rebuild`. `shared/migrations.py` gains the migration's `apply` function plus every table-name-and-shape-derived helper the spec requires so there is no second, driftable copy of any column list. Every code path that reads or writes a rebuilt table — `sync.py`, `recommendations.py`, `goals.py`, both `app.py` files — is threaded with an explicit `person_id` parameter (no defaults, per the spec's Group-D rule), resolved for now via a single new helper, `get_primary_person_id()`, that both services call at the top of every affected route or background task. Phase 2 replaces that helper's call sites one at a time with real per-request identity; this phase does not touch routing, auth, or Garmin credentials at all.

**Tech Stack:** Python 3.12+, `aiosqlite`, `pytest` + `pytest-asyncio`, the existing `tests/fixtures/production_schema.sql` + `production_schema_db` fixture (already captures the exact pre-migration production schema and row counts for `weight_log`/`weight_history` — this plan extends it, it does not replace it).

**Spec:** `docs/superpowers/specs/2026-08-25-family-multitenancy-design.md` — this plan implements §(a) (data model), §(b) (exact schema changes), §(c.4)–(c.8) (where the migration fits, the apply function, testing), §(f.4) (slug validation, needed here because `_ensure_primary_person` depends on it), §(g.1) (the primary-person bootstrap), and §0.2's 13-statement-plus-signature-chain audit. §(h)'s Phase 1 paragraph and Appendix B's phase-1 row are the authoritative scope statement; read them before starting. Phase 0 (the generic runner, `assert_schema_understood`, `_KNOWN_MIGRATIONS = ("001-person-id-rebuild",)`) is already merged (PR #31) — this plan is the first thing that actually calls `run_migration` with that name.

## Global Constraints

- **User-visible behavior is identical after this phase.** One person exists (`is_primary = 1`); every metric, weight, recommendation, and correlation the dashboard/weight app show today must show exactly the same data after this migration runs. Any task whose tests can't confirm that has not implemented this phase correctly.
- **No route, auth, or Garmin-credential changes in this phase.** `/p/{slug}/` routing, grant management, and `require_person` are Phase 2. `garmin_links`/per-person credentials are Phase 3. Do not create those tables or touch `shared/garmin_client.py`.
- **No function threaded with `person_id` may give it a default value.** This is spec §0.2 Group D's rule, extended in this plan to every read path it audits plus two it didn't catch (`api_correlations` and `goals.py`'s `compute_progress` — both resolve to the same rebuilt tables via `METRIC_TABLES` and would otherwise merge all persons' data once Phase 2 admits a second one). A default silently turns a wrong-person bug into a one-line typo instead of an import-time/call-time error.
- **`get_primary_person_id()` (new, in `shared/database.py`) is a deliberate, temporary shim.** Every route/background task that needs "the current person" in this phase calls it explicitly at its own entry point — never buried inside a shared helper several calls deep — so each call site is a visible, greppable line Phase 2 replaces with real identity resolution. Do not thread a `person_id: int | None = None` "resolve if missing" convenience anywhere; that reintroduces exactly the implicit-default risk the rule above forbids.
- **Derive rebuild column lists from the live schema, never restate them.** `_rebuild_columns` (spec §c.5) reads `PRAGMA table_info` inside the migration's `BEGIN IMMEDIATE` lock — do not create a second `_REBUILD_TABLES`-style dict of columns. `_REBUILD_TABLES` itself holds table **names** only.
- **One transaction for the whole rebuild** (spec §c.6): all 11 tables plus the `schema_migrations` marker commit or roll back together. `weight_log.person_id`'s `ALTER TABLE` is a separate, independently-committing step at `init_db()`'s step 2 — see spec §c.6's atomicity scoping before writing the interruption test.
- **The pre-migration snapshot name is fixed, not timestamped:** `fitness.pre-001-person-id.db`. Treat any file with that name, or a `.partial` sibling, as containing real personal health data per `CLAUDE.md`'s PRIVACY block — never read, log, or print its contents in code or in test assertions beyond `PRAGMA integrity_check`.
- Run `ruff check .` and the full non-playwright `pytest -q` after every task, not just at the end. Every existing test that calls a function this plan re-signatures must be updated in the same task — `pytest -q` must show **zero** failures, not just zero failures in newly-added tests.
- **Baseline, confirmed before Task 1:** `pytest tests/test_migration_gating_assumptions.py -v` (4 passed) and the full suite `pytest -q` (438 passed, 4 deselected for `-m playwright`) are green as of this plan's writing. `tests/test_fit_import.py` needs the `fitparse` package (`pip install fitparse`) to collect at all, independent of this plan — install it once, in the venv, before Task 1 if `pytest -q` reports a collection error there.
- **Two existing test files were checked and need NO changes in this plan:** `tests/test_migration.py` (the older Track-B `weight_log`/`weight_history` additive-column tests) only asserts column *superset* membership (`COMPOSITION_COLUMNS <= columns`) and row counts, both unaffected by adding `person_id` as one more column; `tests/test_docs_drift.py` pins README/`.env.example` text about the bearer-token/composition features, none of which Task 7's `CLAUDE.md`/`README.md` edits touch. Re-run both explicitly at the end of Task 2 and Task 7 respectively to confirm this holds, rather than trusting this note blindly.

---

## File Structure

- **Modify `shared/auth.py`** — adds `_SLUG_RE`, `_RESERVED_SLUGS`, `_slugify` next to the existing `_RESERVED_USERNAMES` (spec §f.4: this is where person-authorization helpers belong, and `shared/migrations.py` importing from `shared/auth.py` is a new one-way edge that must never reverse).
- **Modify `shared/migrations.py`** — adds the 001-specific apply function and every helper it needs: `_REBUILD_TABLES`, `_has_column`, `_needs_person_id_rebuild`, `_first_admin_username`, `now_iso`, `_ensure_primary_person`, `_rebuild_columns`, `_rebuild_sync_status`, `_apply_person_id_rebuild`, `_PERSON_ID_REBUILD_SNAPSHOT_NAME`.
- **Modify `shared/database.py`** — new-shape DDL for the 10 metric tables and `sync_status`, new `persons`/`person_grants` tables, additive `users.default_person_id` and `weight_log.person_id` columns, the `weight_log` index swap, `init_db()` restructured per spec §c.4 to close its connection before calling the runner, and a new `get_primary_person_id()` helper.
- **Modify `vitalforge-dashboard/sync.py`** — every function in the sync chain (`get_synced_dates`, `upsert`, `sync_date`, `sync_weight_history`, `run_sync`, `scheduled_sync`) becomes person-aware.
- **Modify `vitalforge-dashboard/recommendations.py`** — `get_metric`, `get_all_metrics`, `get_recommendations`, `get_rules_only` take `person_id`; the module-level cache becomes a dict keyed by `person_id`.
- **Modify `vitalforge-dashboard/goals.py`** — `compute_progress` takes `person_id`.
- **Modify `vitalforge-dashboard/readiness.py`** — `compute_readiness` takes `person_id`.
- **Modify `vitalforge-dashboard/app.py`** — every route touching a rebuilt table resolves `person_id = await get_primary_person_id()` and passes it down: `/api/sync`, `/api/sync/status`, `/api/metrics/{name}`, `/api/correlations`, `/api/readiness`, `/api/recommendations`, `/api/recommendations/rules-only`, and the goal routes via `_goal_progress`/`_goal_out`.
- **Modify `vitalforge-weight/app.py`** — `post_weight`'s dedup `SELECT`/`INSERT` gain `person_id`; `/api/weight/recent`, `/api/weight/trend`, `DELETE /api/weight/{id}` gain a `person_id` predicate.
- **Modify `tests/conftest.py`** — nothing structural, but `production_schema_db` becomes the fixture the big migration test in Task 2 runs against; no change needed there itself (it already seeds the exact pre-migration shape).
- **New tests** land inside `tests/test_migrations.py` (extends the Phase 0 file — same module, new migration) and `tests/test_migration_gating_assumptions.py` is untouched (Phase 0's assumptions don't change).
- **Modify `CLAUDE.md`** — the repo-layout comment on `database.py` and the `METRIC_TABLES` convention bullet (Appendix B's two phase-1 items).
- **Modify `README.md`** — new upgrade/rollback section per spec §c.7 mitigation 3.

---

### Task 1: Slug validation helpers

**Files:**
- Modify: `shared/auth.py`
- Test: `tests/test_slug_validation.py` (new)

**Interfaces:**
- Produces: `shared.auth._SLUG_RE` (compiled pattern), `shared.auth._RESERVED_SLUGS` (set[str]), `shared.auth._slugify(raw: str) -> str`. Task 2's `_ensure_primary_person` imports all three.

Spec §f.4: `persons.slug` is `TEXT NOT NULL UNIQUE` and appears in every future URL, so it needs the same kind of reservation the codebase already applies to usernames (`_RESERVED_USERNAMES` at `shared/auth.py:61`).

- [ ] **Step 1: Add imports and the three symbols to `shared/auth.py`**

Add to the import block at the top of `shared/auth.py` (alongside the existing `import hashlib` etc.):

```python
import re
import unicodedata
```

Add immediately after the existing line `_RESERVED_USERNAMES = {"anonymous", "api-token"}` (`shared/auth.py:61`):

```python
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

# Anything that would shadow a real path segment under /p/{slug}/ or collide
# with the sentinels this module already reserves for usernames.
_RESERVED_SLUGS = {
    "api", "auth", "static", "health", "p", "new", "admin", "persons",
    "anonymous", "api-token",
}


def _slugify(raw: str) -> str:
    """Derive a URL-safe slug from a display name or username.

    Slugify, do not copy verbatim: _RESERVED_USERNAMES governs "safe as a
    username", a different and smaller rule than "safe as a path segment
    under /p/{slug}/". Returns "" when nothing usable survives -- callers
    must handle that rather than persisting an empty slug into a NOT NULL
    UNIQUE column (see shared/migrations.py's _ensure_primary_person).
    """
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32].strip("-")
    return s if _SLUG_RE.match(s) else ""
```

- [ ] **Step 2: Write the tests**

```python
"""Tests for shared/auth.py's slug validation helpers (spec §f.4)."""

from shared.auth import _RESERVED_SLUGS, _SLUG_RE, _slugify


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Jane Doe") == "jane-doe"


def test_slugify_strips_leading_trailing_hyphens():
    assert _slugify("  --Jane--  ") == "jane"


def test_slugify_normalizes_unicode():
    assert _slugify("José") == "jose"


def test_slugify_truncates_to_32_chars():
    raw = "a" * 50
    result = _slugify(raw)
    assert len(result) <= 32
    assert _SLUG_RE.match(result)


def test_slugify_returns_empty_string_for_unusable_input():
    assert _slugify("!!!") == ""
    assert _slugify("") == ""
    assert _slugify("---") == ""


def test_slug_regex_rejects_uppercase_and_slashes():
    assert not _SLUG_RE.match("Jane")
    assert not _SLUG_RE.match("a/b")
    assert not _SLUG_RE.match("")


def test_reserved_slugs_cover_real_path_segments():
    for segment in ("api", "auth", "static", "health", "admin"):
        assert segment in _RESERVED_SLUGS
```

- [ ] **Step 3: Run the tests**

Run: `pytest tests/test_slug_validation.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 4: Lint and commit**

```bash
ruff check shared/auth.py tests/test_slug_validation.py
git add shared/auth.py tests/test_slug_validation.py
git commit -m "feat: add slug validation helpers for person URLs"
```

---

### Task 2: The `001-person-id-rebuild` migration — schema, apply function, and correctness tests

**Files:**
- Modify: `shared/database.py`
- Modify: `shared/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `shared.auth._slugify`, `shared.auth._RESERVED_SLUGS` (Task 1); `shared.migrations.run_migration`, `shared.migrations.ensure_pre_migration_snapshot`, `shared.migrations.SCHEMA_MIGRATIONS_TABLE_SQL`, `shared.migrations._KNOWN_MIGRATIONS` (Phase 0, already merged).
- Produces: `shared.database.get_primary_person_id() -> int` (every later task in this plan calls it). New tables `persons`, `person_grants`. New columns `users.default_person_id`, `weight_log.person_id`. Rebuilt PKs `(person_id, date)` on the 10 metric tables and `person_id` on `sync_status`. New index `idx_weight_log_person_timestamp`.

This is the largest and highest-risk task in the phase — it is also the one the spec insists must not be split (§c.6: one transaction, all 11 tables). Read spec §(b), §c.4, §c.5, §c.7, §c.8, and §g.1 before starting; this task's code is copied close to verbatim from those sections because they were already reviewed and tested at the design stage.

- [ ] **Step 1: Change the 10 rebuilt tables' `CREATE TABLE IF NOT EXISTS` DDL in `shared/database.py`**

For each of `sleep`, `resting_hr`, `hrv`, `body_battery`, `stress`, `vo2max`, `weight_history`, `training_load`, `steps`, `active_calories`, replace `date TEXT PRIMARY KEY` with `person_id INTEGER NOT NULL` plus `date TEXT NOT NULL` and a trailing `PRIMARY KEY (person_id, date)`. Example for `sleep` (`shared/database.py:141-152`):

```python
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
```

Apply the identical transformation to the other 9 tables — `person_id INTEGER NOT NULL` and `date TEXT NOT NULL` replace `date TEXT PRIMARY KEY`, every other column is untouched, and `PRIMARY KEY (person_id, date)` is appended as the last item. The complete new DDL for each:

```python
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
        # exist -- unchanged from today, still runs after this CREATE TABLE.
        await _add_columns(db, "weight_history", _WEIGHT_HISTORY_ADDITIVE_COLUMNS)

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
```

`weight_history`'s existing `_add_columns(db, "weight_history", _WEIGHT_HISTORY_ADDITIVE_COLUMNS)` call (today at `shared/database.py:216`) stays exactly where it is relative to `weight_history`'s `CREATE TABLE` — shown above for placement clarity, not as a separate change. `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already has these tables in the old shape — this change only takes effect on a brand-new database; existing installations get the new shape from the migration in Step 5.

- [ ] **Step 2: Change `sync_status`'s DDL**

Replace (`shared/database.py:296-303`):

```python
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sync_time TEXT,
                last_sync_result TEXT,
                last_sync_days INTEGER
            )
        """)
```

with:

```python
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                person_id      INTEGER PRIMARY KEY,
                last_sync_time TEXT,
                last_sync_result TEXT,
                last_sync_days INTEGER,
                backoff_until  TEXT
            )
        """)
```

`backoff_until` lands here, in Phase 1, even though nothing writes it until Phase 4 — see spec §b.2 for why (this table is being rebuilt anyway; adding the column later would be pointless churn).

- [ ] **Step 3: Add `persons`, `person_grants`, and the two additive columns**

Add immediately after the `users` table's `CREATE TABLE IF NOT EXISTS` block and its `_add_columns(db, "users", _USERS_ADDITIVE_COLUMNS)` call:

```python
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
```

Add `"default_person_id INTEGER"` to `_USERS_ADDITIVE_COLUMNS` (`shared/database.py:45-47`):

```python
_USERS_ADDITIVE_COLUMNS = [
    "session_version INTEGER NOT NULL DEFAULT 1",
    "default_person_id INTEGER",
]
```

Add `"person_id INTEGER"` to `_WEIGHT_LOG_ADDITIVE_COLUMNS` (`shared/database.py:19-25`):

```python
_WEIGHT_LOG_ADDITIVE_COLUMNS = [
    "body_fat_pct REAL",
    "body_water_pct REAL",
    "muscle_pct REAL",
    "bone_mass_kg REAL",
    "source TEXT",
    "person_id INTEGER",
]
```

- [ ] **Step 4: Swap the `weight_log` index**

Replace the unconditional index creation (`shared/database.py:310`):

```python
        await db.execute("CREATE INDEX IF NOT EXISTS idx_weight_log_timestamp ON weight_log(timestamp)")
```

with:

```python
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp "
            "ON weight_log(person_id, timestamp)"
        )
```

This line already runs after `_add_columns(db, "weight_log", _WEIGHT_LOG_ADDITIVE_COLUMNS)`, so `person_id` exists by the time this executes — no reordering needed.

- [ ] **Step 5: Add the migration helpers to `shared/migrations.py`**

Add these imports at the top of `shared/migrations.py`, alongside the existing ones:

```python
from shared.auth import _RESERVED_SLUGS, _slugify
```

Add the following, after `_KNOWN_MIGRATIONS` and before `assert_schema_understood`:

```python
_PERSON_ID_REBUILD_SNAPSHOT_NAME = "fitness.pre-001-person-id.db"

# Table NAMES only -- no column DDL. _rebuild_columns derives the actual
# column list from the live schema (PRAGMA table_info) instead, so there is
# no second copy of any table's shape to drift out of sync with
# shared/database.py. See CLAUDE.md's METRIC_TABLES convention note: a new
# metric table created before a future rebuild must be added HERE by name.
_REBUILD_TABLES = [
    "sleep", "resting_hr", "hrv", "body_battery", "stress",
    "vo2max", "weight_history", "training_load", "steps", "active_calories",
]


def now_iso() -> str:
    # datetime/timezone are already imported at the top of this module.
    return datetime.now(timezone.utc).isoformat()


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info([{table}])")
    return any(row["name"] == column for row in await cur.fetchall())


async def _needs_person_id_rebuild(db) -> bool:
    """Cheap, TOCTOU-racy-by-design pre-check (spec §c.7): the only cost of
    losing this race is a wasted snapshot, because correctness comes
    entirely from the marker check inside run_migration's transaction."""
    return not await _has_column(db, "sleep", "person_id")


async def _first_admin_username(db) -> str | None:
    cur = await db.execute("SELECT username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    row = await cur.fetchone()
    return row["username"] if row else None


async def _ensure_primary_person(db) -> int:
    """Create (or return) the person that owns all pre-multi-tenancy data.

    Idempotent: called on every migration run, including the fresh-DB path
    where no rebuild follows. Runs inside the migration transaction, so the
    check-then-insert is not racy.
    """
    # `os` is already imported at the top of this module (used by
    # ensure_pre_migration_snapshot) -- no new import needed here.
    existing = await (await db.execute(
        "SELECT id FROM persons WHERE is_primary = 1"
    )).fetchone()
    if existing is not None:
        return existing["id"]

    any_person = await (await db.execute("SELECT COUNT(*) FROM persons")).fetchone()
    if any_person[0] != 0:
        raise RuntimeError("persons rows exist but none is_primary; refusing to guess")

    raw = os.environ.get("VITALFORGE_PRIMARY_PERSON", "").strip() \
        or await _first_admin_username(db) or "primary"
    slug = _slugify(raw)
    if not slug or slug in _RESERVED_SLUGS:
        logger.warning("Primary person slug %r is unusable; falling back to 'primary'", raw)
        slug = "primary"
    cursor = await db.execute(
        "INSERT INTO persons (slug, display_name, created_at, is_primary) "
        "VALUES (?, ?, ?, 1)",
        (slug, raw or slug, now_iso()),
    )
    person_id = cursor.lastrowid
    admin = await (await db.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).fetchone()
    if admin is not None:
        await db.execute(
            "INSERT INTO person_grants (person_id, user_id, access, granted_at) "
            "VALUES (?, ?, 'own', ?)",
            (person_id, admin["id"], now_iso()),
        )
        await db.execute(
            "UPDATE users SET default_person_id = ? WHERE id = ?",
            (person_id, admin["id"]),
        )
    return person_id


async def _rebuild_columns(db, table: str) -> list[tuple[str, str]]:
    """Return [(name, declared_type)] for every non-`date` column of `table`,
    read from the live schema. Fails loud on any shape this migration cannot
    faithfully reproduce."""
    rows = await (await db.execute(f"PRAGMA table_info([{table}])")).fetchall()
    columns: list[tuple[str, str]] = []
    for r in rows:
        if r["name"] == "date":
            if r["pk"] != 1:
                raise RuntimeError(f"{table}.date is not the primary key; refusing to rebuild")
            continue
        if r["notnull"] or r["dflt_value"] is not None or r["pk"]:
            raise RuntimeError(
                f"{table}.{r['name']} carries NOT NULL/DEFAULT/PK, which this migration "
                f"does not know how to reproduce -- update _apply_person_id_rebuild"
            )
        columns.append((r["name"], r["type"]))
    if not columns:
        raise RuntimeError(f"{table} has no non-date columns; refusing to rebuild")
    return columns


async def _rebuild_sync_status(db, person_id: int) -> None:
    await db.execute("""
        CREATE TABLE [sync_status__new] (
            person_id        INTEGER PRIMARY KEY,
            last_sync_time   TEXT,
            last_sync_result TEXT,
            last_sync_days   INTEGER,
            backoff_until    TEXT
        )
    """)
    await db.execute(
        "INSERT INTO [sync_status__new] "
        "(person_id, last_sync_time, last_sync_result, last_sync_days, backoff_until) "
        "SELECT ?, last_sync_time, last_sync_result, last_sync_days, NULL FROM sync_status",
        (person_id,),
    )
    await db.execute("DROP TABLE sync_status")
    await db.execute("ALTER TABLE [sync_status__new] RENAME TO sync_status")


async def _apply_person_id_rebuild(db) -> None:
    # Runs inside BEGIN IMMEDIATE (run_migration opens it). Any exception
    # rolls back the ENTIRE rebuild -- all 11 tables plus the marker --
    # leaving the original schema untouched. weight_log.person_id was added
    # and COMMITTED by _add_columns at init_db's step 2, OUTSIDE this
    # transaction -- see spec §c.6 for why that is correct and safe.
    person_id = await _ensure_primary_person(db)

    if await _has_column(db, "sleep", "person_id"):
        return  # fresh DB: tables already correctly shaped by init_db's DDL step.

    for table in _REBUILD_TABLES:
        columns = await _rebuild_columns(db, table)
        col_names = ", ".join(f"[{name}]" for name, _ in columns)
        col_ddl = ", ".join(f"[{name}] {type_}" for name, type_ in columns)
        await db.execute(f"""
            CREATE TABLE [{table}__new] (
                person_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                {col_ddl},
                PRIMARY KEY (person_id, date)
            )
        """)
        await db.execute(
            f"INSERT INTO [{table}__new] (person_id, date, {col_names}) "
            f"SELECT ?, date, {col_names} FROM [{table}]",
            (person_id,),
        )
        await db.execute(f"DROP TABLE [{table}]")
        await db.execute(f"ALTER TABLE [{table}__new] RENAME TO [{table}]")

    await _rebuild_sync_status(db, person_id)

    await db.execute("UPDATE weight_log SET person_id = ? WHERE person_id IS NULL", (person_id,))
    await db.execute("DROP INDEX IF EXISTS idx_weight_log_timestamp")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp "
        "ON weight_log(person_id, timestamp)"
    )
```

- [ ] **Step 6: Wire the migration and `get_primary_person_id()` into `shared/database.py`**

Restructure `init_db()` (`shared/database.py:112-357`) per spec §c.4 — the migration runs on its own connection, opened and closed **after** `init_db`'s own connection is fully closed:

```python
async def init_db():
    """Create all tables if they don't exist, then run any pending schema
    migrations."""
    from shared.migrations import (
        SCHEMA_MIGRATIONS_TABLE_SQL,
        _apply_person_id_rebuild,
        _needs_person_id_rebuild,
        _PERSON_ID_REBUILD_SNAPSHOT_NAME,
        assert_schema_understood,
        ensure_pre_migration_snapshot,
        run_migration,
    )

    db = await get_db()
    try:
        # ... every existing CREATE TABLE IF NOT EXISTS / _add_columns call,
        # unchanged in ORDER, but using the new DDL from Steps 1-4 above ...
        await db.commit()
    finally:
        await db.close()          # <-- connection closed BEFORE anything below

    await ensure_pre_migration_snapshot(_PERSON_ID_REBUILD_SNAPSHOT_NAME, _needs_person_id_rebuild)
    await run_migration("001-person-id-rebuild", _apply_person_id_rebuild)

    await assert_schema_understood()


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
```

(The `# ...` line above is a placement instruction for you to apply Steps 1-4's edits in place — every one of those DDL statements must actually be written out in `shared/database.py`; nothing is omitted from the real file.)

- [ ] **Step 7: Write the migration correctness tests**

Append to `tests/test_migrations.py`:

```python
"""001-person-id-rebuild tests appended below the Phase 0 runner tests
above. See docs/superpowers/specs/2026-08-25-family-multitenancy-design.md
§c.8 for the full required-test list this section implements.
"""

from datetime import datetime, timedelta, timezone

import pytest_asyncio


@pytest.mark.asyncio
async def test_fresh_db_gets_new_shape_directly_and_one_primary_person(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(sleep)")
        cols = {row["name"]: row for row in await cur.fetchall()}
        assert "person_id" in cols
        pk_cols = sorted(name for name, row in cols.items() if row["pk"])
        assert pk_cols == ["date", "person_id"]

        cur = await db.execute("SELECT COUNT(*) FROM persons WHERE is_primary = 1")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_preserves_row_counts_and_backfills_person_id(production_schema_db, monkeypatch):
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == 17
        cur = await db.execute("SELECT COUNT(*) FROM weight_history")
        assert (await cur.fetchone())[0] == 34
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("SELECT COUNT(*) FROM weight_history WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0

        cur = await db.execute("SELECT id FROM persons WHERE is_primary = 1")
        primary_id = (await cur.fetchone())["id"]
        cur = await db.execute("SELECT DISTINCT person_id FROM weight_history")
        assert [row["person_id"] for row in await cur.fetchall()] == [primary_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_parity_fresh_vs_migrated(tmp_path, monkeypatch, production_schema_db):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    fresh_db = await database.get_db()

    monkeypatch.setattr(database, "DB_PATH", production_schema_db)
    await database.init_db()
    migrated_db = await database.get_db()

    try:
        for table in migrations._REBUILD_TABLES + ["sync_status", "weight_log"]:
            fresh_info = await (await fresh_db.execute(f"PRAGMA table_info([{table}])")).fetchall()
            migrated_info = await (await migrated_db.execute(f"PRAGMA table_info([{table}])")).fetchall()
            fresh_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in fresh_info)
            migrated_shape = sorted((r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]) for r in migrated_info)
            assert fresh_shape == migrated_shape, f"{table} shape diverged between fresh and migrated"

            fresh_idx = sorted(r["name"] for r in await (await fresh_db.execute(f"PRAGMA index_list([{table}])")).fetchall())
            migrated_idx = sorted(r["name"] for r in await (await migrated_db.execute(f"PRAGMA index_list([{table}])")).fetchall())
            assert fresh_idx == migrated_idx, f"{table} index set diverged between fresh and migrated"
    finally:
        await fresh_db.close()
        await migrated_db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(production_schema_db):
    await database.init_db()
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        first_count = (await cur.fetchone())[0]
    finally:
        await db.close()

    await database.init_db()  # second boot against the already-migrated DB
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == first_count
        cur = await db.execute("SELECT COUNT(*) FROM persons")
        assert (await cur.fetchone())[0] == 1, "a second run created a second primary person"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_interruption_rolls_back_rebuild_but_weight_log_person_id_stays_committed(
    production_schema_db, monkeypatch
):
    """Spec §c.6: the *rebuild* (11 tables + marker) is fully-old-or-fully-new,
    but weight_log.person_id is added by a separate, already-committed
    _add_columns step at init_db's step 2. A test that checks only
    byte-identity on the rebuilt tables would pass in both failure modes and
    prove nothing -- this test asserts both halves explicitly."""
    call_count = 0
    real_apply = migrations._apply_person_id_rebuild

    async def failing_apply(db):
        nonlocal call_count
        call_count += 1
        person_id = await migrations._ensure_primary_person(db)
        if await migrations._has_column(db, "sleep", "person_id"):
            return
        for i, table in enumerate(migrations._REBUILD_TABLES):
            if i == 5:
                raise RuntimeError("simulated crash mid-rebuild")
            columns = await migrations._rebuild_columns(db, table)
            col_names = ", ".join(f"[{name}]" for name, _ in columns)
            col_ddl = ", ".join(f"[{name}] {type_}" for name, type_ in columns)
            await db.execute(
                f"CREATE TABLE [{table}__new] (person_id INTEGER NOT NULL, date TEXT NOT NULL, "
                f"{col_ddl}, PRIMARY KEY (person_id, date))"
            )
            await db.execute(
                f"INSERT INTO [{table}__new] (person_id, date, {col_names}) "
                f"SELECT ?, date, {col_names} FROM [{table}]",
                (person_id,),
            )
            await db.execute(f"DROP TABLE [{table}]")
            await db.execute(f"ALTER TABLE [{table}__new] RENAME TO [{table}]")

    monkeypatch.setattr(migrations, "_apply_person_id_rebuild", failing_apply)

    pre_shapes = {}
    db = await database.get_db()
    try:
        for table in migrations._REBUILD_TABLES + ["sync_status"]:
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
            pre_shapes[table] = (await cur.fetchone())["sql"]
    finally:
        await db.close()

    with pytest.raises(RuntimeError, match="simulated crash"):
        # Calling init_db() itself is correct here, not a shortcut: its
        # local `from shared.migrations import _apply_person_id_rebuild`
        # resolves the monkeypatched attribute at call time, so this run
        # uses failing_apply while still exercising the real init_db/
        # run_migration wiring end to end.
        await database.init_db()

    db = await database.get_db()
    try:
        for table in migrations._REBUILD_TABLES + ["sync_status"]:
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
            assert (await cur.fetchone())["sql"] == pre_shapes[table], f"{table} was not rolled back"
        cur = await db.execute("SELECT name FROM schema_migrations WHERE name = '001-person-id-rebuild'")
        assert await cur.fetchone() is None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%__new'"
        )
        assert await cur.fetchall() == [], "a __new table survived the rollback"

        cur = await db.execute("PRAGMA table_info(weight_log)")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "person_id" in cols, "weight_log.person_id must survive -- it committed independently"
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NOT NULL")
        assert (await cur.fetchone())[0] == 0, "person_id must be all-NULL before the rebuild completes"
    finally:
        await db.close()

    monkeypatch.setattr(migrations, "_apply_person_id_rebuild", real_apply)
    await database.init_db()  # run again cleanly
    db = await database.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cur.fetchone())[0] == 0, "did not converge on a clean re-run"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_snapshot_is_created_before_the_rebuild_runs(production_schema_db):
    await database.init_db()
    snapshot = production_schema_db.parent / "fitness.pre-001-person-id.db"
    assert snapshot.exists()

    check = await aiosqlite.connect(str(snapshot))
    try:
        cur = await check.execute("SELECT COUNT(*) FROM weight_log")
        assert (await cur.fetchone())[0] == 17
        cur = await check.execute("PRAGMA table_info(sleep)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "person_id" not in cols, "snapshot must predate the rebuild (weight_log.person_id may exist, sleep must not)"
    finally:
        await check.close()


@pytest.mark.asyncio
async def test_snapshot_not_taken_on_a_fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    assert not (tmp_path / "fitness.pre-001-person-id.db").exists()


@pytest.mark.asyncio
async def test_cross_person_isolation_after_second_person_exists(production_schema_db):
    """Regression test for the bug this whole design exists to prevent.
    Phase 1 itself never creates a second person, but the rebuilt PK must
    already make this safe -- Phase 2 relies on that, not on any code it
    will add."""
    await database.init_db()

    db = await database.get_db()
    try:
        cur = await db.execute("SELECT id FROM persons WHERE is_primary = 1")
        person_a = (await cur.fetchone())["id"]
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", datetime.now(timezone.utc).isoformat()),
        )
        person_b = cursor.lastrowid
        await db.commit()

        await db.execute(
            "INSERT INTO sleep (person_id, date, duration_seconds) VALUES (?, '2026-01-01', 100)",
            (person_a,),
        )
        await db.execute(
            "INSERT INTO sleep (person_id, date, duration_seconds) VALUES (?, '2026-01-01', 200)",
            (person_b,),
        )
        await db.commit()

        cur = await db.execute("SELECT COUNT(*) FROM sleep WHERE date = '2026-01-01'")
        assert (await cur.fetchone())[0] == 2, "same date, different persons must NOT overwrite each other"
    finally:
        await db.close()
```

Add the two missing imports at the top of `tests/test_migrations.py` (it already imports `asyncio`, `aiosqlite`, `pytest`, `shared.database as database`, `shared.migrations as migrations`):

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 8: Run the tests**

Run: `pytest tests/test_migrations.py -v`
Expected: every test in Step 7, plus all Phase 0 tests already in the file, PASS.

Run: `pytest tests/test_migration.py -v`
Expected: all PASS unchanged (Global Constraints notes why this file is expected to be unaffected — confirm it here rather than assuming).

Run: `pytest -q` (full suite)
Expected: pre-existing tests that call `init_db()` and then assert on old-shape `sleep`/`sync_status`/etc. now fail — this is correct and expected at this point; they get fixed in Tasks 3-6 below as their production code changes. Note which ones fail here so you can confirm they are exactly the ones this plan's later tasks touch.

- [ ] **Step 9: Lint and commit**

```bash
ruff check shared/database.py shared/migrations.py tests/test_migrations.py
git add shared/database.py shared/migrations.py tests/test_migrations.py
git commit -m "feat: add 001-person-id-rebuild migration (persons/person_grants + PK rebuild)"
```

---

### Task 3: Person-aware sync (`vitalforge-dashboard/sync.py`)

**Files:**
- Modify: `vitalforge-dashboard/sync.py`
- Modify: `tests/test_sync.py`

**Interfaces:**
- Consumes: `shared.database.get_primary_person_id` (Task 2).
- Produces: every function below now requires `person_id: int` with **no default**. `vitalforge-dashboard/app.py` (Task 6) is the only caller outside this file and this file's tests.

This implements spec §0.2 Group D's signature chain plus statements #3, #7, #8.

- [ ] **Step 1: Update `get_synced_dates` and `upsert`**

```python
async def get_synced_dates(table: str, person_id: int) -> set[str]:
    """Return the set of dates already stored for a given metric table."""
    db = await get_db()
    try:
        cursor = await db.execute(f"SELECT date FROM [{table}] WHERE person_id = ?", (person_id,))
        rows = await cursor.fetchall()
        return {row["date"] for row in rows}
    finally:
        await db.close()


async def upsert(table: str, date: str, person_id: int, **columns):
    """Insert or replace a row in a metric table, scoped to person_id."""
    cols = ["person_id", "date"] + list(columns.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [person_id, date] + list(columns.values())

    db = await get_db()
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO [{table}] ({col_names}) VALUES ({placeholders})",
            values,
        )
        await db.commit()
    finally:
        await db.close()
```

- [ ] **Step 2: Thread `person_id` through `sync_date`**

Replace the whole function with this version — every `await upsert(...)` call gains `person_id` as its third positional argument, immediately after `date_str`; nothing else in the function's logic changes:

```python
async def sync_date(date_str: str, person_id: int):
    """Pull all metrics from Garmin for a single date and store them."""

    # --- Sleep ---
    sleep = garmin_client.get_sleep_data(date_str)
    if sleep and isinstance(sleep, dict):
        dto = sleep.get("dailySleepDTO", sleep)
        if isinstance(dto, dict) and dto.get("sleepTimeSeconds"):
            await upsert(
                "sleep", date_str, person_id,
                duration_seconds=dto.get("sleepTimeSeconds"),
                deep_seconds=dto.get("deepSleepSeconds"),
                light_seconds=dto.get("lightSleepSeconds"),
                rem_seconds=dto.get("remSleepSeconds"),
                awake_seconds=dto.get("awakeSleepSeconds"),
                sleep_score=_extract_sleep_score(dto, sleep),
                avg_spo2=dto.get("averageSpO2Value"),
                avg_respiration=dto.get("averageRespirationValue"),
            )

    # --- User summary (steps, calories, RHR) ---
    summary = garmin_client.get_user_summary(date_str)
    if summary and isinstance(summary, dict):
        rhr = summary.get("restingHeartRate")
        if rhr:
            await upsert("resting_hr", date_str, person_id, value=rhr)

        total_steps = summary.get("totalSteps")
        if total_steps is not None:
            await upsert("steps", date_str, person_id, value=total_steps)

        active_cal = summary.get("activeKilocalories")
        if active_cal is not None:
            await upsert("active_calories", date_str, person_id, value=active_cal)

    # --- HRV ---
    hrv = garmin_client.get_hrv_data(date_str)
    if hrv and isinstance(hrv, dict):
        hrv_summary = hrv.get("hrvSummary", hrv)
        if isinstance(hrv_summary, dict):
            last_night = hrv_summary.get("lastNightAvg")
            if last_night:
                await upsert(
                    "hrv", date_str, person_id,
                    last_night_avg=last_night,
                    last_night_5min_high=hrv_summary.get("lastNight5MinHigh"),
                    weekly_avg=hrv_summary.get("weeklyAvg"),
                    status=hrv_summary.get("status"),
                )

    # --- Body Battery ---
    bb = garmin_client.get_body_battery(date_str)
    if bb:
        entry = bb[0] if isinstance(bb, list) and bb else bb
        if isinstance(entry, dict):
            bb_array = entry.get("bodyBatteryValuesArray", [])
            highest = None
            lowest = None
            if bb_array:
                bb_levels = [item[1] for item in bb_array if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1] is not None]
                if bb_levels:
                    highest = max(bb_levels)
                    lowest = min(bb_levels)

            if highest is None:
                highest = entry.get("bodyBatteryHighestValue")
            if lowest is None:
                lowest = entry.get("bodyBatteryLowestValue")

            if highest is not None:
                await upsert(
                    "body_battery", date_str, person_id,
                    charged=entry.get("charged") or entry.get("bodyBatteryChargedValue"),
                    drained=entry.get("drained") or entry.get("bodyBatteryDrainedValue"),
                    highest=highest,
                    lowest=lowest,
                )

    # --- Stress ---
    stress = garmin_client.get_stress_data(date_str)
    if stress and isinstance(stress, dict):
        avg_stress = stress.get("avgStressLevel") or stress.get("overallStressLevel")
        if avg_stress is not None:
            await upsert(
                "stress", date_str, person_id,
                avg_level=avg_stress,
                max_level=stress.get("maxStressLevel"),
                rest_duration=stress.get("restStressDuration"),
                low_duration=stress.get("lowStressDuration"),
                medium_duration=stress.get("mediumStressDuration"),
                high_duration=stress.get("highStressDuration"),
            )

    # --- VO2 Max (from training status, since get_max_metrics often returns null) ---
    training = garmin_client.get_training_status(date_str)
    if training and isinstance(training, dict):
        most_recent = training.get("mostRecentVO2Max", {})
        if isinstance(most_recent, dict):
            generic = most_recent.get("generic") or {}
            if isinstance(generic, dict):
                vo2 = generic.get("vo2MaxValue")
                if vo2:
                    await upsert(
                        "vo2max", date_str, person_id,
                        vo2max_value=vo2,
                        fitness_age=generic.get("fitnessAge"),
                    )

        load_balance = training.get("mostRecentTrainingLoadBalance")
        if isinstance(load_balance, dict):
            load_map = load_balance.get("metricsTrainingLoadBalanceDTOMap", {})
            if isinstance(load_map, dict):
                for device_id, device_data in load_map.items():
                    if isinstance(device_data, dict):
                        aero_low = device_data.get("monthlyLoadAerobicLow") or 0
                        aero_high = device_data.get("monthlyLoadAerobicHigh") or 0
                        anaerobic = device_data.get("monthlyLoadAnaerobic") or 0
                        total = round(aero_low + aero_high + anaerobic, 1)
                        if total > 0:
                            await upsert(
                                "training_load", date_str, person_id,
                                acute_load=total,
                                chronic_load=None,
                                load_ratio=None,
                            )
                        break  # use first/primary device only

        if not load_balance:
            agg = training.get("aggregatedTrainingLoad") or {}
            acute = training.get("acuteLoad") or (agg.get("acuteLoad") if isinstance(agg, dict) else None)
            if acute is not None:
                await upsert(
                    "training_load", date_str, person_id,
                    acute_load=acute,
                    chronic_load=training.get("chronicLoad") or (agg.get("chronicLoad") if isinstance(agg, dict) else None),
                    load_ratio=training.get("loadRatio") or (agg.get("loadRatio") if isinstance(agg, dict) else None),
                )
```

- [ ] **Step 3: Thread `person_id` through `sync_weight_history`**

Change the signature to `async def sync_weight_history(start_date: str, end_date: str, person_id: int):` and update its one `await upsert(...)` call:

```python
                await upsert(
                    "weight_history", date_val, person_id,
                    weight_grams=weight_g,
                    bmi=latest.get("bmi"),
                    body_fat=latest.get("bodyFat"),
                    body_water=latest.get("bodyWater"),
                    bone_mass_g=latest.get("boneMass"),
                    muscle_mass_g=latest.get("muscleMass"),
                )
```

- [ ] **Step 4: Thread `person_id` through `run_sync`**

```python
async def run_sync(days: int = 7, *, person_id: int):
    """Run a full sync for the given number of days back from today."""
    logger.info("Starting sync for person %s, last %d days", person_id, days)
    start_time = datetime.now(timezone.utc)
    result = "success"
    errors = 0

    garmin_client.authenticate()

    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

    tables = [
        "sleep", "resting_hr", "hrv", "body_battery",
        "stress", "vo2max", "training_load", "steps", "active_calories",
    ]
    existing = {}
    for table in tables:
        existing[table] = await get_synced_dates(table, person_id)

    today_str = today.isoformat()

    for date_str in dates:
        if date_str != today_str:
            all_present = all(date_str in existing[t] for t in tables)
            if all_present:
                continue

        try:
            await sync_date(date_str, person_id)
        except Exception:
            logger.exception("Error syncing date %s", date_str)
            errors += 1

    try:
        start_date = (today - timedelta(days=days)).isoformat()
        await sync_weight_history(start_date, today_str, person_id)
    except Exception as e:
        logger.error("Error syncing weight history: %s", e)
        errors += 1

    if errors:
        result = f"completed with {errors} errors"

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info("Sync completed in %.1fs — %s", elapsed, result)

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO sync_status (person_id, last_sync_time, last_sync_result, last_sync_days) "
            "VALUES (?, ?, ?, ?)",
            (person_id, start_time.isoformat(), result, days),
        )
        await db.commit()
    finally:
        await db.close()

    return result
```

- [ ] **Step 5: Resolve `person_id` at the top of `scheduled_sync`**

```python
async def scheduled_sync(lock: asyncio.Lock):
    """Background loop that syncs every SYNC_INTERVAL_HOURS."""
    from shared.database import get_primary_person_id

    logger.info("Running initial 90-day backfill...")
    try:
        person_id = await get_primary_person_id()
        async with lock:
            await run_sync(days=90, person_id=person_id)
    except Exception as e:
        logger.error("Initial backfill failed: %s", e)

    while True:
        await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)
        try:
            person_id = await get_primary_person_id()
            logger.info("Running scheduled sync...")
            async with lock:
                await run_sync(days=3, person_id=person_id)
        except Exception as e:
            logger.error("Scheduled sync failed: %s", e)
```

`person_id` is re-resolved every cycle rather than resolved once at task start — cheap (one `SELECT`), and correct even if a future phase ever changes which person is primary.

- [ ] **Step 6: Update `tests/test_sync.py`**

Every test in this file that calls `get_synced_dates`, `upsert`, `sync_date`, `sync_weight_history`, or `run_sync` must pass a `person_id`. Resolve one via the `initialized_db` fixture (which runs `init_db()`, so a primary person already exists) plus `shared.database.get_primary_person_id()`. Worked example — update the pattern used across the file:

```python
@pytest.mark.asyncio
async def test_run_sync_stores_metrics(initialized_db, fake_garmin_client):
    from shared.database import get_primary_person_id

    person_id = await get_primary_person_id()
    result = await sync.run_sync(days=1, person_id=person_id)

    assert result == "success"
```

Apply the identical `person_id = await get_primary_person_id()` resolution, then thread `person_id` into the corresponding call, to every other test in `tests/test_sync.py` that calls one of the five changed functions.

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_sync.py -v`
Expected: all PASS.

- [ ] **Step 8: Lint and commit**

```bash
ruff check vitalforge-dashboard/sync.py tests/test_sync.py
git add vitalforge-dashboard/sync.py tests/test_sync.py
git commit -m "feat: thread person_id through the sync chain"
```

---

### Task 4: Person-aware recommendations (`vitalforge-dashboard/recommendations.py`, `readiness.py`)

**Files:**
- Modify: `vitalforge-dashboard/recommendations.py`
- Modify: `vitalforge-dashboard/readiness.py`
- Modify: `tests/test_recommendations.py`
- Modify: `tests/test_readiness.py`

**Interfaces:**
- Produces: `get_metric(table, column, person_id, days=30)`, `get_all_metrics(person_id, days=30)`, `get_recommendations(person_id, force=False)`, `get_rules_only(person_id)`, `compute_readiness(person_id)`. `run_rules`, `avg`, `stdev`, `trend_slope` etc. are unchanged (they operate on already-fetched, already-scoped data).

This implements spec §0.2 statement #2 and §i Q11 (cache keyed by person).

- [ ] **Step 1: Update `get_metric` and `get_all_metrics`**

```python
async def get_metric(table: str, column: str, person_id: int, days: int = 30) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT date, [{column}] as value FROM [{table}] "
            f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
            (person_id, f"-{days} days"),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [{"date": r["date"], "value": r["value"]} for r in rows if r["value"] is not None]


async def get_all_metrics(person_id: int, days: int = 30) -> dict:
    metrics = {
        "sleep_duration": ("sleep", "duration_seconds"),
        "sleep_score": ("sleep", "sleep_score"),
        "resting_hr": ("resting_hr", "value"),
        "hrv": ("hrv", "last_night_avg"),
        "body_battery": ("body_battery", "highest"),
        "stress": ("stress", "avg_level"),
        "vo2max": ("vo2max", "vo2max_value"),
        "weight": ("weight_history", "weight_grams"),
        "training_load": ("training_load", "acute_load"),
        "steps": ("steps", "value"),
    }
    result = {}
    for name, (table, col) in metrics.items():
        result[name] = await get_metric(table, col, person_id, days)
    return result
```

- [ ] **Step 2: Key the cache by `person_id`**

Replace the module-level cache (`recommendations.py:15`):

```python
# Cache: { person_id: {"hash": ..., "timestamp": ..., "recommendations": [...]} }
_cache: dict[int, dict] = {}
CACHE_TTL = 6 * 3600  # 6 hours
```

Replace `get_recommendations` and `get_rules_only`:

```python
async def get_recommendations(person_id: int, force: bool = False) -> dict:
    """Get recommendations for one person, using that person's cache slot if available."""
    data = await get_all_metrics(person_id, days=30)

    data_hash = hashlib.md5(json.dumps(data, default=str).encode()).hexdigest()
    now = time.time()

    cached = _cache.get(person_id)
    if not force and cached and cached["hash"] == data_hash and (now - cached["timestamp"]) < CACHE_TTL and cached["recommendations"]:
        return {
            "recommendations": cached["recommendations"],
            "cached": True,
            "generated_at": cached["timestamp"],
        }

    findings = run_rules(data)
    recommendations = await get_llm_recommendations(findings, data)

    _cache[person_id] = {
        "hash": data_hash,
        "timestamp": now,
        "recommendations": recommendations,
    }

    return {
        "recommendations": recommendations,
        "cached": False,
        "generated_at": now,
    }


async def get_rules_only(person_id: int) -> dict:
    """Get just the rules engine output without LLM, for one person."""
    data = await get_all_metrics(person_id, days=30)
    findings = run_rules(data)
    return {
        "findings": findings,
        "count": len(findings),
    }
```

- [ ] **Step 3: Thread `person_id` through `compute_readiness`**

In `vitalforge-dashboard/readiness.py`, change:

```python
async def compute_readiness(person_id: int) -> dict:
    data = await get_all_metrics(person_id, days=30)
    ...  # rest of the function body is unchanged
```

(Only the signature and the one `get_all_metrics` call site change; the scoring logic below it consumes `data` exactly as before.)

- [ ] **Step 4: Update the tests**

In `tests/test_recommendations.py` and `tests/test_readiness.py`, every direct call to `get_metric`, `get_all_metrics`, `get_recommendations`, `get_rules_only`, or `compute_readiness` needs a `person_id`, resolved the same way as Task 3 Step 6:

```python
from shared.database import get_primary_person_id

person_id = await get_primary_person_id()
result = await recommendations.get_recommendations(person_id)
```

Apply this resolve-then-pass pattern to every existing test in both files that calls one of the five changed functions.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_recommendations.py tests/test_readiness.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint and commit**

```bash
ruff check vitalforge-dashboard/recommendations.py vitalforge-dashboard/readiness.py tests/test_recommendations.py tests/test_readiness.py
git add vitalforge-dashboard/recommendations.py vitalforge-dashboard/readiness.py tests/test_recommendations.py tests/test_readiness.py
git commit -m "feat: scope recommendations and readiness by person, cache keyed by person_id"
```

---

### Task 5: Person-aware weight service (`vitalforge-weight/app.py`)

**Files:**
- Modify: `vitalforge-weight/app.py`
- Modify: `tests/test_dedup.py`, `tests/test_dedup_boundary_precision.py`, `tests/test_dedup_concurrency.py`, `tests/test_weight_api.py`

**Interfaces:**
- Consumes: `shared.database.get_primary_person_id` (Task 2).

This implements spec §0.2 statements #5, #6, #9, #10, #13, and Correction 1 in full.

- [ ] **Step 1: Import `get_primary_person_id`**

Change the existing import line (`vitalforge-weight/app.py:19`):

```python
from shared.database import get_db, get_primary_person_id, init_db
```

- [ ] **Step 2: Scope `post_weight`'s dedup `SELECT` and `INSERT` by person**

At the top of `post_weight`, after computing `timestamp` and before opening the DB connection, add:

```python
    person_id = await get_primary_person_id()
```

Add `AND person_id = ?` to the dedup `SELECT`'s `WHERE` clause and `person_id` as the first bound parameter (`vitalforge-weight/app.py:243-261`):

```python
        cursor = await db.execute(
            "SELECT id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
            "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source "
            "FROM weight_log "
            "WHERE person_id = ? "
            "AND timestamp >= ? "
            "AND ABS(weight_grams - ?) <= ? "
            "AND julianday(timestamp) >= julianday(?, ?) "
            "AND julianday(timestamp) <= julianday(?, ?) "
            "ORDER BY timestamp DESC LIMIT 1",
            (
                person_id,
                sargable_cutoff,
                weight_grams,
                DEDUP_WEIGHT_TOLERANCE_GRAMS,
                timestamp,
                f"-{DEDUP_WINDOW_SECONDS} seconds",
                timestamp,
                f"+{DEDUP_WINDOW_SECONDS} seconds",
            ),
        )
```

Add `person_id` to the `INSERT` (`vitalforge-weight/app.py:278-293`, spec statement #9 — the one the first design draft omitted):

```python
            cursor = await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
                "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, source) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    round(weight_lbs, 2),
                    round(weight_kg, 2),
                    weight_grams,
                    timestamp,
                    data.body_fat_pct,
                    data.body_water_pct,
                    data.muscle_pct,
                    data.bone_mass_kg,
                    data.source,
                ),
            )
```

Leave the enrichment `UPDATE ... WHERE id = ?` and the `synced_to_garmin` flag `UPDATE ... WHERE id = ?` unchanged (spec statements #11/#12 — safe by inference now that #10's predicate exists; a redundant `AND person_id = ?` there would just be dead weight since `existing["id"]` is already provably this person's row).

- [ ] **Step 3: Scope `/api/weight/recent`**

```python
@app.get("/api/weight/recent")
async def get_recent_weights():
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, weight_lbs, weight_kg, timestamp, synced_to_garmin FROM weight_log "
            "WHERE person_id = ? ORDER BY timestamp DESC LIMIT 10",
            (person_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return [
        {
            "id": row["id"],
            "weight_lbs": row["weight_lbs"],
            "weight_kg": row["weight_kg"],
            "timestamp": row["timestamp"],
            "synced_to_garmin": bool(row["synced_to_garmin"]),
        }
        for row in rows
    ]
```

- [ ] **Step 4: Scope `/api/weight/trend`**

```python
@app.get("/api/weight/trend")
async def get_weight_trend():
    """Return last 30 days of weights for the trend chart."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT weight_lbs, weight_kg, timestamp FROM weight_log "
            "WHERE person_id = ? AND timestamp >= datetime('now', '-30 days') ORDER BY timestamp ASC",
            (person_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    return [
        {"weight_lbs": row["weight_lbs"], "weight_kg": row["weight_kg"], "timestamp": row["timestamp"]}
        for row in rows
    ]
```

- [ ] **Step 5: Scope `DELETE /api/weight/{weight_id}` (IDOR guard, spec statement #13)**

```python
@app.delete("/api/weight/{weight_id}")
async def delete_weight(weight_id: int):
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM weight_log WHERE id = ? AND person_id = ?", (weight_id, person_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Weight entry not found")
    finally:
        await db.close()

    return {"success": True, "deleted_id": weight_id}
```

`weight_id` comes straight off the URL path — per spec statement #13 this is not "safe by inference," it needs the explicit predicate.

- [ ] **Step 6: Add a regression test guarding statement #9 (the write the original design draft missed)**

Add to `tests/test_weight_api.py`:

```python
@pytest.mark.asyncio
async def test_posted_weight_always_carries_a_person_id(weight_app_module):
    from httpx import ASGITransport, AsyncClient
    from shared.database import get_db

    async with AsyncClient(transport=ASGITransport(app=weight_app_module.app), base_url="http://test") as client:
        response = await client.post("/api/weight", json={"weight": 180.0, "unit": "lbs"})
        assert response.status_code == 200

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()
```

- [ ] **Step 7: Add the cross-person dedup isolation test (spec §c.8's required test)**

Add to `tests/test_dedup.py`:

```python
@pytest.mark.asyncio
async def test_two_persons_same_second_similar_weight_produce_two_rows(initialized_db):
    """Regression test for spec §0.2 Correction 1: without a person predicate
    on the dedup SELECT, two family members weighing in within the dedup
    window at similar weights would silently merge into one row."""
    from datetime import datetime, timezone

    from shared.database import get_db, get_primary_person_id

    person_a = await get_primary_person_id()
    now = datetime.now(timezone.utc)

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, 0)",
            ("second", "Second Person", now.isoformat()),
        )
        person_b = cursor.lastrowid
        await db.commit()

        for pid in (person_a, person_b):
            await db.execute(
                "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
                "VALUES (?, 180.0, 81.6, 81600, ?, 0)",
                (pid, now.isoformat()),
            )
        await db.commit()

        cursor = await db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM weight_log WHERE timestamp = ?", (now.isoformat(),)
        )
        assert (await cursor.fetchone())[0] == 2
    finally:
        await db.close()
```

- [ ] **Step 8: Update the remaining existing dedup/weight tests**

Every existing test in `tests/test_dedup.py`, `tests/test_dedup_boundary_precision.py`, and `tests/test_dedup_concurrency.py` that seeds a `weight_log` row directly via SQL must add `person_id` to its `INSERT` column list, resolved via `await get_primary_person_id()` beforehand (same pattern as Step 7 above). Apply that one mechanical change to every direct `INSERT INTO weight_log` in those three files; no other part of those tests changes.

- [ ] **Step 9: Run the tests**

Run: `pytest tests/test_dedup.py tests/test_dedup_boundary_precision.py tests/test_dedup_concurrency.py tests/test_weight_api.py -v`
Expected: all PASS.

- [ ] **Step 10: Lint and commit**

```bash
ruff check vitalforge-weight/app.py tests/test_dedup.py tests/test_dedup_boundary_precision.py tests/test_dedup_concurrency.py tests/test_weight_api.py
git add vitalforge-weight/app.py tests/test_dedup.py tests/test_dedup_boundary_precision.py tests/test_dedup_concurrency.py tests/test_weight_api.py
git commit -m "feat: scope weight_log reads/writes by person_id"
```

---

### Task 6: Wire person resolution into `vitalforge-dashboard/app.py` and `goals.py`

**Files:**
- Modify: `vitalforge-dashboard/app.py`
- Modify: `vitalforge-dashboard/goals.py`
- Modify: `tests/test_dashboard_api.py`, `tests/test_correlations_api.py`, `tests/test_goals.py`

**Interfaces:**
- Consumes: `shared.database.get_primary_person_id`, `recommendations.get_recommendations/get_rules_only` (Task 4), `goals.compute_progress` (below), `readiness.compute_readiness` (Task 4), `sync.run_sync` (Task 3).

This implements spec §0.2 statements #1, #4, and extends the audit to `api_correlations` and `goals.py`'s progress computation, which resolve to the same rebuilt tables via `METRIC_TABLES` but weren't in the spec's original 13-item list.

- [ ] **Step 1: Update the import line**

```python
from shared.database import get_db, get_primary_person_id, init_db
```

- [ ] **Step 2: Scope `sync_status` (statement #4) and `trigger_sync`**

```python
@app.post("/api/sync")
async def trigger_sync(days: int = Query(default=7, ge=1, le=90)):
    """Trigger a manual data sync."""
    if _sync_lock.locked():
        return {"status": "already_running", "message": "A sync is already in progress"}

    async def _do_sync():
        person_id = await get_primary_person_id()
        async with _sync_lock:
            await run_sync(days=days, person_id=person_id)

    asyncio.create_task(_do_sync())
    return {"status": "started", "days": days}


@app.get("/api/sync/status")
async def sync_status():
    """Return last sync time and result."""
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT last_sync_time, last_sync_result, last_sync_days FROM sync_status WHERE person_id = ?",
            (person_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        return {"last_sync_time": None, "last_sync_result": "never", "syncing": _sync_lock.locked()}

    return {
        "last_sync_time": row["last_sync_time"],
        "last_sync_result": row["last_sync_result"],
        "last_sync_days": row["last_sync_days"],
        "syncing": _sync_lock.locked(),
    }
```

- [ ] **Step 3: Scope `/api/metrics/{metric_name}` (statement #1)**

```python
@app.get("/api/metrics/{metric_name}")
async def get_metrics(metric_name: str, days: int = Query(default=30, ge=1, le=365)):
    """Return time series data for a metric with 7-day moving average."""
    if metric_name not in METRIC_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric '{metric_name}'. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )

    table, column = METRIC_TABLES[metric_name]
    person_id = await get_primary_person_id()

    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT date, [{column}] as value FROM [{table}] "
            f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
            (person_id, f"-{days} days"),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    data = [{"date": row["date"], "value": row["value"]} for row in rows if row["value"] is not None]

    values = [d["value"] for d in data]
    moving_avg = []
    for i in range(len(values)):
        window = values[max(0, i - 6):i + 1]
        moving_avg.append(round(sum(window) / len(window), 2) if window else None)

    for i, d in enumerate(data):
        d["moving_avg_7d"] = moving_avg[i]

    return {
        "metric": metric_name,
        "days": days,
        "count": len(data),
        "data": data,
    }
```

- [ ] **Step 4: Scope `/api/readiness`, `/api/recommendations`, `/api/recommendations/rules-only`**

```python
@app.get("/api/readiness")
async def api_readiness():
    """Get the composite readiness/recovery score (0-100)."""
    person_id = await get_primary_person_id()
    try:
        return await compute_readiness(person_id)
    except Exception as e:
        logger.error("Readiness scoring failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to compute readiness score")


@app.get("/api/recommendations")
async def api_recommendations(refresh: bool = Query(default=False)):
    """Get AI-powered health recommendations."""
    person_id = await get_primary_person_id()
    try:
        return await get_recommendations(person_id, force=refresh)
    except Exception as e:
        logger.error("Recommendations failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations")


@app.get("/api/recommendations/rules-only")
async def api_rules_only():
    """Get rules engine output without LLM."""
    person_id = await get_primary_person_id()
    return await get_rules_only(person_id)
```

These are complete replacements for the three existing route bodies (each currently a thin wrapper around the corresponding `recommendations.py`/`readiness.py` call, shown with their existing docstrings and, for `api_recommendations`, its existing try/except preserved).

- [ ] **Step 5: Scope `/api/correlations` (extends the spec's audit)**

```python
@app.get("/api/correlations")
async def api_correlations(
    metrics: str = Query(..., description="Comma-separated metric names, e.g. sleep_duration,hrv"),
    days: int = Query(default=30, ge=1, le=365),
    lag: int = Query(default=0, ge=-365, le=365, description="Calendar days to shift each row metric forward before joining"),
    min_pairs: int = Query(default=5, ge=2, description="Minimum aligned pairs required to report r instead of null"),
):
    """Ad-hoc cross-metric correlation matrix. See recommendations.get_metric
    for the person-scoping this endpoint's own inline query mirrors."""
    metric_names = [m.strip() for m in metrics.split(",") if m.strip()]
    if not metric_names:
        raise HTTPException(status_code=400, detail="metrics parameter must contain at least one metric name")

    unknown = [m for m in metric_names if m not in METRIC_TABLES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown metric(s): {', '.join(unknown)}. Valid: {', '.join(sorted(METRIC_TABLES))}",
        )

    person_id = await get_primary_person_id()

    db = await get_db()
    try:
        series: dict[str, dict[str, float]] = {}
        for name in set(metric_names):
            table, column = METRIC_TABLES[name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] "
                f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
                (person_id, f"-{days} days"),
            )
            rows = await cursor.fetchall()
            series[name] = {row["date"]: row["value"] for row in rows if row["value"] is not None}
    finally:
        await db.close()

    cells = [
        [compute_cell(series[row_name], series[col_name], lag, min_pairs) for col_name in metric_names]
        for row_name in metric_names
    ]

    return {
        "metrics": metric_names,
        "days": days,
        "lag": lag,
        "min_pairs": min_pairs,
        "cells": cells,
    }
```

- [ ] **Step 6: Thread `person_id` through `goals.py`'s `compute_progress`**

In `vitalforge-dashboard/goals.py`:

```python
async def compute_progress(
    table: str, column: str, person_id: int, target_value: float, target_date: str | None, days: int = 90
) -> GoalProgress:
    """ETA-to-goal from the metric's own recent trend. See its module
    docstring above for the per-row-vs-per-day caveat, unchanged by this
    signature addition."""
    data = await get_metric(table, column, person_id, days=days)
    latest_value = data[-1]["value"] if data else None
    slope = trend_slope(data) if data else None
    # ... rest of function body unchanged, operating on `data`/`latest_value`/`slope` as before
```

- [ ] **Step 7: Update `_goal_progress`/`_goal_out` and all 5 goal routes in `app.py`**

```python
async def _goal_progress(goal: dict, person_id: int) -> GoalProgress | None:
    mapping = METRIC_TABLES.get(goal["metric"])
    if mapping is None:
        return None
    table, column = mapping
    return await compute_progress(table, column, person_id, goal["target_value"], goal["target_date"])


async def _goal_out(goal: dict, person_id: int) -> GoalOut:
    return GoalOut(**goal, progress=await _goal_progress(goal, person_id))
```

Update every call site of `_goal_out` in the 5 goal routes to resolve and pass `person_id`:

```python
@app.post("/api/goals", status_code=201)
async def create_goal_route(data: GoalCreate, request: Request):
    identity = await require_account_identity(request)
    _validate_goal_metric(data.metric)
    goal_id = await create_goal(identity.user_id, data)
    goal = await get_goal(goal_id)
    person_id = await get_primary_person_id()
    return await _goal_out(goal, person_id)


@app.get("/api/goals")
async def list_goals_route(request: Request):
    identity = await require_account_identity(request)
    goals = await list_goals(identity.user_id)
    person_id = await get_primary_person_id()
    return [await _goal_out(goal, person_id) for goal in goals]


@app.get("/api/goals/{goal_id}")
async def get_goal_route(goal_id: int, request: Request):
    goal = await _owned_goal_or_404(request, goal_id)
    person_id = await get_primary_person_id()
    return await _goal_out(goal, person_id)


@app.patch("/api/goals/{goal_id}")
async def patch_goal_route(goal_id: int, data: GoalUpdate, request: Request):
    await _owned_goal_or_404(request, goal_id)
    _validate_goal_metric(data.metric)
    updated = await update_goal(goal_id, data)
    person_id = await get_primary_person_id()
    return await _goal_out(updated, person_id)
```

(`delete_goal_route` is unaffected — it never calls `_goal_out`.)

- [ ] **Step 8: Update the tests**

In `tests/test_dashboard_api.py`, `tests/test_correlations_api.py`, and `tests/test_goals.py`: any test that seeds a metric row directly via SQL must add `person_id` to the `INSERT`, resolved via `get_primary_person_id()` — same mechanical pattern as Task 5 Step 8. Any test that calls `compute_progress` directly must add the new `person_id` positional argument.

- [ ] **Step 9: Run the tests**

Run: `pytest tests/test_dashboard_api.py tests/test_correlations_api.py tests/test_goals.py -v`
Expected: all PASS.

Run: `pytest -q` (full suite, minus playwright)
Expected: **zero failures**. This is the first point in the plan where the entire non-playwright suite must be green again — confirm it before moving on.

- [ ] **Step 10: Lint and commit**

```bash
ruff check vitalforge-dashboard/app.py vitalforge-dashboard/goals.py tests/test_dashboard_api.py tests/test_correlations_api.py tests/test_goals.py
git add vitalforge-dashboard/app.py vitalforge-dashboard/goals.py tests/test_dashboard_api.py tests/test_correlations_api.py tests/test_goals.py
git commit -m "feat: scope dashboard routes (metrics, correlations, readiness, recommendations, goals) by person"
```

---

### Task 7: Documentation and final regression

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Interfaces:** none — this task has no code interfaces, only prose accuracy.

- [ ] **Step 1: Fix the repo-layout comment in `CLAUDE.md`**

Find:

```
  database.py             # aiosqlite connection + schema (CREATE TABLE IF NOT EXISTS, no migrations)
```

Replace with:

```
  database.py             # aiosqlite connection + schema; migrations.py runs one-shot schema
                           # migrations on top of it (001-person-id-rebuild, Phase 1)
```

- [ ] **Step 2: Extend the `METRIC_TABLES` convention bullet in `CLAUDE.md`**

Find the existing bullet under "Conventions observed in the existing code":

```
- Table names in `shared/database.py` map to metric keys via `METRIC_TABLES` in
  `vitalforge-dashboard/app.py:30-44` — when adding a new synced metric, you must update
  `shared/database.py` (schema), `sync.py` (populate), and this `METRIC_TABLES` dict
  (expose via `/api/metrics/{name}`) together, or the metric silently won't be queryable.
```

Append a sentence:

```
  If the new metric table is created before a future schema rebuild ships, it must also be
  added by name to `shared/migrations.py`'s `_REBUILD_TABLES` list — that list derives its
  column shapes from the live schema rather than duplicating them, so it only ever needs the
  table's name, not its columns.
```

- [ ] **Step 3: Add an upgrade/rollback section to `README.md`**

Add a new section (placement: wherever `README.md` documents deployment/upgrades today — a new `## Upgrading` section is appropriate if none exists):

```markdown
## Upgrading

Some releases (starting with the `001-person-id-rebuild` schema migration) change the
database schema in a way that is not safely readable by an older image. For these:

1. **Stop both services before upgrading**: `docker compose down`, not a rolling restart —
   an old container must never run against the new schema mid-upgrade.
2. Pull/build the new images, then `docker compose up`.
3. The new image takes an automatic pre-migration snapshot (`fitness.pre-001-person-id.db`,
   next to `fitness.db` in the `vitalforge-data` volume) before it changes anything, verified
   with a SQLite integrity check. If you also want to rename the primary person away from the
   default (the first admin's username, slugified), set `VITALFORGE_PRIMARY_PERSON` in `.env`
   **before** this upgrade — it is read once, during the one-shot migration.
4. If the migration fails (most commonly: insufficient free space for the snapshot), the
   container will restart-loop with an error naming the cause and the fix. Free up space and
   restart, or — after taking your own volume-level backup — set
   `VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1` to proceed without the automatic snapshot.
5. **Rollback**: stop both services, replace `fitness.db` with the pre-migration snapshot
   (removing any `-wal`/`-shm` sidecar files), redeploy the previous images.
6. Once the upgrade is verified good and at least 7 days have passed, delete
   `fitness.pre-001-person-id.db` — it is a full second copy of your health data and is not
   cleaned up automatically.
```

- [ ] **Step 4: Full regression run**

```bash
ruff check .
pytest tests/test_docs_drift.py -v
pytest -q
```

Expected: all clean — `test_docs_drift.py` unaffected (Global Constraints explains why), full suite green. Do not proceed to review with any of the three failing.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document the person-id-rebuild migration and upgrade procedure"
```

---

## After this plan lands

Phase 1 is done when: the full non-playwright suite is green, `ruff check .` is clean, and a manual smoke test (`docker compose up --build`, then exercise the weight and dashboard UIs against a copy of a real `fitness.db`) shows identical numbers to before the upgrade. Phase 2 (access control, `/p/{slug}/` routing, ingest routing, unmounting the legacy un-scoped API paths) gets its own plan once this one is reviewed and merged — do not start it from inside this plan's execution.
