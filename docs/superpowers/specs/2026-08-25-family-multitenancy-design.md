# Family / Multi-Person Dashboard — True Multi-Tenancy Design

**Date:** 2026-08-25 (revised 2026-08-25 after adversarial review; decisions applied
2026-08-26 — see Appendix C)
**Status:** Design proposal — NOT approved, NOT scheduled. The thirteen questions that
previously blocked implementation planning have all been answered by the deployment's owner;
section (i) now records those decisions and their rationale. Answering them does not approve
or schedule the work.
**Scope:** "Option C" — a real `person_id` dimension on every metric table, enabling
cross-person queries. Explicitly scoped as a large, multi-week, multi-PR effort.

---

## 0. Grounding — what was verified against the current code

Every load-bearing claim below was read out of the working tree at commit `3560341`, not
assumed. Where the feasibility brief that produced this task was **wrong**, that is called
out explicitly. Where a claim in this document is *not* verified, it says so in the same
sentence — see §d.5's garth filename caveat and §c.1's `aiosqlite` scoping paragraph, which
are the only two such claims left.

### 0.1 Verified facts

| Claim | Verified in | Result |
|---|---|---|
| 10 date-keyed metric tables use `date TEXT PRIMARY KEY` alone | `shared/database.py:97-234` | **Confirmed.** `sleep`, `resting_hr`, `hrv`, `body_battery`, `stress`, `vo2max`, `weight_history`, `training_load`, `steps`, `active_calories` |
| `upsert()` is `INSERT OR REPLACE` keyed on `date` | `vitalforge-dashboard/sync.py:25-40` | **Confirmed.** Two people, same date → silent overwrite |
| `weight_log` is id-keyed `AUTOINCREMENT` | `shared/database.py:76-90` | **Confirmed.** The `AUTOINCREMENT` matters — see §b.3 and §i Q9 |
| Migration approach is additive-only by deliberate design | `shared/database.py:8-19`, `docs/prp/00-design.md` §5.4 | **Confirmed.** §5.4's closing paragraph names table-rewrite as the residual risk this design avoids |
| A **constant** default is metadata-only, not a rewrite | `shared/database.py:36-41` | **Confirmed.** `_USERS_ADDITIVE_COLUMNS` is literally `"session_version INTEGER NOT NULL DEFAULT 1"`, and the comment above it states the rule precisely |
| Single global Garmin credential + module singleton | `shared/garmin_client.py:12,34-38` | **Confirmed.** `_client: Garmin \| None`, `os.environ["GARMIN_EMAIL"]` |
| `garmin_client` has **zero** DB coupling and is fully synchronous | `shared/garmin_client.py:1-10` | **Confirmed.** Imports are `logging`, `os`, `datetime`, `pathlib`, `garminconnect` only |
| `GARTH_TOKEN_DIR.mkdir()` sets **no mode** | `shared/garmin_client.py:19` | **Confirmed.** `mkdir(parents=True, exist_ok=True)` → `0o777 & ~umask`, and `exist_ok=True` does not re-chmod |
| Both lifespans call `authenticate()` with **no arguments** | `vitalforge-dashboard/app.py:63`, `vitalforge-weight/app.py:42`, `vitalforge-dashboard/sync.py:243` | **Confirmed.** All three are bare calls |
| One-way hashed API tokens, no reversible secrets today | `shared/auth.py:369,398` | **Confirmed.** `hashlib.sha256(...).hexdigest()` both at issue and at verify |
| Documented 429 rate-limit scar | `shared/garmin_client.py:22-33` | **Confirmed.** 2026-08-22 incident, forced re-login per request |
| `auth_migrations` one-time-marker pattern exists | `shared/auth.py:206-211, 339-386` | **Confirmed.** `BEGIN IMMEDIATE` → check marker → work → insert marker → single `commit()` |
| SQLite foreign keys are **not** enabled | `shared/auth.py:1238-1242` | **Confirmed.** `REFERENCES` is decorative; deletes are manual and in-transaction |
| `init_db()` runs before the app serves any request | both `app.py` lifespans; `docs/prp/00-design.md:1563-1565` | **Confirmed** |
| `init_db()` holds **one** connection for its whole body | `shared/database.py:73-254` | **Confirmed.** Opened at `:73`, single `commit()` at `:252`, closed at `:254`. This constrains §c.4 |
| `_add_columns` commits **inside** its per-column loop | `shared/database.py:53-59` | **Confirmed.** `await db.commit()` at `:56`. This constrains §c.6's atomicity claim |
| `idx_weight_log_timestamp` is created **unconditionally** | `shared/database.py:250` | **Confirmed.** Constrains §b.3 / §c.5 — see finding 1.8 in Appendix C |
| Both services race on the same DB file with no ordering | `shared/database.py:44-51` docstring; `docs/prp/00-design.md:1580-1584` | **Confirmed.** This is why `_add_columns` is attempt-and-swallow, not `PRAGMA`-then-act |
| No `depends_on`; both services `restart: unless-stopped` | `docker-compose.yml:12,24` | **Confirmed.** A failing lifespan is an unbounded restart loop — see §c.7 |
| `_RESERVED_USERNAMES` is the existing name-reservation pattern | `shared/auth.py:61`, enforced at `:1155` | **Confirmed.** §f.4 mirrors it as `_RESERVED_SLUGS` |
| `recommendations.py` holds a single-slot module-level cache | `vitalforge-dashboard/recommendations.py:13-14, 486-516` | **Confirmed.** See §i Q11 and the phase-1 note in §(h) |
| `cryptography` is **not** a dependency | both `requirements.txt` | **Confirmed.** Encrypted-at-rest credentials means a new pinned dep in both services |

### 0.2 Two corrections to the brief

**Correction 1 — `weight_log` needs more than an additive column.**

The brief states an additive `person_id` "suffices" for `weight_log` because it is id-keyed.
That is true *about the table rebuild* and false *about correctness*. The dedup query at
`vitalforge-weight/app.py:243-261` selects on `timestamp` + `ABS(weight_grams - ?) <= 50`
within a ±60 s window with **no person predicate at all**:

```sql
SELECT id, weight_lbs, ... FROM weight_log
WHERE timestamp >= ?
  AND ABS(weight_grams - ?) <= ?
  AND julianday(timestamp) >= julianday(?, ?)
  AND julianday(timestamp) <= julianday(?, ?)
ORDER BY timestamp DESC LIMIT 1
```

Add `person_id` additively and change nothing else, and two family members who step on the
scale within 60 seconds at weights within 50 g of each other **silently merge into one row** —
and worse, the enrichment branch then writes one person's body-composition onto the other's
record and pushes it to the wrong Garmin account. This is exactly the class of bug constraint 1
exists to prevent, occurring on the table the brief says is safe.

`weight_log` therefore needs three changes, not one: the additive column, a `person_id = ?`
predicate in the dedup `SELECT`, and `idx_weight_log_timestamp` replaced by
`idx_weight_log_person_timestamp ON weight_log(person_id, timestamp)` — the current
single-column index becomes the wrong shape once every query filters on person first.

**Correction 2 — the read-side blast radius is much smaller than "the whole dashboard,"
but it is bigger than the brief's SELECT-only view.**

`recommendations.py` does **not** read metric tables directly beyond one query, and that query
is the same parameterized shape as the dashboard's. But an inventory of *read* statements is
not an inventory of *person-aware* statements: a write that omits `person_id` is strictly more
dangerous than a read that omits it, because the read returns visibly-wrong data while the
write produces a row that is invisible to every correct read afterwards.

The corrected inventory is **13 SQL statements plus a function-signature chain**. It is
grouped by why each one matters, because a reviewer working the list needs to know which
omissions fail loudly and which fail silently.

**Group A — reads that must gain a person predicate (wrong data if missed):**

| # | File:line | Statement | Change |
|---|---|---|---|
| 1 | `vitalforge-dashboard/app.py:145-148` | `SELECT date, [col] FROM [table] WHERE date >= ...` | add `AND person_id = ?` |
| 2 | `vitalforge-dashboard/recommendations.py:25-28` | same shape | add `AND person_id = ?` |
| 3 | `vitalforge-dashboard/sync.py:18` | `SELECT date FROM [table]` | add `WHERE person_id = ?` — **see §e.4, this one is a live bug if missed** |
| 4 | `vitalforge-dashboard/app.py:116` | `SELECT ... FROM sync_status WHERE id = 1` | key on `person_id` |
| 5 | `vitalforge-weight/app.py:418-419` | `/api/weight/recent` | add `WHERE person_id = ?` |
| 6 | `vitalforge-weight/app.py:442-444` | `/api/weight/trend` | add `AND person_id = ?` |

**Group B — writes that must carry `person_id` (silently lost data if missed):**

| # | File:line | Statement | Change |
|---|---|---|---|
| 7 | `vitalforge-dashboard/sync.py:34-37` | `INSERT OR REPLACE INTO [table] (date, ...)` | add `person_id` to the column list |
| 8 | `vitalforge-dashboard/sync.py:290-293` | `INSERT OR REPLACE INTO sync_status (id, ...) VALUES (1, ...)` | key on `person_id` |
| 9 | `vitalforge-weight/app.py:278-293` | `INSERT INTO weight_log (weight_lbs, ..., source) VALUES (...)` | **add `person_id` to the column list.** The original version of this document omitted this statement while claiming its list was complete. Without it every new weight row lands with `person_id` NULL and is instantly invisible to #5, #6, #10 and #13 — total, silent loss of weight logging on a schema that looks correct |

**Group C — statements that are safe *only because* an upstream statement was fixed
(list them so the safety is checked, not inferred):**

| # | File:line | Statement | Why it is safe, and what makes it unsafe |
|---|---|---|---|
| 10 | `vitalforge-weight/app.py:243-261` | dedup `SELECT` | add `AND person_id = ?` (Correction 1). Everything below depends on this |
| 11 | `vitalforge-weight/app.py:297-300` | enrichment `UPDATE weight_log SET ... WHERE id = ?` | Safe **given** #10, because `existing["id"]` then provably belongs to the request's person. Unsafe the moment #10 regresses. Do not add a redundant `AND person_id = ?` here — add a test that asserts #10's predicate exists |
| 12 | `vitalforge-weight/app.py:371` | `UPDATE weight_log SET synced_to_garmin = ? WHERE id = ?` | Same reasoning as #11 |
| 13 | `vitalforge-weight/app.py:459` | `DELETE FROM weight_log WHERE id = ?` | **Not** safe by inference — `weight_id` comes straight off the URL path. Add `AND person_id = ?` (IDOR guard) |

**Group D — the function-signature chain.** Statements #3 and #7 are two lines of SQL inside
`get_synced_dates()` and `upsert()`, but making them person-aware threads a `person_id`
parameter through seven functions that do not take one today. A review pass that checks only
SQL will pass while the code does not compile, or worse, compiles with a default:

- `vitalforge-dashboard/sync.py:14` `get_synced_dates(table)`
- `vitalforge-dashboard/sync.py:25` `upsert(table, date, **columns)`
- `vitalforge-dashboard/sync.py:55` `sync_date(date_str)`
- `vitalforge-dashboard/sync.py:202` `sync_weight_history(start_date, end_date)`
- `vitalforge-dashboard/sync.py:236` `run_sync(days)`
- `vitalforge-dashboard/sync.py:301` `scheduled_sync(lock)`
- `vitalforge-dashboard/app.py:97` `trigger_sync(days)`

**None of these may acquire a default `person_id`.** A default turns T5 (wrong-person Garmin
write, §d.3) from a compile error into a one-typo silent bug.

So the review checklist is two lists, not one: *these 13 statements* **and** *every function
signature on the path from route to SQL*. Neither alone is sufficient.

### 0.3 A table the brief did not mention

`sync_status` (`shared/database.py:236-243`) is a **singleton**:

```sql
CREATE TABLE IF NOT EXISTS sync_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ...
)
```

With N people it must become per-person, and changing the PK from a `CHECK (id = 1)`
singleton to `person_id` is a rebuild. **The rebuild set is 11 tables, not 10.**

---

## (a) The person / user / grant data model

### a.1 The core distinction

A **person** is a subject of health data. A **user** is a login. These are deliberately
decoupled, because the driving requirement — "a parent manages a child's data without the
child having an account" — is precisely the case where a person has *zero* users.

Cardinality is many-to-many in both directions:

- A person may have **0** app users (a child, a tracked relative, an ex-account whose data is
  retained), **1** (the normal case), or **N** (spouses who both log in and both want to see
  the household).
- A user may have access to 0, 1, or N persons.

```
users (login)  ──< person_grants >──  persons (data subject)
                                             │
                                             ├──< garmin_links (0..1 per person)
                                             ├──< api_tokens.person_id (0..N, ingest routing)
                                             └──< all 11 metric tables + weight_log
```

### a.2 DDL

```sql
CREATE TABLE IF NOT EXISTS persons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,          -- URL-safe; validated in code, see §f.4
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    archived_at  TEXT,                          -- soft-delete; NULL = active
    is_primary   INTEGER NOT NULL DEFAULT 0     -- durable marker, see §g.1 / threat T5
);

-- At most one primary person, enforced by the database rather than by convention.
CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_primary
    ON persons(is_primary) WHERE is_primary = 1;

CREATE TABLE IF NOT EXISTS person_grants (
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    access     TEXT NOT NULL CHECK (access IN ('view', 'manage', 'own')),
    granted_at TEXT NOT NULL,
    granted_by INTEGER,                          -- users.id, nullable (migration-created)
    PRIMARY KEY (person_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_person_grants_user ON person_grants(user_id);
```

Note `REFERENCES` is declarative only — this project does not enable
`PRAGMA foreign_keys` (`shared/auth.py:1238-1240` says so explicitly and deletes children
manually). The declaration is documentation; §f.5 specifies the manual cascade.

`is_primary` exists because "the primary person" is consulted by the legacy-token-store
fallback (§d.5) and by nothing else that can tolerate being wrong. Deriving it as
`SELECT id FROM persons ORDER BY id LIMIT 1` is what the first draft did, and §f.6 permits
archiving persons — so archiving person 1 would silently promote person 2, who would then read
person 1's Garmin tokens and push their weight to person 1's account. That is threat T5
created by the design's own convenience fallback. A durable column cannot drift that way.

Additive column on `users` (constant default, metadata-only, safe under the existing
`_add_columns` helper):

```sql
-- appended to _USERS_ADDITIVE_COLUMNS
"default_person_id INTEGER"
```

`default_person_id` is which person a user sees when no `person` is specified. Nullable:
NULL means "resolve to the single person this user can reach, or 400 if ambiguous." It is
used to build the initial redirect (§f.2) and **nowhere else** — never as an implicit fallback
inside a person-scoped data route. Since §f.8 drops the compatibility-alias layer entirely,
the redirect is now its only consumer.

### a.3 Access levels

Three levels, deliberately coarse. This is a household app, not an enterprise IAM system.

| Level | Can |
|---|---|
| `view` | Read this person's metrics, recommendations, weight history |
| `manage` | `view` + POST weight, trigger sync, edit display name, link/unlink Garmin |
| `own` | `manage` + archive the person + grant/revoke other users' access |

The existing `admin` role on `users` **bypasses grant checks entirely**, matching the
precedent already in the codebase at `shared/auth.py:1107` (`row["user_id"] != identity.user_id
and identity.role != "admin"`). Do not invent a second, inconsistent superuser story.

### a.4 Why a grant table rather than `persons.owner_user_id`

An owner column handles "one person, one login" and immediately fails the driving requirement.
A child person has no login to put in that column, and a spouse who should see the household
dashboard has no place to be recorded. The grant table costs one extra table and one extra
join; the owner column costs a redesign the first time the feature is actually used as
described.

---

## (b) Exact schema changes, per table

### b.1 The 10 date-keyed metric tables — full rebuild

Pattern, shown for `sleep`; identical in shape for `resting_hr`, `hrv`, `body_battery`,
`stress`, `vo2max`, `weight_history`, `training_load`, `steps`, `active_calories`.

**Before**

```sql
CREATE TABLE sleep (
    date TEXT PRIMARY KEY,
    duration_seconds INTEGER, deep_seconds INTEGER, light_seconds INTEGER,
    rem_seconds INTEGER, awake_seconds INTEGER, sleep_score INTEGER,
    avg_spo2 REAL, avg_respiration REAL
);
```

**After**

```sql
CREATE TABLE sleep (
    person_id INTEGER NOT NULL,
    date      TEXT NOT NULL,
    duration_seconds INTEGER, deep_seconds INTEGER, light_seconds INTEGER,
    rem_seconds INTEGER, awake_seconds INTEGER, sleep_score INTEGER,
    avg_spo2 REAL, avg_respiration REAL,
    PRIMARY KEY (person_id, date)
);
```

Column order matters: `person_id` first makes `(person_id, date)` a usable prefix for the
`WHERE person_id = ? AND date >= ?` shape every read query becomes. No secondary index is
needed — the PK *is* the index, and SQLite will use it for both the equality and the range.

`NOT NULL` on `person_id` is deliberate and enforceable: the rebuild backfills every row with
a literal, so there is no legacy-NULL case to tolerate. A nullable `person_id` would silently
permit orphan rows that every read query then misses.

**Checked and clear:** none of these 10 tables carries an `AUTOINCREMENT` id or an explicit
`CREATE INDEX`. The only two indexes in `shared/database.py` are `idx_api_tokens_user_id`
(`:201`) and `idx_weight_log_timestamp` (`:250`), neither on a rebuilt table. `DROP TABLE`
silently dropping indexes and resetting `sqlite_sequence` is a real hazard for
create/copy/drop/rename rebuilds in general and simply does not apply to this set — so the
rebuild needs no index-recreation or sequence-carry-forward step, and that is a checked fact
rather than an assumption. Both hazards **would** apply to `weight_log`, which is one more
reason not to rebuild it; the primary reason is that its PK does not need to change. See §b.3
and §i Q9.

**Why a rebuild and not `ALTER TABLE ADD COLUMN person_id`:** the PK must change. A nullable
additive `person_id` leaves `date` as the sole PK, so `INSERT OR REPLACE` (`sync.py:35`)
continues to key on `date` alone and person B's sync silently overwrites person A's row —
the exact failure this whole design exists to prevent. SQLite cannot alter a PRIMARY KEY in
place; the create/copy/drop/rename sequence is the only route.

### b.2 `sync_status` — rebuild (the 11th table)

**Before**

```sql
CREATE TABLE sync_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_sync_time TEXT, last_sync_result TEXT, last_sync_days INTEGER
);
```

**After**

```sql
CREATE TABLE sync_status (
    person_id      INTEGER PRIMARY KEY,
    last_sync_time TEXT,
    last_sync_result TEXT,
    last_sync_days INTEGER,
    backoff_until  TEXT       -- ISO8601; set on Garmin 429, see §(e)
);
```

The existing singleton row (`id = 1`) copies to `person_id = <primary person id>`. The
`CHECK (id = 1)` constraint disappears.

**`backoff_until` lands in migration 001, in phase 1, even though nothing writes it until
phase 4.** This is deliberate and worth stating because the first draft left it ambiguous:
`sync_status` is being rebuilt anyway, so adding the column costs nothing here, whereas adding
it later would either be a second `ALTER TABLE` (fine, but pointless churn) or tempt someone
into a second rebuild. It is NULL for every row until phase 4, and phase-1 code must not read
it. `_rebuild_sync_status` in §c.5 writes it explicitly as `NULL`.

### b.3 `weight_log` — additive column, but three changes

**Before**

```sql
CREATE TABLE weight_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weight_lbs REAL NOT NULL, weight_kg REAL NOT NULL, weight_grams INTEGER NOT NULL,
    timestamp TEXT NOT NULL, synced_to_garmin INTEGER DEFAULT 0,
    body_fat_pct REAL, body_water_pct REAL, muscle_pct REAL, bone_mass_kg REAL, source TEXT
);
CREATE INDEX idx_weight_log_timestamp ON weight_log(timestamp);
```

**After** — no rebuild. The id PK is unaffected.

```sql
-- 1. Additive column, nullable at DDL level, backfilled to the primary person by the
--    migration, then treated as NOT NULL by application code.
ALTER TABLE weight_log ADD COLUMN person_id INTEGER;
```

**Why nullable — the correct reason.** The first draft of this document claimed
`ADD COLUMN ... NOT NULL DEFAULT` "is exactly the table rewrite `shared/database.py:8-12`
warns against." **That is factually wrong and the repo disproves it:**
`_USERS_ADDITIVE_COLUMNS` at `shared/database.py:39-41` is literally
`"session_version INTEGER NOT NULL DEFAULT 1"`, and the comment at `:36-38` states the rule
exactly: a *constant* default is "a fast, metadata-only change, not a table rewrite." So
`ADD COLUMN person_id INTEGER NOT NULL DEFAULT 0` **is** mechanically available.

The confusion originates upstream, not here: `docs/prp/00-design.md:1596-1598` says "any
future migration that adds a defaulted column rewrites the table," which is the imprecise
statement; `shared/database.py:36-38` is the correct one. **Correcting `00-design.md:1596-1598`
is an explicit deliverable of phase 0** (Appendix B), because leaving a false mechanical claim
in the design corpus is how this error propagated into this document in the first place.

The real objection to `NOT NULL DEFAULT 0` is **semantic**: person 0 does not exist. A
defaulted sentinel produces rows that are structurally valid and referentially meaningless,
and it destroys the one audit query that catches Group-B omissions from §0.2 —
`SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL`. Given that finding #9 in §0.2 is
exactly an INSERT that forgets `person_id`, keeping that query meaningful is worth more than
the DDL-level `NOT NULL`. Nullable-plus-backfill it is, and the reason is auditability, not
mechanics.

```sql
-- 2. Index shape change (inside the same migration transaction as the backfill).
DROP INDEX IF EXISTS idx_weight_log_timestamp;
CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp
    ON weight_log(person_id, timestamp);
```

**`shared/database.py:250` must change too.** Today it unconditionally runs
`CREATE INDEX IF NOT EXISTS idx_weight_log_timestamp ON weight_log(timestamp)`. If only the
migration is changed, a **fresh** install permanently carries the old single-column index and
never gets the `(person_id, timestamp)` one that Correction 1 requires — fresh and migrated
DBs diverge silently. The fix is split by role:

- `init_db()` line 250 becomes the **new** index — `CREATE INDEX IF NOT EXISTS
  idx_weight_log_person_timestamp ON weight_log(person_id, timestamp)`. It runs after
  `_add_columns` has added `person_id` (line 94 precedes line 250), so the column exists.
- The `DROP INDEX IF EXISTS idx_weight_log_timestamp` for the **legacy** name lives in the
  migration only. A fresh DB never had it; `IF EXISTS` makes the migration's drop a no-op on
  fresh DBs that somehow reach it.

3. **Code change** at `vitalforge-weight/app.py:243-261` — add `AND person_id = ?` to the
dedup `SELECT`, bound to the request's resolved person. Without this the index change is
cosmetic and the cross-person merge described in §0.2 remains live.

Add `"person_id INTEGER"` to `_WEIGHT_LOG_ADDITIVE_COLUMNS` so a fresh DB and an upgraded DB
converge through the existing helper.

### b.4 New tables

`persons`, `person_grants` (§a.2), `garmin_links` (§d.4), `schema_migrations` (§c.3). All
plain `CREATE TABLE IF NOT EXISTS` — no rebuild, no risk. Plus the additive
`api_tokens.person_id` (§f.7), which is a constant-default-free nullable column.

**The schema-version guard (§i Q12, decided: build it in phase 0) adds no table and no
column.** It reads `schema_migrations`, which already exists for the runner's sake — see the
guard's definition at the end of §c.3. That is deliberate: a separate `user_version` pragma or
a `schema_version` row would be a second source of truth that can disagree with the marker
table, and the marker table is the one the migration itself writes transactionally.

### b.5 Summary of the rebuild set

| Table | Action |
|---|---|
| `sleep`, `resting_hr`, `hrv`, `body_battery`, `stress`, `vo2max`, `weight_history`, `training_load`, `steps`, `active_calories` | **Rebuild** → PK `(person_id, date)` |
| `sync_status` | **Rebuild** → PK `person_id`, plus `backoff_until` |
| `activities` | **Rebuild**, in migration **002** → adds `person_id`, `UNIQUE(file_sha256)` → `UNIQUE(person_id, file_sha256)`, index swap |
| `weight_log` | Additive column + index swap (both DDL sites) + dedup predicate |
| `users` | Additive `default_person_id` |
| `api_tokens` | Additive `person_id` (phase 2, §f.7) |
| `persons`, `person_grants`, `garmin_links`, `schema_migrations` | New |

`activities` was added by the FIT-import feature after this spec's original audit and was
missed by it. It is a **rebuild**, not an additive column like `weight_log`, for one reason:
`file_sha256` carried a global `UNIQUE`, and SQLite cannot alter a constraint in place. Left
global, one person importing a FIT file would make that file permanently un-importable for
everyone else, and scoping only the dedup `SELECT` would convert that into an `IntegrityError`
instead of the clean duplicate response `/api/import/activity` returns. `id INTEGER PRIMARY KEY
AUTOINCREMENT` is preserved, so existing activity URLs keep working.

It ships as migration **002**, not as extra work inside 001, because 001 had already been
applied to development databases by the time the gap was found. Those carry the 001 marker, so
the runner skips 001 entirely on their next boot — anything folded into 001 after the fact
would silently never run there while the routes assumed it had. Migrations are immutable once
written, even before release.

`goals` stays `user_id`-scoped and is deliberately NOT in the rebuild set: a goal belongs to
the account that set it, not to the person whose metrics it measures. Its progress is computed
against person-scoped metric tables, so the person resolution happens at the call site
(`_goal_progress`) rather than in the row. Revisit in phase 2 if one account needs separate
goals per person.
| `auth_migrations` | Unchanged |

---

## (c) Migration execution design — the highest-risk section

### c.1 The question that determines everything, and its answer

The brief frames the risk as "container killed mid-rewrite leaves a torn schema." Whether that
is true depends on one thing: **is a create/copy/drop/rename rebuild committable as a single
atomic transaction in SQLite, alongside its completion marker?**

**This was verified empirically, not assumed.** Exact configuration tested: stdlib `sqlite3`
3.50.2, `journal_mode=WAL`, `PRAGMA foreign_keys` off, **`isolation_level = None`
(autocommit) with an explicit `BEGIN IMMEDIATE`**:

```
BEGIN IMMEDIATE
  CREATE TABLE sleep_new (person_id, date, v, PRIMARY KEY (person_id, date))
  INSERT INTO sleep_new SELECT 1, date, v FROM sleep
  DROP TABLE sleep
  ALTER TABLE sleep_new RENAME TO sleep
  INSERT INTO schema_migrations VALUES ('m1', ...)
ROLLBACK
→ sleep DDL: CREATE TABLE sleep (date TEXT PRIMARY KEY, v INTEGER)   ← original, byte-identical
→ rows:      [('2026-01-01', 5)]                                      ← intact
→ schema_migrations: does not exist                                   ← marker correctly absent
```

The same sequence with `COMMIT` produces the new schema with data correctly carried across.

**SQLite DDL is fully transactional, including `DROP TABLE` and `ALTER TABLE ... RENAME`.**
A kill mid-rebuild rolls the *rebuild* back — old schema, old data, no marker. There is no
torn state to recover from and no repair path to write. The "container killed mid-migration"
window does not collapse to "small"; for the rebuild it collapses to **zero, at the database
level**. §c.6 states precisely which part of the migration this covers and which part it does
not; the guarantee is narrower than "the migration is atomic," and the difference matters.

This is the single most important fact in this document, and it changes the design: the
elaborate resumable-migration machinery the brief anticipates is not needed. What *is* needed
is much smaller and is specified below.

**Scope of that verification, stated precisely — this matters.** The test ran with
`isolation_level = None`. `get_db()` (`shared/database.py:62-68`) does **not** set
`isolation_level`, so production connections run in Python's *legacy* mode (`""`), where the
driver manages an implicit `BEGIN` around DML. The repo proves explicit `BEGIN IMMEDIATE` +
`commit()` works in legacy mode for **DML** (`bootstrap_migrated_token`, `admin_update_user`,
the weight dedup transaction). It does **not** prove it for `CREATE`/`DROP`/`ALTER … RENAME`.

Two consequences, both of which the design absorbs rather than hand-waves:

1. **The migration connection must explicitly set `isolation_level = None`**, so the code runs
   in exactly the configuration that was verified rather than one assumed to be equivalent.
   This is specified in `run_migration` below and is not optional.
2. **Re-verify through `aiosqlite` before phase 0 merges.** `aiosqlite` is a thin thread
   wrapper delegating to stdlib `sqlite3`, so the semantics should carry — but "should" is not
   what this section is allowed to rest on. §c.8 lists this as a gating test. If DDL turns out
   *not* to roll back in whatever configuration is finally chosen, the recommendation in §c.6
   inverts to the per-table-marker variant (already written there) and Appendix A's central
   claim needs rewriting. Settle this first; everything downstream of it is cheap by
   comparison.

### c.2 What actually remains risky

Four things survive the transactionality result, and they are what the runner must handle.

1. **The two-service startup race.** Both containers call `init_db()` against the same file
   with no `depends_on` ordering (`shared/database.py:44-51`, `docker-compose.yml`).
   `ADD COLUMN` is idempotent by attempt-and-swallow; a rebuild is not — running it twice
   would rebuild an already-correct table, and worse, two concurrent rebuilds could interleave.
   **This, not the kill case, is the race the runner exists to solve.**
2. **Lock timeout.** `aiosqlite` inherits Python `sqlite3`'s 5-second default busy timeout.
   The loser of `BEGIN IMMEDIATE` must wait for the winner's *entire* rebuild, which for 11
   tables may exceed 5 s. Without a raised timeout the loser gets `database is locked`, its
   lifespan fails, and Docker restarts it — recoverable but noisy and, under
   `restart: unless-stopped`, potentially indefinite.
3. **Connection lifetime inside `init_db()`.** `init_db()` holds one connection from
   `shared/database.py:73` to `:254`. A second connection opening `BEGIN IMMEDIATE` while the
   first is still open is a self-deadlock waiting to happen. §c.4 resolves this structurally.
4. **Backward incompatibility.** Unlike every prior migration in this repo, the result is
   **not** readable by the previous image *correctly*. §c.7 covers this; it is the sharpest
   remaining edge.

### c.3 The migration runner

A direct generalization of the `auth_migrations` pattern already proven in
`bootstrap_migrated_token()` (`shared/auth.py:339-386`). Same primitive, same reasoning,
new table so the two concerns stay separable.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    name         TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
```

```python
# shared/migrations.py  (new module — keeps shared/database.py under the 800-line bar
#                        and gives the runner its own test surface)

async def run_migration(name: str, apply: Callable[[aiosqlite.Connection], Awaitable[None]]):
    """Run one migration exactly once, atomically, across both services.

    MUST be called with no other connection from this process open against the
    same file -- see init_db()'s ordering comment. This function opens its own
    connection and takes the write lock; if init_db()'s connection were still
    open AND holding a write transaction, BEGIN IMMEDIATE below would block on a
    lock held by the same coroutine that will never yield, wait out the 30s
    busy_timeout, and raise `database is locked` -- which restart: unless-stopped
    turns into a permanent boot loop. init_db() today runs only DDL, so that
    trap is latent rather than live; adding a single seed INSERT to init_db()
    (which this very feature invites) would arm it. Closing first removes the
    question rather than relying on an answer about SQLite/CPython internals --
    the same discipline §c.1 applies to DDL rollback.

    The marker is committed in the SAME transaction as the schema change, so the
    two can never disagree. Verified: SQLite rolls back CREATE/DROP/ALTER RENAME
    together with the marker INSERT, leaving the original schema byte-identical.

    Concurrency: both services call this during startup against the same file with
    no ordering between them. BEGIN IMMEDIATE serializes them -- the loser blocks
    until the winner commits, then observes the marker and no-ops. This is why the
    marker check must be INSIDE the transaction: a pre-check would be TOCTOU-racy in
    exactly the way shared/database.py:44-51 documents for _add_columns.

    `database is locked` is NOT swallowed, matching _add_columns' policy -- a
    container that cannot migrate must fail its lifespan and be restarted rather
    than serve traffic against a schema it did not verify.
    """
    db = await get_db()
    try:
        # Run in the configuration §c.1 actually verified DDL rollback under --
        # autocommit + an explicit BEGIN IMMEDIATE -- rather than get_db()'s
        # inherited legacy isolation_level (""), where the driver's implicit
        # transaction management around DML is not proven to leave DDL inside
        # the explicit transaction. Do not remove this line on the grounds that
        # "the rest of the codebase doesn't set it": the rest of the codebase
        # only runs DML inside its explicit transactions.
        db.isolation_level = None
        # 30s, not the sqlite3 default of 5s: the loser of the race below waits for
        # the winner's entire 11-table rebuild, which can exceed 5s on a DB with
        # years of history.
        await db.execute("PRAGMA busy_timeout = 30000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            done = await (await db.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
            )).fetchone()
            if done is not None:
                await db.rollback()
                return
            started = time.monotonic()
            await apply(db)
            await db.execute(
                "INSERT INTO schema_migrations (name, completed_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        except BaseException:
            # Explicit, matching shared/auth.py:1186-1197 and :1226-1237. Closing
            # the connection in `finally` would also discard the transaction, but
            # relying on that is the kind of implicit behavior this section exists
            # to avoid depending on. BaseException, not Exception, so a cancelled
            # lifespan also rolls back rather than leaving a held lock.
            await db.rollback()
            raise
        # Logged at WARNING with elapsed time so §c.6's "measure before switching
        # to per-table transactions" has an actual instrument. Without this the
        # ~10s trigger stated there is unobservable in production.
        logger.warning(
            "Applied schema migration %s in %.2fs", name, time.monotonic() - started
        )
    finally:
        await db.close()
```

`schema_migrations` itself is created by `CREATE TABLE IF NOT EXISTS` in `init_db()` before
any `run_migration` call — that statement is idempotent and race-safe on its own.

**`busy_timeout` must be raised on *every* startup connection, not just this one.** Raising it
only inside `run_migration` leaves a real gap: service B can still be at `init_db()` step 2
(`_add_columns`) on its own `get_db()` connection, at the stdlib 5 s default, while service A
holds the write lock for its full 11-table rebuild at step 4. B's `ALTER TABLE ADD COLUMN`
blocks, times out at 5 s, and `database is locked` propagates by design — B's lifespan fails
and Docker restarts it, potentially in a loop for as long as the rebuild takes. The fix is one
line in `get_db()` itself:

```python
await db.execute("PRAGMA busy_timeout = 30000")   # alongside the existing journal_mode=WAL
```

This is a strict improvement for the request path too (today a 5 s stall becomes a 500), and
it does not weaken the deliberate "let `database is locked` propagate" policy — it only moves
the threshold to a value that reflects how long a legitimate writer can now hold the lock.

**Be honest about what the raise buys: it moves the threshold, it does not remove the loop.**
A rebuild that exceeds 30 s produces the identical restart loop one order of magnitude later.
Two things reduce the exposure rather than relabel it:

- **A cheap shape pre-check inside `_add_columns`.** Today the loser blocks on an
  `ALTER TABLE ADD COLUMN` that is going to fail `duplicate column name` and be swallowed
  anyway. A `PRAGMA table_info` read costs nothing and, in WAL mode, does not block behind a
  writer — so the loser can skip the wait entirely when the column is already present. This
  does **not** contradict `_add_columns`' docstring: that rule says correctness may not come
  from a `PRAGMA`-then-act pre-check, and here correctness still comes entirely from
  attempt-and-swallow. The pre-check is a pure latency optimization that is *allowed to be
  wrong*. Say exactly that in the code comment, because the docstring above it says the
  opposite about correctness.
- **The elapsed-time log above**, which is the only way anyone learns the rebuild is
  approaching the timeout before it crosses it.

**The schema-version guard, in the same module.** §i Q12 is decided — this is built, in
phase 0, not merely considered. It needs no new storage (§b.4): the set of rows in
`schema_migrations` *is* the version, and the code already has to know the names it applies.

```python
# Every marker name this image knows how to apply. These strings MUST match the
# names passed to run_migration verbatim (§c.4 passes "001-person-id-rebuild") --
# a typo here makes the guard read this image's own migration as one from the
# future and boot-loops the container. The snapshot filename in §c.7 is a
# separate string and is deliberately not derived from this one.
_KNOWN_MIGRATIONS = ("001-person-id-rebuild",)


async def assert_schema_understood() -> None:
    """Refuse to serve a database that is newer than this image understands.

    Called at the end of init_db(), after run_migration, on its own connection
    (§c.4 step 5) -- so both services get it without either app.py changing.

    An applied marker whose name is not in _KNOWN_MIGRATIONS means some newer
    image migrated this file. This image would then read the result WITHOUT
    erroring and return quietly wrong data (§c.7). Fail the lifespan instead: a
    documented boot loop beats silently merging several people's metrics into
    one series.

    A fresh or pre-runner database has zero markers, which is an empty set and
    therefore passes. The guard only ever fires on names from the future.
    """
    db = await get_db()
    try:
        rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    finally:
        await db.close()
    unknown = sorted({r["name"] for r in rows} - set(_KNOWN_MIGRATIONS))
    if unknown:
        raise RuntimeError(
            f"Database has migrations this image does not know: {unknown}. "
            "Redeploy the newer image, or restore the pre-migration snapshot."
        )
```

Its limits are worth stating where the code is, not only in §i: it **cannot** protect the
001 migration itself, because the image being rolled back to predates the guard. It protects
every migration after this one, and it costs one table read per boot.

### c.4 Where the migration fits relative to `init_db()`

The first draft placed `run_migration` as "step 3 inside `init_db()`," which puts a second
connection's `BEGIN IMMEDIATE` inside the lifetime of `init_db()`'s own connection (risk 3 in
§c.2). The corrected structure closes `init_db()`'s connection first. It also gives the
`VACUUM INTO` snapshot (§c.7) an unambiguous home, which the first draft left unresolved —
the snapshot is 001-specific while `run_migration` is generic, so it cannot live inside the
generic runner.

```python
async def init_db():
    db = await get_db()
    try:
        # 1. CREATE TABLE IF NOT EXISTS for every table, USING THE NEW TARGET DDL,
        #    including persons / person_grants / schema_migrations / garmin_links.
        # 2. _add_columns(...) for the additive columns, plus weight_log.person_id.
        # 3. CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp (see §b.3).
        #    (Body unchanged in structure from shared/database.py:74-252 today.)
        await db.commit()
    finally:
        await db.close()          # <-- connection closed BEFORE anything below

    # 4. 001-specific: snapshot, then migrate. Each opens and closes its own
    #    connection. Nothing from this module holds one across these calls.
    await ensure_pre_migration_snapshot()          # §c.7
    await run_migration("001-person-id-rebuild", _apply_person_id_rebuild)

    # 5. Generic: refuse to serve a DB carrying markers this image does not know
    #    (§c.3, §i Q12). Own connection, nothing held across it.
    await assert_schema_understood()
```

On a **fresh** DB, step 1 creates the tables already correctly shaped; step 4 observes the
correct shape and writes the marker with zero work. On an **existing** DB, step 1 no-ops for
the tables that exist, and step 4 rebuilds.

The shape check inside `_apply_person_id_rebuild` uses `PRAGMA table_info(sleep)` to decide
whether `person_id` is already present. **This is safe even though `_add_columns` explicitly
rejects `PRAGMA`-then-act as TOCTOU-racy** — the difference is that this check runs *inside*
`BEGIN IMMEDIATE`, holding the write lock, where no other connection can change the schema
between observation and action. That distinction is worth a comment in the code, because it
directly contradicts `_add_columns`' docstring in `shared/database.py:44-52` — and, now that
the runner lives in `shared/migrations.py`, the contradicted rule is in a *different* file,
so the comment has to name it explicitly or the connection is invisible to the next reader.

**Alternatives considered for step 1/4 interaction:**

- **A1 — keep the old DDL in `CREATE TABLE IF NOT EXISTS`, always rebuild.** One code path,
  no shape check. Rejected: `shared/database.py` would no longer describe the actual schema,
  which is the file's primary documentary value.
- **A2 — new DDL + shape check inside the transaction. ← Recommended.** `database.py` reads
  as the truth. One extra `PRAGMA` per boot, inside a lock that is already held.
- **A3 — new DDL + seed the marker on fresh DBs.** Requires "is this DB fresh?" detection,
  which is itself a race. Rejected.

### c.5 The `apply` function

The first draft duplicated every metric table's column DDL into a `_REBUILD_TABLES` dict. That
creates a permanent drift surface and silently adds a **fourth** place to update when a metric
is added, contradicting the convention in `CLAUDE.md` ("update `shared/database.py`, `sync.py`,
and `METRIC_TABLES` together"). Worse, the two copies must agree forever: a column present in
`CREATE TABLE IF NOT EXISTS` but missing from the dict is silently dropped on migrated DBs,
and the reverse makes the `INSERT…SELECT` raise and boot-loop.

**Fix: derive the column list from the live table instead of restating it.** The rebuild runs
inside `BEGIN IMMEDIATE`, so `PRAGMA table_info` is authoritative and cannot be raced. The
convention list stays at three places, and there is no second copy to drift.

```python
# Table NAMES only -- no column DDL. See _rebuild_columns for why.
_REBUILD_TABLES = [
    "sleep", "resting_hr", "hrv", "body_battery", "stress",
    "vo2max", "weight_history", "training_load", "steps", "active_calories",
]


async def _rebuild_columns(db, table: str) -> list[tuple[str, str]]:
    """Return [(name, declared_type)] for every non-`date` column of `table`.

    Read from the live schema rather than duplicated from database.py's CREATE
    TABLE text: a second copy of the column list is a fourth place to update
    when a metric is added (CLAUDE.md names three), and any disagreement between
    the copies either silently drops a column on migrated DBs or raises and
    boot-loops. Safe to read here because we already hold the write lock.

    Fails loud on any shape this migration cannot faithfully reproduce, rather
    than silently dropping a constraint. All 10 tables are plain `name TYPE`
    today (verified, shared/database.py:97-234); this guard is what makes that
    an assertion instead of an assumption.
    """
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
    """Rebuild the CHECK(id = 1) singleton into a per-person table.

    Written out explicitly rather than derived, because this is the one table
    whose SHAPE changes (the CHECK disappears, backoff_until appears), so there
    is nothing to derive from. It is therefore also the one table whose DDL
    exists in two places -- §c.8's schema-parity test is what keeps the two
    honest.
    """
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
    # Runs inside BEGIN IMMEDIATE. Any exception rolls back the ENTIRE rebuild --
    # all 11 tables plus the marker -- leaving the original schema untouched.
    # NOTE the scope: weight_log.person_id was added and COMMITTED by
    # _add_columns at init_db step 2, outside this transaction. See §c.6.

    # BEFORE the shape check, not after: on a fresh DB the tables are already
    # correctly shaped and the rebuild below is skipped, but the primary person
    # must still exist or the app boots with an empty `persons` table and no slug
    # to route to (see §g.4).
    person_id = await _ensure_primary_person(db)   # see §(g), idempotent

    if await _has_column(db, "sleep", "person_id"):
        return                      # fresh DB: tables already correctly shaped by step 1.
                                    # "Already migrated" cannot reach here -- the marker
                                    # check upstream excluded it.

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

    # weight_log: backfill + index swap. No rebuild, so its AUTOINCREMENT
    # sqlite_sequence high-water mark is untouched -- see §i Q9 for why that
    # matters if weight_log is ever rebuilt.
    await db.execute("UPDATE weight_log SET person_id = ? WHERE person_id IS NULL", (person_id,))
    await db.execute("DROP INDEX IF EXISTS idx_weight_log_timestamp")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_weight_log_person_timestamp "
        "ON weight_log(person_id, timestamp)"
    )
```

The `__new` suffix (double underscore) is chosen so it cannot collide with a real table name;
after a rollback no `__new` table survives, so no cleanup pass is needed.

**Trivial helpers referenced above, spelled out so nothing here is a placeholder.** Each is a
one-statement wrapper and none carries a design decision: `_has_column(db, table, column)` is
`PRAGMA table_info` membership; `_needs_person_id_rebuild(db)` is
`not await _has_column(db, "sleep", "person_id")` (used only outside the lock, by the snapshot
step, where losing the race costs a wasted snapshot and nothing else — §c.7);
`_first_admin_username(db)` is `SELECT username FROM users WHERE role = 'admin' ORDER BY id
LIMIT 1`, the same rule `bootstrap_migrated_token` already uses at `shared/auth.py:359-361`;
`now_iso()` is `datetime.now(timezone.utc).isoformat()`.

One caution to encode as a code comment: `ALTER TABLE ... RENAME TO` in modern SQLite rewrites
references to the renamed table in views and triggers. This repo defines **no views and no
triggers** (verified: `grep -rn "CREATE VIEW\|CREATE TRIGGER" --include=*.py .` returns
nothing, and `shared/database.py` is the sole schema authority), so the behavior is inert
here. If a view is ever added, revisit.

### c.6 One transaction for all 11 tables, or one per table?

| | Single transaction (all 11) | Per-table transaction + per-table marker |
|---|---|---|
| Atomicity | The **rebuild** is all-or-nothing (see the qualification below) | Crash leaves a *mixed* schema: some tables person-scoped, some not |
| Rollback to previous image | Possible from a backup only, but the DB is at least self-consistent | Mixed schema is unreadable by *either* image |
| Lock hold time | Longest — the loser must wait it out (hence the 30 s busy timeout) | Short per table |
| Resumability | Restart from the beginning (cheap, because nothing was written) | Resume at table granularity |
| Complexity | 1 marker, 1 code path | 11 markers, plus a "which combinations are valid?" analysis |

**The atomicity guarantee, stated at its true width.** The first draft claimed "disk is always
fully-old or fully-new." That is false as written, and the repo shows why: `_add_columns`
(`shared/database.py:53-59`) calls `await db.commit()` **inside its per-column loop**, so
`weight_log.person_id` is added and committed at `init_db()` step 2 — before `run_migration`
opens any transaction. A kill between step 2's commit and step 4's commit leaves 10 metric
tables plus `sync_status` at the old schema, `weight_log` carrying a committed, all-NULL
`person_id`, and no marker.

That is **not** data loss and **not** a torn schema: `person_id INTEGER` is nullable and
invisible to the old query shapes, and the next boot re-runs step 4 and converges. But the
invariant must be stated correctly, because two things depend on it:

> **The *rebuild* (11 tables + marker) is fully-old or fully-new. The additive
> `weight_log.person_id` at step 2 commits independently and converges on re-run.**

1. §c.8's interruption test must assert the pre/post `PRAGMA table_info(weight_log)` **delta**,
   not only `sqlite_master.sql` byte-identity for the rebuilt tables. Byte-identity holds in
   both failure modes, so a test that checks only that would pass while this hole stayed open.
2. §c.7's snapshot is taken *after* step 2 (it must be — it needs its own connection, and
   §c.4 closes `init_db`'s first). So the snapshot already contains the nullable `person_id`
   column and is **not** a byte-exact pre-migration image. It is a fully-functional
   pre-migration *database* — the old image reads it correctly, because the extra nullable
   column is invisible to `SELECT date, [col] FROM …`. Say this in the runbook rather than
   promising byte-identity nobody can deliver.

**Recommendation: single transaction.** The resumability the per-table option buys is only
valuable when the work is too large to redo, and at this repo's scale it is not: a personal
health DB after several years is on the order of a few thousand rows per table, and the
rebuild is sub-second. The mixed-schema state the per-table option can produce is strictly
worse than the "start over from an untouched database" state the single transaction produces.

**Adopt the per-table variant only if** a real deployment's DB grows to where the rebuild
exceeds ~10 s. That trigger is now observable: `run_migration` logs elapsed time at WARNING
(§c.3). Measure before switching; do not pre-optimize into the worse failure mode.

### c.7 Rollback — the sharpest remaining edge, stated honestly

Every prior migration in this repo was rollback-safe by construction: additive nullable
columns are invisible to the previous image, so rollback is a plain image redeploy with no
data step (`docs/prp/00-design.md:1591-1594`). **That property does not survive this
migration, and the way it fails is dangerous.**

An old image running `SELECT date, [col] FROM sleep WHERE date >= ...` against the *new*
schema **does not error**. `person_id` is simply an extra column it does not name. The query
succeeds and returns every person's rows merged into one series — silently presenting a
household's mixed sleep data as one person's. A loud failure would be far safer than what
actually happens.

Three mitigations, in decreasing order of how much they actually help:

**1. Mandatory pre-migration snapshot, taken by the migration itself.**

```python
_SNAPSHOT_NAME = "fitness.pre-001-person-id.db"


async def ensure_pre_migration_snapshot() -> None:
    """VACUUM INTO a temp name, verify it, then atomically rename into place.

    The first draft VACUUM INTO'd the fixed name directly and treated
    VACUUM INTO's refusal to overwrite as the idempotence guard. That is the
    single most damaging kill-point in the design: a container killed
    mid-VACUUM leaves a PARTIAL fitness.pre-001-person-id.db, the next boot
    sees exists() == True, skips the snapshot, migrates -- and the operator now
    holds a corrupt file they believe is the one-way-door backup.

    Temp-name + integrity_check + os.rename fixes it: the fixed name is only
    ever produced by a rename of a file that passed integrity_check, so
    exists() on the fixed name really does mean "a good snapshot exists."
    os.rename is atomic within a filesystem, and both paths are on the same
    vitalforge-data volume by construction.
    """
    final = DB_PATH.parent / _SNAPSHOT_NAME
    if final.exists():
        return                       # only ever produced by the verified rename below

    if os.getenv("VITALFORGE_SKIP_MIGRATION_SNAPSHOT", "").strip() == "1":
        logger.warning(
            "VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1 -- skipping the pre-migration "
            "snapshot. This is a one-way door; take a volume-level backup first."
        )
        return

    db = await get_db()
    try:
        if not await _needs_person_id_rebuild(db):
            return
        tmp = DB_PATH.parent / f"{_SNAPSHOT_NAME}.partial"
        tmp.unlink(missing_ok=True)  # a previous kill can leave one; it is worthless
        try:
            await db.execute("VACUUM INTO ?", (str(tmp),))
        except Exception:
            # Scoped to the VACUUM only: a failure in _needs_person_id_rebuild is
            # not a disk-space problem and must not be reported as one.
            logger.error(
                "Pre-migration snapshot failed. The most likely cause is insufficient "
                "free space on the vitalforge-data volume: VACUUM INTO needs room for a "
                "full second copy of fitness.db. Free space and restart, or -- after "
                "taking a volume-level backup by other means -- set "
                "VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1 to proceed without it. Until one "
                "of those happens this container will restart-loop "
                "(restart: unless-stopped), which is deliberate: migrating without a "
                "backup is worse."
            )
            raise
    finally:
        await db.close()

    check = await aiosqlite.connect(str(tmp))
    try:
        row = await (await check.execute("PRAGMA integrity_check")).fetchone()
    finally:
        await check.close()
    if row is None or row[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Pre-migration snapshot failed integrity_check; refusing to migrate")

    os.rename(tmp, final)
    logger.warning("Pre-migration snapshot written and verified: %s", final)
```

`VACUUM INTO` produces a consistent copy in one statement and **cannot run inside a
transaction**, so it must precede `BEGIN IMMEDIATE` — hence its own connection, and hence
§c.4's ordering.

Two design points that stay from the first draft, both still correct:

- The name is **fixed, not timestamped**. The marker check that proves the migration is needed
  happens inside `run_migration`'s transaction, i.e. after this point, so a timestamped name
  would take a full-DB copy on **every container restart, forever**, growing without bound on
  the same volume that holds the health data.
- The `_needs_person_id_rebuild(db)` shape pre-check is a cheap `PRAGMA table_info` outside the
  lock. It is TOCTOU-racy in principle, but the only consequence of losing that race is a
  wasted snapshot, because correctness still comes entirely from the in-transaction marker.
  That is the one place in this design where a `PRAGMA` pre-check drives an action, and the
  reason belongs in the code comment.

**Three operational facts that must be in the runbook, not left implicit:**

- **Failure is a boot loop, and the exit is documented above.** `docker-compose.yml:12` and
  `:24` are both `restart: unless-stopped`, so a raised exception from the lifespan restarts
  forever. The error message names the cause (free space), the fix, and the escape hatch. An
  undocumented boot loop is the worst version of this; a documented one is an acceptable
  fail-closed.
- **The snapshot is on the same volume as the live DB.** It protects against a bad migration.
  It does **not** protect against volume loss, which is exactly why §g.2 step 2 still calls for
  a separate volume-level backup. Both, not either.
- **The snapshot is a second full copy of real personal health data.** `CLAUDE.md`'s PRIVACY
  block treats `/app/data/fitness.db` as sensitive; `fitness.pre-001-person-id.db` is the same
  data under a name nothing else in the repo mentions. It inherits the same handling rules —
  never read, log, print, or copy its contents. Deleting it is an operator decision and stays
  un-automated (an automatic cleanup would race the window in which it is most needed), but
  the runbook must give a trigger rather than leaving it to memory: **delete it once the
  upgrade has been verified good and at least 7 days have passed**, and record that step in
  the README's upgrade section.

Rollback then means: stop both services, replace `fitness.db` with the snapshot (removing WAL
and SHM sidecars), redeploy the old images.

**2. A schema-version guard for the future — decided, and built in phase 0** (§i Q12). Each
service refuses to serve if the DB carries a migration marker the code does not know
(`assert_schema_understood`, §c.3; no new table or column). Be honest about what this buys:
**it does not protect this migration**, because the old image predates the guard. It protects
the *next* one, and it is cheap enough to add now while the surrounding code is open.

**3. Operator procedure.** `docker compose down` before `up`, so no old container is running
while the new one migrates. This is procedural, not enforced. It must be written into the
README's upgrade section — **which is a phase-1 deliverable, listed as such in Appendix B**.
The first draft required this mitigation and then listed no phase that touches the file, which
is exactly how a required mitigation becomes an unshipped one.

### c.8 Testing the migration

`docs/prp/00-design.md`'s open-items list already records "a migration fixture DB matching
the current production schema" as resolved — that fixture pattern is the right vehicle.

**Gating test, write this one first (§c.1).** Through `aiosqlite`, on a connection built
exactly as `get_db()` builds one plus `isolation_level = None`: open `BEGIN IMMEDIATE`, run
the full create/copy/drop/rename sequence plus a marker `INSERT`, then `rollback()`. Assert
the original `sqlite_master.sql` for every touched table is byte-identical, all rows intact,
no marker, and no `__new` tables surviving. **If this fails, stop** — §c.6's single-transaction
recommendation inverts to the per-table-marker variant and Appendix A needs rewriting. Also
assert it against the *legacy* `isolation_level` (`""`) and record the result either way: if
legacy mode turns out to be equally safe, the explicit `isolation_level = None` in
`run_migration` becomes belt-and-braces rather than load-bearing, which is worth knowing.

**Second gating test — cross-connection DDL visibility.** Open connection 1 and run
`init_db()`'s DDL body on it; while it is still open, open connection 2 and issue
`BEGIN IMMEDIATE` + a trivial `CREATE TABLE`. Assert it does not block or raise. Then repeat
with a seed `INSERT` added to connection 1's body and assert what happens. This is the
verifiable half of §c.2's risk 3: §c.4 removes the hazard structurally, and this test is what
stops someone from reintroducing it by moving `run_migration` back inside `init_db()`.

The rest:

- A fixture DB at the **pre-migration** schema with rows in all 11 tables → run `init_db()` →
  assert new PKs, assert row counts preserved, assert every row has the primary `person_id`.
- **Schema parity, fresh vs migrated.** For all 11 tables plus `weight_log`, assert that
  `PRAGMA table_info` tuples `(name, type, notnull, dflt_value, pk)` and `PRAGMA index_list`
  are identical between a DB created fresh by `init_db()` and a DB migrated from the
  pre-migration fixture. This is the drift guard that replaces the deleted `_REBUILD_TABLES`
  column dict (§c.5), and it is the only thing keeping `_rebuild_sync_status`' hand-written
  DDL honest against `shared/database.py`. It is also what would have caught the
  `idx_weight_log_timestamp` divergence in §b.3.
- **Idempotence:** run `init_db()` twice; assert the second is a no-op (marker present, row
  counts unchanged).
- **Fresh DB:** `init_db()` on an empty file → correct shape, marker written, no rebuild work,
  exactly one `persons` row with `is_primary = 1`.
- **Schema-version guard (§c.3, §i Q12), two cases:** an empty `schema_migrations` and one
  holding exactly `_KNOWN_MIGRATIONS` both pass; an inserted marker named `"002-from-the-future"`
  raises. Assert the passing case against a DB that `init_db()` just migrated, which is what
  catches `_KNOWN_MIGRATIONS` drifting from the name `run_migration` is actually called with.
- **Interruption:** inject an exception in the middle of `_apply_person_id_rebuild` (e.g. on
  the 6th table) → assert the rebuilt tables' `sqlite_master.sql` is byte-identical to
  pre-migration, assert no marker, assert no `__new` tables survive, **and assert the
  `PRAGMA table_info(weight_log)` delta explicitly** — `person_id` is expected to be present
  and all-NULL, because §c.6 says step 2 committed it independently. Then run again cleanly
  and assert convergence. A test that asserts only byte-identity passes in both failure modes
  and proves nothing.
- **Snapshot integrity:** truncate a `.partial` file to simulate a kill mid-`VACUUM INTO`,
  re-run `ensure_pre_migration_snapshot()`, assert the partial is discarded and a fresh
  verified snapshot is produced; then corrupt a `.partial` in place and assert
  `integrity_check` rejects it and no fixed-name file appears.
- **Concurrency:** two connections calling `run_migration` simultaneously → exactly one
  performs work, the other observes the marker, final schema correct. This mirrors the
  existing concurrent-bootstrap test for `bootstrap_first_admin`.
- **Cross-person isolation:** two persons, same date, both synced → two rows, neither
  overwritten. This is the regression test for the bug the whole design exists to prevent.
- **Weight dedup isolation:** two persons, same second, weights within 50 g → two rows.
  Regression test for §0.2 Correction 1.
- **`person_id`-on-INSERT regression:** POST a weight, then assert
  `SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL` is 0. This is the direct guard for
  §0.2 statement #9, the one the first draft's "complete list" omitted, and it is the reason
  §b.3 keeps the column nullable.

**Test-environment note:** `tests/conftest.py` points `shared.database.DB_PATH` at a per-test
`tmp_path`, so `ensure_pre_migration_snapshot()` will materialize
`fitness.pre-001-person-id.db` inside test temp directories whenever a fixture DB triggers a
rebuild. That is expected and correct — the snapshot follows `DB_PATH` by construction — but
state it so it does not read as a leak in review.

---

## (d) Garmin per-person credentials and client registry

### d.1 What exists today

```python
_client: Garmin | None = None                       # module-level singleton

def authenticate():
    global _client
    email = os.environ["GARMIN_EMAIL"]              # one global pair
    password = os.environ["GARMIN_PASSWORD"]
    client = Garmin(email=email, password=password)
    client.login(tokenstore=str(GARTH_TOKEN_DIR))   # resumes from disk OR logs in fresh,
    _client = client                                # and persists tokens, in one call
```

Two properties of this code make the per-person change smaller than it appears:

- `login(tokenstore=path)` **already** resumes from a saved token store and only falls back
  to email/password when nothing valid is on disk. The token store, not the password, is the
  steady-state credential.
- `run_sync()` calls `authenticate()` unconditionally on every run (`sync.py:243`), so the
  singleton is re-established per sync anyway. Turning it into a per-person lookup is a
  narrower change than replacing a long-lived cached object would be.

### d.2 Options for storing per-person credentials

**Option D1 — reversible encrypted credentials in SQLite.**
Store `garmin_email` + AES-GCM/Fernet-encrypted `garmin_password`, key from a new
`VITALFORGE_GARMIN_KEY` env var.

- *For:* fully unattended re-auth forever; no operator action when a token store expires.
- *Against:* introduces the first reversible secret this repo has ever stored, contradicting
  the one-way-hash discipline of `api_tokens`. Requires adding `cryptography` to **both**
  `requirements.txt` files — and this repo has a documented scar about pinning a crypto-adjacent
  dependency (`garminconnect==0.3.11`, with a comment demanding the source be re-read before
  any bump). Key management becomes a new operational burden: rotation, loss (= all links
  broken), and the key sitting in the same `.env` as everything else, so it is not a real
  second factor against host compromise.

**Option D2 — per-person garth token stores, no password at rest. ← Recommended.**
`GARTH_TOKEN_DIR/person-{id}/`, one directory per linked person. A person is linked through a
one-time operator flow: `POST /p/{slug}/api/garmin/link` accepts `{email, password}`,
constructs `Garmin(email, password)`, calls `login(tokenstore=<person dir>)`, and **discards
the password without ever writing it**. The DB stores only non-secret metadata.

- *For:* **no new dependency, and no new *kind* of secret.** A `.garth` token store already
  exists on this volume and is already covered by the repo's PRIVACY rule. The change is one
  of *scale* (N stores instead of 1), not of kind — a far easier thing to reason about and to
  review. Compromise of one person's store does not yield a password, and does not reach the
  other persons' Garmin accounts.
- *Against:* when a token store expires or is invalidated (Garmin password change, forced
  logout), that person's sync fails until an operator re-runs the link flow. Needs a clear
  "link expired" surface in the UI rather than a silent sync failure.

**Option D3 — OS keyring / external secret manager.**
Rejected for this deployment. There is no keyring inside a container, and introducing one
couples the app to the host in a way that contradicts the self-contained Docker Compose model
this repo is built on. Revisit only if VitalForge ever runs outside containers.

**Recommendation: D2**, with D1 available as a strictly-additive later enhancement *if and
only if* token expiry proves painful in practice. Do not build D1 speculatively — it is the
most security-sensitive code in the whole feature and should be justified by measured pain,
not anticipated convenience.

### d.3 Threat model for the new surface (under D2)

**Assets:** per-person garth token stores (each grants full read/write to that person's Garmin
Connect account, including the ability to write body-composition records); the metric data in
`fitness.db`; session cookies and API tokens.

**What is genuinely new versus today:** N token stores instead of 1, and an HTTP endpoint that
accepts a Garmin password in a request body. Everything else — a token store on disk, health
data in SQLite — already exists.

| # | Threat | Today | After D2 | Mitigation |
|---|---|---|---|---|
| T1 | Host/volume compromise | 1 Garmin account exposed | N accounts exposed | Unchanged in kind. Blast radius scales with the feature; document it. Per-person directories created with **explicit** `mkdir(mode=0o700)` plus a `chmod(0o700)` on `GARTH_TOKEN_DIR` itself — see T9 |
| T2 | Password logged or brute-forced during link | n/a | Password transits a request body | Never log the request body on that route; explicit `logger` scrub; no `repr(data)` anywhere in the handler; require HTTPS (reuse `_request_is_https`) and **reject the link route over plain HTTP** unless an explicit dev override is set. **Plus a throttle**: the route proxies arbitrary `{email, password}` to Garmin and returns success/failure, making it a credential-testing oracle for any `manage`-level user and a direct way to get the household's IP 429'd or an account locked. Run every link attempt through §e.3's token bucket **and** cap attempts per user (e.g. 5 per 15 min, 429 thereafter) |
| T3 | Password persisted by accident | n/a | Password lands in the DB or a token store | The link handler binds the password to a local, passes it to `Garmin()`, and lets it fall out of scope. A test must assert the password string appears nowhere in the DB file or the token dir after linking |
| T4 | Cross-person access via path traversal | n/a | `person_id` in a filesystem path | `person_id` is an `int` from the DB, never a user-supplied string. Construct as `GARTH_TOKEN_DIR / f"person-{int(person_id)}"`, never by string concatenation of request input |
| T5 | Wrong-person Garmin write | n/a | Weight pushed to the wrong account | The registry is keyed on the same `person_id` used for the DB write, resolved once per request from the grant check. **This is the highest-severity new bug class** — §0.2 Correction 1 is one instance of it. Every Garmin call must take `person_id` explicitly; no default, no fallback to "the primary person," and "the primary person" itself is a durable `persons.is_primary` marker rather than `ORDER BY id LIMIT 1`, which archiving person 1 would silently re-point (§a.2) |
| T6 | Privilege escalation via grants | n/a | `view` user reaches `manage` action | The single `require_person(level)` dependency (§f.1) is the *only* way a route obtains a `person_id`; no route performs its own ad-hoc grant check |
| T7 | Link/unlink CSRF | n/a | Forced link/unlink | Cookie is `samesite="lax"` (existing). Link and unlink require step-up re-auth, reusing `_require_step_up` (`shared/auth.py:245-249`) exactly as token creation does |
| T8 | Stale grant after user deletion | Tokens cascade manually | Grants would orphan | `admin_delete_user` must also `DELETE FROM person_grants WHERE user_id = ?` in the same transaction — see §f.5 |
| T9 | Credential outlives the link that the UI says is gone | n/a | Unlink/archive leaves `person-{id}/` on disk | **Unlink deletes the token directory**, and archiving a person unlinks first. Non-transactional: the filesystem step cannot join the DB transaction, so ordering is fixed as *commit the DB row deletion first, then delete the directory*, and a failure to delete is logged at ERROR with the exact path so an operator can finish it by hand. The reverse order would leave a linked-looking row with no tokens; this order leaves at worst an orphan directory that the next link overwrites |
| T10 | Third-party exception text leaks into the DB | n/a | `garmin_links.last_auth_error` | Store a **bounded code**, not the raw string: a `garminconnect` exception can carry the account email or a response body, which collides with `CLAUDE.md`'s PRIVACY rule. See §d.4 |

**Residual, accepted:** a host-level compromise yields every linked person's Garmin account.
D2 does not prevent this; it prevents the *password* disclosure and keeps the exposure the
same kind the repo already accepts for one person. If that residual is unacceptable, the
answer is not D1 (which puts the decryption key on the same host) — it is not storing
long-lived credentials at all, i.e. re-auth-per-sync with an operator present, which is
incompatible with a background scheduler. That tradeoff should be named explicitly rather
than papered over.

### d.4 `garmin_links` table

```sql
CREATE TABLE IF NOT EXISTS garmin_links (
    person_id          INTEGER PRIMARY KEY REFERENCES persons(id),
    garmin_email       TEXT NOT NULL,  -- identification and display only; NOT a secret
    linked_at          TEXT NOT NULL,
    linked_by          INTEGER,        -- users.id
    last_auth_ok       TEXT,           -- ISO8601 of last successful login
    last_auth_error    TEXT            -- bounded code, NOT a raw exception string
        CHECK (last_auth_error IS NULL OR last_auth_error IN
               ('auth_failed', 'rate_limited', 'network', 'unknown')),
    last_auth_error_at TEXT            -- ISO8601 of the last failure
);
```

No password column, by design. The token store lives on the filesystem, where garth already
manages it.

`last_auth_error` exists to surface "re-link needed" in the UI, which needs four states, not
free text. The raw exception is logged (server-side, where `CLAUDE.md` already scopes the
privacy rule) and **mapped** to one of the codes above before it touches the DB; the
timestamp goes in its own column so the UI can say "failing since". Persisting
`str(exception)` would make this column an unscrubbed sink for a third-party string that can
contain the account email or a response body.

### d.5 Client registry

The first draft put a DB lookup inside a synchronous `_token_dir()` in
`shared/garmin_client.py`. That module imports only `logging`, `os`, `datetime`, `pathlib` and
`garminconnect` — **zero DB coupling, fully synchronous** (verified, `:1-10`). Reaching the DB
from there needs either a blocking `sqlite3` call from inside the event loop or an `await`
that cannot exist in a sync function, plus a `shared.database` import that inverts the current
dependency direction. Neither is acceptable.

**Fix: resolve the token directory in the async caller and pass a `Path` down.**
`garmin_client` stays DB-free.

```python
# shared/garmin_client.py  -- still imports no DB module

_clients: dict[int, Garmin] = {}


def _ensure_token_dir(path: Path) -> Path:
    """Create the per-person token dir with 0700, and tighten the parent.

    mode=0o700 is explicit because Path.mkdir()'s default is 0o777 & ~umask,
    and exist_ok=True does NOT re-chmod a directory that already exists --
    which is exactly the state every already-deployed install is in for
    GARTH_TOKEN_DIR itself (shared/garmin_client.py:19 today passes no mode).
    So: chmod the parent unconditionally, mkdir the child with a mode.
    """
    GARTH_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    GARTH_TOKEN_DIR.chmod(0o700)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def authenticate(person_id: int, token_dir: Path, email: str, password: str | None) -> None:
    """Authenticate one person and cache their client.

    person_id is REQUIRED and has no default. A default would make T5
    (wrong-person Garmin write) a one-typo mistake instead of a compile-time-
    obvious one. token_dir is passed in, not derived here: resolving it needs a
    DB read (which person is primary), and this module has no DB dependency and
    is synchronous -- see the caller-side resolver below.

    password is None in the steady state: login(tokenstore=...) resumes from the
    saved store and only falls back to credentials when nothing valid is on disk.
    A None password with an expired store raises, which is the correct signal for
    "this person needs re-linking" (surfaced via garmin_links.last_auth_error).
    """
    _ensure_token_dir(token_dir)
    client = Garmin(email=email, password=password)
    client.login(tokenstore=str(token_dir))
    _clients[person_id] = client


def is_authenticated(person_id: int) -> bool:
    return person_id in _clients


def forget(person_id: int) -> None:
    """Drop a cached client -- called on unlink (T9) and on an auth failure, so
    the next use re-authenticates instead of reusing a dead session."""
    _clients.pop(person_id, None)


def get_client(person_id: int) -> Garmin:
    """Return an already-authenticated client. Never authenticates implicitly.

    An implicit authenticate() here would need the token dir and email, i.e. a
    DB read, i.e. the dependency inversion this design just removed. Callers go
    through the async resolver, which is also where the rate limiter lives.
    """
    try:
        return _clients[person_id]
    except KeyError:
        raise RuntimeError(f"Garmin client for person {person_id} is not authenticated")
```

```python
# shared/garmin_registry.py (new) -- the async, DB-aware half.

async def ensure_authenticated(person_id: int) -> None:
    """Resolve this person's token dir + email from the DB, then authenticate.

    This is the only place that knows both halves. It is also the only place
    that touches the rate limiter (§e.3), which is why authenticate() must not
    be reachable any other way.
    """
    if garmin_client.is_authenticated(person_id):
        return
    row = await _load_link(person_id)          # garmin_links row, or None
    if row is None:
        raise GarminNotLinked(person_id)
    token_dir = await _resolve_token_dir(person_id)
    async with garmin_rate_limiter:
        await asyncio.to_thread(
            garmin_client.authenticate, person_id, token_dir, row["garmin_email"], None
        )
```

**Legacy token-store fallback.** The existing flat `GARTH_TOKEN_DIR` holds the primary
person's tokens today. Rather than move files during a migration (filesystem operations are
not transactional and cannot join the DB transaction), the resolver falls back for the primary
person only:

```python
async def _resolve_token_dir(person_id: int) -> Path:
    per_person = GARTH_TOKEN_DIR / f"person-{int(person_id)}"
    if per_person.exists():
        return per_person
    if await _is_primary_person(person_id) and _looks_like_legacy_token_store(GARTH_TOKEN_DIR):
        return GARTH_TOKEN_DIR              # legacy flat layout, read in place
    return per_person
```

Two things about this that the first draft got wrong or left unverified:

- **"Primary" comes from `persons.is_primary`, not `ORDER BY id LIMIT 1`.** With the
  ordering-based definition, archiving person 1 promotes person 2, who then reads person 1's
  Garmin tokens and pushes weight to person 1's account — threat T5, manufactured by this very
  fallback. `_is_primary_person` reads the durable column from §a.2.
- **`_looks_like_legacy_token_store` is UNVERIFIED and must be verified before phase 3.**
  The first draft keyed the fallback on `(GARTH_TOKEN_DIR / "oauth1_token.json").exists()`.
  That filename is asserted, not checked — nothing in this repo names it, and this environment
  could not execute anything to confirm it against the pinned `garminconnect==0.3.11` / its
  garth dependency. If the real filename differs, or changes on a version bump, the fallback
  **silently never fires** and the primary person re-authenticates from scratch. So:
  - Phase 3 begins with a task: read the installed garth source, record the actual token
    filenames, and cite the file and line here the way §0.1 cites everything else.
  - Until then, implement the check as "the directory exists and contains at least one
    `*.json` file, and no `person-*` subdirectory" — a shape test that does not depend on a
    specific name — and tighten it once the name is confirmed.
  - The `garminconnect==0.3.11` comment at `shared/garmin_client.py:22-33` is a standing scar
    about exactly this class of assumption. Do not add another one.
- **The fallback's asymmetry is permanent without operator action.** The first draft said it
  "self-resolves the first time that person re-links." True — but re-linking requires a
  password this design deliberately never stores, so it cannot happen unattended. The fallback
  is therefore load-bearing indefinitely, not transitional, and must be treated as supported
  code rather than a migration shim.

**Boot path: the lifespans authenticate nobody.** Today `vitalforge-dashboard/app.py:63` and
`vitalforge-weight/app.py:42` both call bare `authenticate()` at startup. §d.5 makes
`person_id` required with no default, so the obvious reading — authenticate every linked
person at startup — means **N Garmin logins on every container start, on two services, under
`restart: unless-stopped`.** That is the exact shape of the 2026-08-22 429 incident quoted in
§e.1, amplified by N and by a restart loop.

So phase 3 **removes** the `authenticate()` call from both lifespans. Authentication becomes
lazy and per-person, on first use, through `ensure_authenticated()` — which is inside the
token bucket (§e.3), so even a pathological restart loop is rate-bounded. The visible
behavior change is that a bad credential is no longer reported at boot; it surfaces on the
first sync instead, via `garmin_links.last_auth_error`, which is a better place for it anyway
(it is per-person, and the boot-time version only ever logged a warning).

Every pull/push function gains a leading `person_id: int` parameter. There are 9 of them
(`push_weight` plus 8 getters) — mechanical, and the type checker plus the existing
`tests/test_garmin_client_api.py` guard will catch omissions.

**Event-loop caution, inherited:** `garminconnect` is synchronous and blocks the event loop
for the duration of every call — `vitalforge-weight/app.py:328-334` documents this and notes
that some existing race-freedom *depends* on it. N people means N× the blocking. Do not "fix"
this by moving pushes to a thread pool as part of this work: that comment explicitly warns it
would reopen a stale-read window around `synced_to_garmin`. Treat it as a separate,
independently-reviewed change. (The `asyncio.to_thread` in `ensure_authenticated` above is
deliberately scoped to *login only*, which touches no `weight_log` row and so is outside that
warning — say so in the code comment, or the next reader will read it as a contradiction.)

---

## (e) Sync scheduling and rate limits for N people

### e.1 The existing exposure

`shared/garmin_client.py:22-33` records a real 2026-08-22 incident: a broken token-resume path
forced a full credential login on every request and triggered a Garmin 429. The current
schedule (`sync.py:301-325`) is a 90-day backfill at boot, then `run_sync(days=3)` every
`SYNC_INTERVAL_HOURS` (default 2). Per run that is up to 7 Garmin calls per date plus one
range call.

N people multiply this linearly *and* add N logins. A 4-person household doing a boot backfill
is ~4 × 90 × 7 ≈ 2,500 API calls in a burst — a plausible way to get the household's IP
rate-limited or an account flagged.

### e.2 Options

**Option E1 — loop over people inside the existing tick.**
Simplest. Rejected on its own: it multiplies burst size by N with no smoothing, which is
precisely the failure mode already experienced once.

**Option E2 — round-robin, one person per tick.**
Person i syncs at tick i mod N. Burst size stays at today's level regardless of N. Effective
per-person interval becomes `SYNC_INTERVAL_HOURS × N` — for the default 2 h and 4 people,
every 8 h. Acceptable for daily health metrics (they are daily aggregates; sub-8-hour
freshness is not meaningful), and tunable by lowering `SYNC_INTERVAL_HOURS`.

**Option E3 — global token bucket in front of every Garmin call.**
An `asyncio`-based limiter enforcing a max call rate regardless of who is calling, plus jitter.
Orthogonal to scheduling — it bounds the worst case even when a manual `POST /api/sync` for
person A overlaps the scheduled sync for person B, and it covers the two non-sync paths
(link, and lazy boot authentication) that scheduling alone does not.

**Recommendation: E2 + E3.** Round-robin for the steady state, token bucket as the backstop
that makes the guarantee independent of how many code paths call Garmin. Neither is
complicated; together they make the rate bound a property of the system rather than of the
scheduler's good behavior.

### e.3 Concrete design

- **Serialization:** keep the single global `_sync_lock` (`vitalforge-dashboard/app.py:29`),
  not a per-person lock. Because `garminconnect` blocks the event loop (§d.5), concurrent
  per-person syncs would not overlap usefully anyway, and one global lock preserves the
  reasoning in `scheduled_sync`'s docstring about last-writer-wins `INSERT OR REPLACE`.
- **Cursor — derived, not stored.** The first draft named a "persisted cursor" with nowhere to
  live: `sync_status` became per-person in §b.2, so there is no singleton row left to hold a
  global counter, and inventing a settings table for one integer is not worth it. Instead,
  **the next person is the eligible person with the oldest `sync_status.last_sync_time`**
  (`NULL` sorts first, so a newly linked person goes next):

  ```sql
  SELECT p.id
  FROM persons p
  JOIN garmin_links g ON g.person_id = p.id
  LEFT JOIN sync_status s ON s.person_id = p.id
  WHERE p.archived_at IS NULL
    AND (s.backoff_until IS NULL OR s.backoff_until <= ?)   -- now, ISO8601
  ORDER BY s.last_sync_time IS NOT NULL, s.last_sync_time ASC, p.id ASC
  LIMIT 1
  ```

  This needs no new state, is correct across container restarts (the state it reads is already
  durable), self-heals when persons are added, archived, linked or unlinked mid-rotation, and
  degenerates to today's behavior at N=1. A stored cursor has none of those properties.
- **Staggered backfill:** the boot 90-day backfill runs for **one** person per tick rather
  than all persons at boot. A newly linked person's backfill is queued the same way — and the
  cursor above already schedules them next, because their `last_sync_time` is NULL. New env
  var `SYNC_BACKFILL_DAYS` (default 90) to make the burst tunable without a code change.
- **429 handling:** on a 429 from any call for person P, set
  `sync_status.backoff_until = now + backoff(P)` with exponential growth (e.g. 15 m → 30 m →
  1 h → 2 h, capped at 6 h) and skip P until then. Persisting backoff in `sync_status`
  (rather than in memory) means a container restart does not reset it — restart-loops are
  exactly how a rate limit turns into a ban. `backoff_until` is created by migration 001 in
  phase 1 (§b.2) and first written here in phase 4.
- **Per-person error isolation:** one person's failure must not abort the tick. `run_sync`
  already counts errors per date and continues; extend the same discipline across persons.
- **Manual sync:** `POST /p/{slug}/api/sync` requires `manage` on that person and passes
  through the same token bucket.
- **The token bucket's scope is every Garmin call, not just sync.** That explicitly includes
  `POST /p/{slug}/api/garmin/link` (threat T2) and lazy boot authentication (§d.5). A
  bucket scoped only to the scheduler leaves the two paths a user can trigger at will
  unbounded, which is the wrong half.

### e.4 A bug this section must not create

`get_synced_dates(table)` (`sync.py:14-22`) returns every date in a table, and `run_sync`
uses it to skip dates already present. **Without a `person_id` filter, person B is skipped
because person A already has that date** — B's data would never sync, silently, with no error
anywhere. This is statement #3 in §0.2's inventory and is the single most likely way to ship
a broken feature that looks like it works.

---

## (f) Access control, in `shared/auth.py` terms

### f.1 Resolving a person per request

The mechanism is a **FastAPI dependency that is the only way to obtain a `person_id`**, and
that authorizes as it resolves. The first draft made this a callable helper and leaned on URL
shape for safety; §f.2 explains why that was not sufficient.

```python
_ACCESS_ORDER = {"view": 0, "manage": 1, "own": 2}


async def _identity_and_grant(
    request: Request, slug: str
) -> tuple[_Identity | None, str | None, int | None]:
    """Resolve identity, person and grant together.

    The first draft's docstring claimed single-query discipline and then did
    _get_current_identity() followed by a separate _lookup_grant() -- two
    queries on two connections, leaving the exact hazard the claim disclaims:
    a grant revoked between the two is still usable by the request that passed
    the first. This version resolves person and grant in ONE statement bound to
    the identity that was just established, so the grant read cannot be
    separated from the person read.

    Returns (identity, access_level_or_None, person_id_or_None). person_id is
    None only when the slug matches no active person; the caller turns both
    that and a missing grant into the same 404.
    """
    identity = await _get_current_identity(request)
    if identity is None:
        return None, None, None
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT p.id AS person_id, g.access AS access "
                "FROM persons p "
                "LEFT JOIN person_grants g "
                "  ON g.person_id = p.id AND g.user_id = ? "
                "WHERE p.slug = ? AND p.archived_at IS NULL",
                (identity.user_id, slug),
            )
        ).fetchone()
    finally:
        await db.close()
    if row is None:
        return identity, None, None
    return identity, row["access"], row["person_id"]


def require_person(level: str):
    """Build the dependency. Usage:

        @app.get("/p/{slug}/api/metrics/{name}")
        async def get_metrics(name: str,
                              person_id: int = Depends(require_person("view"))):
            ...

    A route obtains a person_id ONLY this way. There is no module-level helper
    that returns a person_id without authorizing, because such a helper is the
    thing that gets called by mistake.

    Scope: this resolves ACTIVE persons only (archived_at IS NULL). Admin
    routes that must list or un-archive an archived person use _require_admin
    and address the person by id, not through this dependency.
    """
    async def dependency(request: Request, slug: str) -> int:
        identity, granted, person_id = await _identity_and_grant(request, slug)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if person_id is None:             # no such active slug -- same 404 as no grant
            raise HTTPException(status_code=404, detail="Person not found")
        if identity.user_id is None:      # anonymous == open-access mode, see f.3
            return person_id              # `granted` is always None here; not consulted
        if identity.role == "admin":      # matches the bypass at auth.py:1107
            return person_id
        if granted is None or _ACCESS_ORDER[granted] < _ACCESS_ORDER[level]:
            raise HTTPException(status_code=404, detail="Person not found")
        return person_id
    return dependency
```

**404, not 403,** for a missing grant: a 403 confirms the person exists, which leaks household
membership to any authenticated user. The existing code returns 403 for the admin-only case,
which is fine — that leaks nothing. Note the same 404 is returned for "no such slug" and "no
grant," deliberately.

Ordering note, unchanged and still correct: the anonymous check precedes the admin check, so
the open-access sentinel (`shared/auth.py:176` returns `_Identity("anonymous", None, None,
None)` when `_is_auth_configured()` is False) short-circuits before anything reads
`identity.role`, which is `None` in that mode.

### f.2 How routes select a person

**Option F1 — query parameter `?person=<slug>`.**
Smallest diff: existing templates, static JS, and the PWA service worker keep their URLs and
gain a parameter. Risk: an endpoint that forgets to read it silently falls back to the default
person.

**Option F2 — path segment `/p/{slug}/api/metrics/{name}`. ← Recommended.**

The first draft justified F2 by claiming the person becomes "structurally impossible to omit."
**That claim is false and should not be repeated.** Verified against
`shared/auth.py:1248-1268`: `auth_middleware` gates only on `user is None` and knows nothing
about persons. A route can carry `{slug}`, resolve it to a `person_id`, query data, and never
authorize — the URL shape forces nothing on its own.

F2 is still the better option, for two reasons that are actually true:

- **A cache-key boundary.** The PWA service worker caches by URL. With a path segment, one
  person's cached responses can never be served to another; with a query parameter, the cache
  list and the invalidation story both have to become person-aware by hand.
- **No implicit fallback.** A missing path segment is a 404 from the router. A missing query
  parameter is an empty string that something will helpfully default.

**The structural safety comes from `Depends(require_person(...))` (§f.1), not from the URL.**
That dependency is the only supplier of `person_id`, it authorizes as it resolves, and a route
that forgets it has no `person_id` to pass to its SQL — which is a `NameError` at import time,
not a silent wrong-data response. Adopt F2 *and* the dependency; neither substitutes for the
other.

*Cost:* templates and static JS must be updated, and the service worker's cached URL list
changes. That is real churn, but it is churn in exactly the layer that most needs to become
person-aware, and it is mechanical.

**Recommendation: F2** (decided — §i Q7), with `default_person_id` used only to build the
initial redirect (`GET /` → `/p/{slug}/`), never as an implicit fallback inside a
person-scoped data route. Because §f.8 drops the alias layer, that rule now has **no
exception anywhere in this design**.

**URL convention, stated once so the rest of this document is unambiguous:**

| Shape | For | Authorized by |
|---|---|---|
| `/p/{slug}/` | The dashboard / weight UI for one person | `require_person("view")` |
| `/p/{slug}/api/...` | Everything scoped to one person: `metrics/{name}`, `weight`, `weight/recent`, `weight/trend`, `weight/{id}`, `sync`, `sync/status`, `recommendations`, `garmin/link`, `garmin/unlink` | `require_person(level)` — `view` for reads, `manage` for writes and Garmin actions |
| `/api/persons`, `/api/persons/{id}` | Admin CRUD over the person *collection*, including archived persons and grant management | `_require_admin`, or `own` on that person for grant changes |
| `/api/metrics/...`, `/api/weight...` | **Nothing — unmounted in phase 2, no aliases** (§f.8) | n/a; the router 404s |

Person-scoped routes address the person by **slug** (it is what the browser and the service
worker cache see). Admin collection routes address it by **id**, because they must be able to
reach archived persons, which `require_person` deliberately cannot.

### f.3 Open-access (empty `users` table) mode must keep working

`_is_auth_configured()` returns False while `users` is empty, and the middleware then lets
everything through (`shared/auth.py:1255-1256`). CLAUDE.md documents this as intentional local
development behavior. Preserve it exactly: when identity is the `anonymous` sentinel
(`user_id is None`), the dependency returns without consulting grants — anonymous has
implicit `own` on every person. Any other choice breaks `docker compose up` on a fresh volume,
which is the primary dev path.

**Say the blast radius out loud, because it changes.** If `VITALFORGE_PASS` is never set,
`users` stays empty forever and anonymous holds implicit `own` on **every person** — which
now means several people's health data and (after phase 3) the ability to push weight to
several people's Garmin accounts, where today it means one person's. The *kind* of exposure is
unchanged and `CLAUDE.md` documents it as intentional; the *scale* is not. This belongs in the
README's security note alongside the existing open-access warning, not only in this design.

### f.4 Slug validation and reserved names

`persons.slug` is `TEXT NOT NULL UNIQUE` and appears in every URL. The first draft validated
it nowhere: §g.1 set it from `_first_admin_username(db)` verbatim or from
`VITALFORGE_PRIMARY_PERSON` with only `.strip()`. Usernames in this codebase are checked only
against `_RESERVED_USERNAMES` (`shared/auth.py:61`, enforced at `:1155`) — nothing stops `/`,
whitespace, `..`, or a value that shadows a real route. The migration runs exactly once, so a
bad slug is minted once and irreversibly.

Mirror the pattern that already exists:

```python
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

# Anything that would shadow a real path segment under /p/{slug}/ or collide
# with the sentinels auth.py already reserves.
_RESERVED_SLUGS = {
    "api", "auth", "static", "health", "p", "new", "admin", "persons",
    "anonymous", "api-token",
}


def _slugify(raw: str) -> str:
    """Derive a URL-safe slug. Slugify, do not copy: a username is validated
    against _RESERVED_USERNAMES only, which is a different and smaller rule
    than 'safe as a path segment'.

    Returns "" when nothing usable survives; callers must handle that rather
    than persisting an empty slug into a NOT NULL UNIQUE column.
    """
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32].strip("-")
    return s if _SLUG_RE.match(s) else ""
```

Every slug — migration-created, admin-created, or renamed — passes `_SLUG_RE` and is rejected
if it is in `_RESERVED_SLUGS`. `_ensure_primary_person` slugifies its input and falls back to
`"primary"` if the result is empty or reserved, logging loudly when it does.

`_SLUG_RE`, `_RESERVED_SLUGS` and `_slugify` live in `shared/auth.py` next to
`_RESERVED_USERNAMES`, since that is where person authorization lives and where an admin route
would reach for them. `shared/migrations.py` imports them for `_ensure_primary_person`; that
import direction (migrations → auth) is new but one-way, and `shared/auth.py` must not import
`shared/migrations.py` back.

**Slug reuse after archive — decided, not left open.** `UNIQUE` stays **global**, including
archived persons: an archived person's slug is permanently taken. The alternative (uniqueness
over active persons only) means an old bookmark or a cached service-worker URL silently
resolves to a *different human's* health data, which is the worst failure this design can
produce. The cost — a burned name — is addressed by a rename path instead: `PATCH
/api/persons/{id}` may change `slug` (subject to the same validation), so an operator who
wants a name back renames the archived person first. No redirect is issued from the old slug;
it simply 404s.

### f.5 Manual cascade on user deletion (concrete finding)

`admin_delete_user` (`shared/auth.py:1220-1246`) manually deletes `api_tokens` before the user
because FKs are off. **It must also delete `person_grants`**, in the same `BEGIN IMMEDIATE`
transaction:

```python
await db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
await db.execute("DELETE FROM person_grants WHERE user_id = ?", (user_id,))       # NEW
await db.execute("UPDATE person_grants SET granted_by = NULL WHERE granted_by = ?",
                 (user_id,))                                                      # NEW
await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
```

(`users.default_person_id` needs nothing here — it is a column *on* the row being deleted, and
no other user's copy of it references this user. It does need attention when a **person** is
removed, which is why §f.6 archives rather than deletes persons: an archived person's id stays
valid, so no `default_person_id` can dangle.)

Omitting the first `DELETE` leaves orphan grants whose `user_id` a future `AUTOINCREMENT`
reuse could resurrect — handing a new account the deleted account's access. Same failure shape
as the username-reuse hazard that `validate_session`'s docstring already reasons about.

The `granted_by` `UPDATE` is the same hazard one step removed: it is a `users.id` that the
cascade would otherwise leave dangling, and `users.id` is `AUTOINCREMENT`, so it can be
reused. It is display-only today ("granted by alice"), but a dangling id that later points at
a different account is an audit-trail lie. NULL it rather than leaving it.

### f.6 Person lifecycle

- **Create:** admin-only (`_require_admin`). The creator gets an `own` grant automatically.
- **Archive, not delete:** `persons.archived_at`. Deleting a person means deleting years of
  health data across 11 tables with no FK cascade to help — too sharp an edge for a UI button.
  Archived persons drop out of sync rotation and default listings; a separate, explicit,
  admin-only purge can come later if actually wanted.
- **Archiving unlinks first.** Archive runs the same unlink path as §d.4/T9: delete the
  `garmin_links` row in the transaction, then delete the token directory after commit. An
  archived person must not retain a live Garmin bearer credential on disk.
- **The primary person cannot be archived while `is_primary = 1`.** Refuse with 409 and a
  message naming the fix (promote another person first). The alternative is the T5 drift this
  design removed in §a.2 — a durable marker that points at an archived row is only marginally
  better than a derived one that silently re-points.
- **Grants:** `own` on the person, or any admin, may grant/revoke. Revoking one's own last
  `own` grant is permitted, because admins can always restore it — deliberately *not* modeled
  on the "cannot demote the last admin" guard (`shared/auth.py:1235-1237`), which exists
  because there is no higher authority to recover from that state. Say so in a comment; the
  asymmetry will otherwise look like an oversight.
- **The orphaned person is a reachable, deliberate state.** §f.5's cascade deletes all of a
  deleted user's grants, and the bullet above permits revoking the last `own`. Together, a
  person can end up with **zero** grants, reachable only by admins (`shared/auth.py:1107`).
  That is accepted, not an oversight: admins bypass grants entirely, so an orphaned person is
  always recoverable, and the alternative — a "cannot delete the last grant" guard — would
  make deleting a user fail for reasons the admin cannot see from the users page. State it in
  the code and in the release notes so it is discovered by reading, not by hitting it.

### f.7 API tokens and ingest routing

Tokens inherit their owner's grants for free — `_resolve_bearer_token` already returns a full
`_Identity` including `user_id` and `role`, so the §f.1 dependency works unchanged for bearer
requests.

**But grants alone do not answer the question this feature exists to answer.** §0.2
Correction 1 fixes the dedup `SELECT` by binding it to "the request's resolved person," which
presumes the caller already knows whose reading it is. For the PWA that is true — a human
opened `/p/alex/`. For the **driving requirement** — a parent managing a child who has no
login — nothing in the first draft said how a scale reading gets attributed.
`vitalforge-weight/app.py:97` types `source: Literal["pwa","bascule","bridge","tasker"]`; a
bridge authenticates as the parent and, under F2, posts to exactly one person's path. So a
household with one bridged scale and two people had a data model, an access model, and no way
to route a measurement. That was the end-to-end hole, and it is closed here:

**`api_tokens.person_id` is load-bearing, not a phase-5 nicety.** Additive nullable column:

```sql
-- appended to a new _API_TOKENS_ADDITIVE_COLUMNS
"person_id INTEGER"
```

Resolution order for `POST /p/{slug}/api/weight`, most specific first:

1. **If the bearer token has `person_id` set**, that person is the subject — and the request
   is **rejected with 403** if `{slug}` resolves to any other person. A scoped token cannot be
   pointed at a different person by changing the URL, which is the whole point of scoping it.
2. **Otherwise** the `{slug}` in the path is the subject, authorized by `require_person("manage")`
   as any other route.
3. **There is no third rule.** No body field, no default, no "the primary person."

This means a household with one bridged scale per person issues one scoped token per person
and configures each bridge with its own — which is also the answer to "can the child's phone
reach the parent's data?" (no). A household with **one shared scale and two people** cannot be
routed automatically by any mechanism this design offers, and that is a genuine product
limitation, not an oversight: the reading itself carries no identity. The supported answers
are (a) one linked scale per person, or (b) the person who weighed themself confirms in the
PWA. **The deployment has chosen (b)** — one shared scale, human confirmation, no automatic
guess (§i Q4). That adds nothing to phase 2's list: rule 2 above *is* the confirmation. The
human picks the person by posting to that person's `/p/{slug}/api/weight` from the PWA, and
`require_person("manage")` authorizes the choice. No new endpoint, no pending-measurement
queue, no attribution heuristic.

Because ingest routing depends on it, `api_tokens.person_id` lands in **phase 2** alongside
the route change, not phase 5.

### f.8 The legacy un-scoped API paths — direct cutover, no alias layer

Phase 2 changes `/api/metrics/{name}` → `/p/{slug}/api/metrics/{name}` and `/api/weight` →
`/p/{slug}/api/weight`. **The old paths are unmounted in the same change. There is no
compatibility alias, no deprecation window, and no alias-removal phase.**

An earlier revision of this document designed a time-boxed alias layer here, on the inference
that `bootstrap_migrated_token` (`shared/auth.py:339-386`) exists *because* external automation
clients hold tokens — phones running Tasker/Bascule, which nobody re-flashes on a
`docker compose pull`. That code artifact is real and stays; the inference about live traffic
was wrong. **The deployment's owner confirmed on 2026-08-26 that nothing calls `/api/weight`
or `/api/metrics/...` today** (§i Q8). Tokens were issued; no client is using those paths. So
there is no client to preserve, and an alias layer would be a mechanism whose entire
justification is a population of zero.

Why that is the better outcome rather than a corner cut:

- **The alias was the design's only exception to §f.2's "no implicit fallback" rule.** It had
  to resolve a person from the token, then `default_person_id`, then "the caller's only
  reachable person," and 400 if none of that was unambiguous. Deleting it means every route in
  this design gets its `person_id` from exactly one place, `Depends(require_person(...))`, with
  no second path to audit and no ambiguity branch to get wrong. §0.2 Correction 1's failure
  mode — one person's weight written onto another's record — loses a door it could have come
  back through.
- **The failure mode if a forgotten client does exist is loud and harmless.** An un-scoped call
  hits no route and gets a `404`. It never writes, and it never reads another person's data. A
  404 on a path nobody calls is the cheapest possible way to be wrong about this.
- **The fix, should that ever happen, is to point the client at the new URL** — which is what
  the alias layer's own WARNING log existed to prompt, minus the layer.

This is a decision about *this* deployment's facts, not a general position: if a client is
ever discovered mid-phase-2, the answer is to reconfigure it, not to reintroduce an alias.
Nothing in phases 3-5 depends on the legacy paths existing.

---

## (g) Existing-data migration and cutover

### g.1 The primary person

All existing rows belong to exactly one real human. They become person 1, and that row is
marked `is_primary = 1` — durably, not by ordering (§a.2, threat T5).

```python
async def _ensure_primary_person(db) -> int:
    """Create (or return) the person that owns all pre-multi-tenancy data.

    Idempotent: called on every migration run, including the fresh-DB path where
    no rebuild follows. Runs inside the migration transaction, so the
    check-then-insert is not racy.
    """
    existing = await (await db.execute(
        "SELECT id FROM persons WHERE is_primary = 1"
    )).fetchone()
    if existing is not None:
        return existing["id"]

    # Only reachable on a persons table with no primary. If rows exist without
    # one, something else created them -- fail loud rather than pick one.
    any_person = await (await db.execute("SELECT COUNT(*) FROM persons")).fetchone()
    if any_person[0] != 0:
        raise RuntimeError("persons rows exist but none is_primary; refusing to guess")

    raw = os.environ.get("VITALFORGE_PRIMARY_PERSON", "").strip() \
        or await _first_admin_username(db) or "primary"
    slug = _slugify(raw)                       # §f.4: validated, reserved-checked
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
        await db.execute("UPDATE users SET default_person_id = ? WHERE id = ?",
                         (person_id, admin["id"]))
    return person_id
```

Deliberate choices:

- **Naming from the first admin, overridable by `VITALFORGE_PRIMARY_PERSON`.** The first admin
  is who `bootstrap_migrated_token` already treats as the canonical owner
  (`shared/auth.py:359-361`); reusing that rule avoids inventing a second notion of "the main
  account." The username is **slugified**, not copied — §f.4.
- **`display_name` keeps the raw input; `slug` keeps the slugified form.** The human-readable
  name has no URL constraints and should not inherit them.
- **No admin? No grant.** That is the open-access dev case (empty `users` table), where
  anonymous has implicit `own` (§f.3). Correct and requires no special handling.
- **Non-admin users get nothing.** Any additional accounts created before this migration have
  no grant and see no persons until an admin grants them access. Fail-closed is the right
  default when the migration cannot know who should see whose health data. **No such account
  exists in this deployment** (§i Q6), so this cuts nobody off in practice; it remains the rule
  for any non-admin account created before the upgrade actually runs, and a one-line release
  note is enough.

### g.2 Cutover procedure

The migration is not backward-compatible in the dangerous, silent way described in §c.7, so
cutover is a short coordinated restart rather than a rolling one.

1. `docker compose down` — **both** services stopped. This is the step that matters; the
   danger is an old-image container serving while the new schema is live.
2. Take a **volume-level** backup of `vitalforge-data`. The migration takes its own snapshot,
   but that snapshot lives on the same volume and so does not survive volume loss. This is a
   one-way door; both backups, not either.
3. Confirm free space on `vitalforge-data` is at least the size of `fitness.db`, or the
   snapshot step will fail and boot-loop (§c.7).
4. Set `VITALFORGE_PRIMARY_PERSON` in `.env` now if the default (first admin's username) is
   not wanted — it is read exactly once, during a migration that runs exactly once.
5. `docker compose pull` / `up -d` with both new images.
6. Whichever service wins the `BEGIN IMMEDIATE` race migrates; the other blocks up to 30 s and
   then observes the marker. Check the logs for `Applied schema migration 001-person-id-rebuild
   in N.NNs` — that line is also the §c.6 measurement.
7. Verify: `/health` on both, `/api/persons` lists exactly one person with `is_primary`, and
   spot-check that a metric series returns the same shape and count as before.
8. After the upgrade is verified good and at least 7 days have passed, delete
   `fitness.pre-001-person-id.db` from the volume (§c.7 — it is a second full copy of personal
   health data and inherits the PRIVACY rule).

**Rollback:** stop both, restore the pre-migration snapshot over `fitness.db` (removing WAL
and SHM sidecar files alongside it), redeploy the previous images. Note the snapshot is taken
*after* `weight_log.person_id` was added, so it is not byte-identical to the pre-upgrade file
— it is a functionally-equivalent one the old image reads correctly, because that column is
nullable and unnamed by every old query (§c.6). Any data written after the migration is lost —
which for a health tracker syncing daily aggregates means re-syncing, not permanent loss. Say
this plainly in the runbook.

### g.3 Online migration — considered and not recommended

**Option G1 — maintenance window (above). ← Recommended.** Downtime is one container restart,
seconds, on a personal app with one household of users. The simplicity is worth more than the
seconds.

**Option G2 — expand/contract across multiple releases.** Release 1 adds nullable `person_id`
and dual-writes; release 2 backfills; release 3 swaps the PK. This is the standard
zero-downtime playbook and it is genuinely safer *for a system that cannot take downtime*.
Here it triples the release count, keeps the codebase in a half-migrated state for weeks, and
the PK swap — the actual risk — still has to happen at the end. It buys nothing this
deployment needs.

**Option G3 — migrate into a new DB file and swap.** Build `fitness.new.db`, then rename over
the original at boot. Loses the atomicity guarantee verified in §c.1 (a rename is not part of
the DB transaction), and reintroduces the torn-state window the single-transaction design
eliminates. Strictly worse.

### g.4 Fresh installs

A brand-new deployment (`docker compose up` on an empty volume) never runs the rebuild — step
1 of `init_db()` creates the tables already correctly shaped. But it **must still get a
primary person**, or the app boots with an empty `persons` table: no slug for `GET /` to
redirect to, nothing for the dashboard to display, and — in open-access mode, where anonymous
holds implicit `own` on every person (§f.3) — implicit `own` over an empty set. That breaks
`docker compose up` on a fresh volume, which is the primary development path this design
committed to preserving.

Hence `_ensure_primary_person` is called **before** the shape check in
`_apply_person_id_rebuild` (§c.5) and is idempotent. On a fresh DB it creates person 1 and
does nothing else.

One ordering caveat: on a fresh volume `init_db()` runs *before* `bootstrap_first_admin()` in
both lifespans, so no admin exists yet when `_ensure_primary_person` runs, and it therefore
creates no grant and falls back to the `"primary"` slug. That is correct for open-access mode
but leaves the eventually-bootstrapped admin with no grant and no `default_person_id`. Two
options: have `bootstrap_first_admin()` also grant the new admin `own` on the primary person
(preferred — it already owns the "first admin" concept, and its concurrent-race handling is
already proven), or reorder the lifespan. **Do not** solve it by making
`_ensure_primary_person` non-idempotent.

---

## (h) Phasing recommendation

Six phases. The natural seam — and the strongest reason to phase at all — is that
**the entire schema and migration story can land without touching Garmin credentials.** That
isolates the irreversible change from the most security-sensitive one, so neither review has
to carry the other's risk.

**Phase 0 — migration runner.** Starts with §c.8's **two gating tests**: DDL rollback through
`aiosqlite`, and cross-connection DDL visibility. A negative result on the first reshapes §c.6
and Appendix A, so it comes before any other line of code. Then `shared/migrations.py`, the
`schema_migrations` table, `run_migration`, `ensure_pre_migration_snapshot`, the `get_db()`
`busy_timeout` raise, the `_add_columns` shape pre-check, the schema-version guard, and tests.
**No schema change.** Documentation deliverables land here too, because they are corrections
to statements phase 0 proves wrong: fix `docs/prp/00-design.md:1596-1598`'s
defaulted-column claim (§b.3), and amend `shared/database.py:8-19`'s comment per Appendix A.
Lands alone, is independently valuable (this repo has wanted a runner), and is reviewable in
isolation.

**Phase 1 — the rebuild.** `persons` (with `is_primary`), `person_grants`, the 11-table
rebuild, `weight_log` additive column + index swap at **both** DDL sites, backfill to the
primary person, and all 13 statements plus the signature chain in §0.2 scoped to that one
person. **User-visible behavior is identical.** One person exists, every query returns exactly
what it returned before. Includes `recommendations.py` — `_get_metric`, `get_all_metrics` and
`get_rules_only` take a person here, and the module-level `_cache` (`:13-14`) is keyed by
`person_id` in the same change (see §i Q11 for why that is phase 1 and not phase 5).
Documentation: `CLAUDE.md` (see below) and the README upgrade/rollback section. This is the
irreversible phase, and it ships with no new features to confound a bisect.

**Phase 2 — access control and ingest routing.** The `require_person` dependency,
`/p/{slug}/` routing (§f.2), slug validation and `_RESERVED_SLUGS` (§f.4), person CRUD for
admins, grant management UI, the `admin_delete_user` cascade fix (§f.5),
**`api_tokens.person_id` and the ingest resolution order (§f.7)**, and **unmounting the legacy
un-scoped `/api/weight` and `/api/metrics/...` paths in the same change** (§f.8 — no aliases,
nothing calls them). Multiple persons can now exist, be viewed, and receive ingested
measurements — but only one has Garmin data. `api_tokens.person_id` moved here from phase 5
because ingest routing depends on it.

**Phase 3 — per-person Garmin.** Begins with the garth-token-filename verification task
(§d.5). Then `garmin_links`, `shared/garmin_registry.py`, per-person token stores with explicit
`0700`, link/unlink endpoints with step-up auth and throttling, unlink deleting the token
directory (T9), removing `authenticate()` from both lifespans, and the legacy fallback keyed
on `persons.is_primary`. This is the phase that needs a dedicated security review; keeping it
separate means that review has one subject. `.env.example` and the README change here too —
`GARMIN_EMAIL`/`GARMIN_PASSWORD` stop being the ongoing credential and become bootstrap-only
for the primary person's legacy store.

**Phase 4 — sync scheduling.** Derived round-robin cursor, token bucket (covering sync, link,
and lazy auth), 429 backoff written to the `backoff_until` column created in phase 1,
per-person error isolation, staggered backfill, `SYNC_BACKFILL_DAYS`. Only meaningful once
phase 3 allows more than one linked person, and best measured against a real second account.

**Phase 5 — cross-person features.** The actual payoff: comparison views, household
aggregates. Everything here is additive on a schema that is already correct.

This phase used to also carry "remove the §f.8 compatibility aliases at the end of their
window." That item is gone with the aliases (§i Q8), but the phase is not: what remains is
dashboard `templates/`/`static/` work that has nothing to do with phase 4's scheduler, lands
after it rather than with it, and is the one phase a reviewer can evaluate on whether it is
*useful* rather than whether it is *safe*. Six phases, not five.

**Do not compress phases 1 and 3.** They fail in unrelated ways (data loss vs. credential
disclosure), need different reviewers, and combining them means a rollback for either reason
reverts both.

---

## (i) Decisions — the thirteen questions, answered

*This section is written to stand alone — it can be lifted out of the document and read
without the rest. Each item states the context it needs.*

**All thirteen were answered by the deployment's owner on 2026-08-26. Nothing here is open.**
Twelve confirm what the document already recommended; **Q8 changed the design** and is
propagated through §a.2, §f.2, §f.8, §(h) and Appendix B.

The `Q<n>` labels are kept exactly as they were, because a dozen cross-references elsewhere in
this document address them by number. Ordering still reflects how much downstream design each
one blocked.

1. **Is a short maintenance-window cutover acceptable? — YES.**
   The plan stops both containers, runs a one-shot schema migration on the shared SQLite file,
   and starts them again: total downtime on the order of a container restart. This is a
   personal household app; nobody is paged by a 20-second gap. The alternative
   (expand/contract across three releases, the standard zero-downtime playbook) triples the
   release count, keeps the codebase half-migrated for weeks, and still has to do the risky
   step at the end. §c and §g.2 stand as written, and no zero-downtime requirement exists.

2. **Garmin credentials: per-person token stores, not encrypted passwords at rest.**
   One garth token directory per linked person; the password is accepted once over HTTPS
   during linking and never written anywhere. No new dependency, and no new *kind* of secret —
   a token store already lives on this volume today. The accepted cost is explicit: when a
   token store expires or is invalidated (the person changes their Garmin password, or Garmin
   forces a logout), that person's sync fails until someone manually re-links, and re-linking
   needs a password nobody stored. Unattended operation across token expiry is **not** a
   requirement here, which is the condition that would have flipped this.
   The rejected alternative — an encrypted password in SQLite keyed from a new env var — would
   introduce the first reversible secret this repo has ever held, add `cryptography` to both
   services, and put the key in the same `.env` as everything else, so it is not a real second
   factor against host compromise anyway. §d is built as designed.

3. **How many people, realistically? — 4.**
   Not a range: four. Both things that hinge on it resolve comfortably. Round-robin (§e.2, E2)
   gives each person a refresh every `SYNC_INTERVAL_HOURS × N` = 2 h × 4 = **every 8 hours**,
   which is fine for metrics that are daily aggregates, and remains tunable by lowering
   `SYNC_INTERVAL_HOURS`. The one-shot migration over four people's data is trivially fast and
   nowhere near the 30 s lock timeout (§c.3).
   *If this ever grows past ~10 people*, two things want revisiting — per-person schedules
   instead of one round-robin cursor, and measuring the migration's duration against that
   timeout before upgrading. Neither is designed for now, and neither should be.

4. **How does a scale reading get attributed to a person? — Option (b): shared scale, manual
   confirmation in the PWA, no auto-guessing.**
   The household has one shared scale. The measurement carries no identity, so a human names
   the subject: the PWA posts to that person's `/p/{slug}/api/weight`, and
   `require_person("manage")` authorizes the choice (§f.7). There is deliberately **no
   automatic guess** — guessing by weight proximity is exactly how one family member's
   body-composition ends up pushed to another's Garmin account (§0.2 Correction 1).
   Option (a), one token-scoped bridge per person, remains supported by the same resolution
   order for anyone who later adds a dedicated scale; it simply is not this deployment's setup.
   **This adds nothing to phase 2** — the person-scoped route is the confirmation mechanism.

5. **What is the primary person called? — the default: the first admin's username, slugified.**
   Overridable by setting `VITALFORGE_PRIMARY_PERSON` in `.env`. No custom name is being
   chosen up front.
   **The operational rule survives unchanged: it is read once, during a migration that runs
   once, so it must be set *before* the upgrade** (§g.2 step 4). Getting it wrong is
   recoverable — display name and slug can both be renamed afterwards — but the old slug is
   not redirected.

6. **Do pre-existing non-admin accounts get access? — NO, and it affects nobody.**
   The migration grants access only to the first admin; fail-closed, because it cannot know
   who should see whose health data. The stated risk was that a second existing account
   (a spouse already seeing the dashboard) would silently lose access. **Confirmed: no second
   account exists in this deployment**, so the fail-closed default cuts no one off and there is
   no access to restore. It stays the rule for any non-admin account created before the upgrade
   actually runs, and a one-line release note covers it (§g.1).

7. **Person selection in URLs: path-based (`/p/{slug}/api/...`). — Confirmed.**
   As recommended in §f.2: it gives the PWA service worker a real cache boundary (one person's
   cached responses can never be served to another) and has no "forgot the parameter, silently
   got the default" failure mode. The churn in both services' templates, static JS, and
   service-worker cached-URL lists is accepted — it is mechanical, and it lands in exactly the
   layer that has to become person-aware regardless.

8. **How long do old API URLs keep working? — They don't. Direct cutover, no aliases.**
   **This is the one answer that changed the design.** The alias layer was designed against an
   inference — `bootstrap_migrated_token` exists, therefore automation clients are calling the
   un-scoped paths. Confirmed with the owner: **nothing calls `/api/weight` or
   `/api/metrics/...` today.** Tokens were issued; no client uses those routes.
   So phase 2 unmounts them in the same change that mounts `/p/{slug}/api/...`. No aliases, no
   deprecation window, no alias-removal work in phase 5, and no window to decide the length of.
   Two things get *better*, not merely cheaper: §f.2's "no implicit fallback" rule now has no
   exception anywhere in the design, and `Depends(require_person(...))` becomes the sole
   supplier of `person_id` with no second resolution path to audit. If a forgotten client ever
   does surface, it gets a loud `404` — it cannot read or write the wrong person's data — and
   the fix is to point it at the new URL, not to build the layer back. Full reasoning: §f.8.

9. **Should `weight_log.person_id` eventually become `NOT NULL`? — No, and probably not ever.**
   Confirmed as recommended. Two reasons, both of which correct claims an earlier draft of this
   document got wrong:
   - *It is not blocked by mechanics.* Adding `person_id INTEGER NOT NULL DEFAULT 0` is a
     fast, metadata-only change in SQLite (the repo already does exactly this for
     `users.session_version`). The objection is **semantic**: person 0 does not exist, so a
     defaulted sentinel produces rows that are structurally valid and meaningless — and it
     destroys the audit query `SELECT COUNT(*) FROM weight_log WHERE person_id IS NULL`, which
     is the one cheap check that catches an INSERT that forgot to pass a person. Given that
     this document's own review found exactly such an omitted INSERT, that query is worth more
     than the constraint.
   - *Making it `NOT NULL` properly is not cheap.* It requires rebuilding `weight_log`, and
     `weight_log` uses `AUTOINCREMENT`. A create/copy/drop/rename rebuild **resets the
     `sqlite_sequence` high-water mark** unless it is explicitly carried over — and those `id`
     values escape to API clients and are the addressing key for `DELETE /api/weight/{id}`. A
     reset high-water mark means reissued ids and a client-side delete hitting a different
     person's row. Any future proposal to do this must state how `sqlite_sequence` is
     preserved.

10. **What happens to a person's data when their Garmin link is removed? — Keep the data,
    delete the credential.** Confirmed as recommended. Unlinking removes the link row and
    deletes that person's stored Garmin tokens from disk — a link the UI says is gone must not
    leave a live credential behind (threat T9) — but the historical metrics stay. Archiving a
    person unlinks first, for the same reason.
    The opposite reading ("unlink means forget me") was the reason to ask: it is a reasonable
    expectation and the difference is destructive and irreversible. Unlink is not a delete;
    deleting a person's history stays a separate, explicit action.

11. **Is the recommendations cache fixed in the schema phase, not deferred? — YES, phase 1.**
    `recommendations.py` holds a single-slot module-level cache keyed on a hash of the metric
    data. With several people it does not leak one person's recommendations to another (the
    hash is derived from that person's own data, so a mismatch is a cache miss, and the only
    collision — two people with no data at all — produces identical output anyway). What it
    does do is thrash: person A's read evicts person B's, every time. The fix is one line —
    key the cache by person — and the file is already being edited in phase 1.
    This was flagged only because an earlier draft deferred it to the final phase while listing
    the same file in the first, which is a contradiction someone would otherwise have resolved
    mid-implementation. It is resolved here: phase 1, in the same change.

12. **Is a schema-version guard worth adding now? — YES. It is being built, in phase 0.**
    This is a firm decision, not a "cheap enough to consider": `assert_schema_understood`
    (§c.3) is a phase-0 deliverable alongside the runner, and each service refuses to serve a
    database carrying a migration marker its code does not know.
    It needs **no new table and no new column** — `schema_migrations` already exists for the
    runner, and the set of applied marker names *is* the version (§b.4). A fresh or pre-runner
    database has zero markers, which passes; the guard only ever fires on a name from the
    future.
    Its limit is unchanged and still worth stating: it **cannot** protect the 001 migration,
    because the image being rolled back to predates the guard — and that is precisely the
    danger §c.7 describes, an old image reading the new schema *successfully* and silently
    merging several people's data into one chart. The pre-migration snapshot is what covers
    that one. The guard covers every non-additive migration after it, for the cost of one table
    read per boot.

13. **Are the three deliberate edge states accepted? — YES, all three.** They are deliberate
    rather than oversights, and each will look like a bug the first time it is hit:
    - A person can end up with **zero** grants (delete the last user who had one, or revoke
      your own last `own` grant). Admins bypass grants, so it is always recoverable.
    - If `VITALFORGE_PASS` is never set, the `users` table stays empty and the app runs in
      open-access mode where anonymous has full control of **every** person — the same
      intentional local-dev behavior as today, but now spanning several people's health data
      and (after the Garmin phase) several Garmin accounts.
    - An **archived** person's URL slug stays permanently reserved. This is chosen so an old
      bookmark can never resolve to a different human's data; the escape hatch is renaming the
      archived person to free the name.

---

## Appendix A — Why this contradicts `shared/database.py`'s stated principle, and why that is correct

`shared/database.py:8-19` and `docs/prp/00-design.md` §5.4 establish additive-only migration as
a deliberate design rule, justified by a specific hazard: a table rewrite "reintroduces a real
interruption window for *container killed during first boot after upgrade*."

That justification rests on an assumption this design tested and found to be **false for
SQLite**: that an interrupted table rewrite leaves a torn intermediate state. It does not.
`CREATE` / `INSERT…SELECT` / `DROP` / `ALTER…RENAME` inside `BEGIN IMMEDIATE` roll back
together, leaving the original table byte-identical (§c.1, verified on SQLite 3.50.2, WAL,
foreign keys off, autocommit + explicit `BEGIN IMMEDIATE`).

**Two scoping statements, both load-bearing:**

1. **This claim is one gating test away from being fully established** — the verification used
   stdlib `sqlite3`, and §c.8's first test re-runs it through `aiosqlite`. If that test fails,
   this appendix's conclusion is wrong and the original additive-only justification stands as
   written. Do not cite this appendix as settled until that test is green.
2. **"Byte-identical" describes the rebuild, not the migration as a whole.** §c.6 spells this
   out: `_add_columns` commits `weight_log.person_id` at `init_db()` step 2, outside the
   rebuild's transaction, so a kill can leave that one nullable column committed with no
   marker. It converges on the next boot and loses nothing, but a reader who takes
   "byte-identical" as a statement about the whole migration will write the wrong interruption
   test — one that passes while that hole stays open.

The additive-only rule was still the right call for the migrations that motivated it: for
adding nullable columns, `ADD COLUMN` is O(1) and needs no runner at all, so the rule bought
real simplicity for zero cost. It is being set aside here only because the requirement —
changing a PRIMARY KEY — has no additive form, and the hazard it was protecting against turns
out not to be the one that exists.

**A second, narrower correction in the same corpus.** `docs/prp/00-design.md:1596-1598` says
"any future migration that adds a defaulted column rewrites the table." That is imprecise:
`shared/database.py:36-38` states the correct rule (a **constant** default is metadata-only
and fast; a non-constant one is the rewrite case), and `_USERS_ADDITIVE_COLUMNS` relies on it
in production today. The imprecise version is what led the first draft of this document to
give a false mechanical reason for `weight_log.person_id` being nullable (§b.3). Phase 0 fixes
the source, not just this document.

**The hazards that do exist**, and that §(c) is actually designed around, are: the two-service
startup race (a rebuild is not idempotent the way attempt-and-swallow `ADD COLUMN` is), the
5-second default busy timeout, the second-connection-inside-`init_db()` deadlock trap, and —
most importantly — the loss of the silent-rollback-safety property, where an old image reads
the new schema successfully and returns merged data instead of erroring (§c.7).

If this design is accepted, `shared/database.py`'s comment should be amended rather than
deleted: additive-only remains the default and the burden of proof stays on anything else,
but the stated *reason* should be corrected to "no runner needed, no rollback hazard" rather
than "rewrites are interruption-unsafe," which measurement does not support.

## Appendix B — Files touched, by phase

Source, tests, **and documentation**. The first draft listed only source and tests, which left
three mitigations it required (§c.7's README upgrade section, the `00-design.md` correction,
the `CLAUDE.md` convention updates) with no phase that performs them.

| Phase | Files |
|---|---|
| 0 | `shared/migrations.py` (new — runner **and** the `assert_schema_understood` guard, §i Q12), `shared/database.py` (`init_db` calls the guard as step 5, `get_db` busy_timeout, `_add_columns` pre-check, comment amendment per Appendix A), `tests/test_migrations.py` (new), **`docs/prp/00-design.md`** (correct `:1596-1598`'s defaulted-column claim) |
| 1 | `shared/database.py`, `shared/migrations.py`, `vitalforge-dashboard/sync.py`, `vitalforge-dashboard/app.py`, `vitalforge-dashboard/recommendations.py`, `vitalforge-weight/app.py`, tests, **`CLAUDE.md`**, **`README.md`** |
| 2 | `shared/auth.py`, `shared/database.py` (`api_tokens.person_id`), both `app.py`, both `templates/`, both `static/` (incl. service workers), `tests/test_smoke_ui.py` + `tests/live_server.py`, other tests, **`README.md`** |
| 3 | `shared/garmin_client.py`, `shared/garmin_registry.py` (new), `shared/database.py`, `vitalforge-dashboard/app.py`, `vitalforge-weight/app.py`, `tests/conftest.py`, tests, **`.env.example`**, **`README.md`**, **`CLAUDE.md`** |
| 4 | `vitalforge-dashboard/sync.py`, `vitalforge-dashboard/app.py`, tests, **`.env.example`** (`SYNC_BACKFILL_DAYS`) |
| 5 | dashboard `templates/`/`static/` (comparison + household views), `vitalforge-dashboard/app.py`, tests |

**`CLAUDE.md` edits, specifically** — it currently states things this work falsifies:

- The repo-layout block says `database.py # ... CREATE TABLE IF NOT EXISTS, no migrations`.
  After phase 1 there is a migration runner and one applied migration. (Phase 1.)
- The "blast-radius module" bullet lists `shared/auth.py`, `database.py`, `garmin_client.py`.
  Phase 3 adds `shared/garmin_registry.py` to that set. (Phase 3.)
- The `METRIC_TABLES` convention bullet ("update `shared/database.py`, `sync.py`, and
  `METRIC_TABLES` together") stays at **three** places — §c.5 deliberately derives the rebuild
  column list from the live schema rather than adding a fourth. Add one sentence saying so,
  and saying that a new metric table must be added to `_REBUILD_TABLES`' **name** list if it
  is created before a future rebuild. (Phase 1.)
- The dashboard-read-endpoints bullet ("`/api/metrics/{name}` ... only read from local SQLite")
  keeps its meaning but its URLs change in phase 2. (Phase 2.)

**Phase 2 and the Playwright constraint.** Phase 2 touches `tests/test_smoke_ui.py` and
`tests/live_server.py`, named here explicitly. `CLAUDE.md` is emphatic that these run as a
**separate process** under `pytest -q -m playwright`, that `addopts = "-m 'not playwright'"`
must never be removed, and that the two suites must never be merged into one invocation
(`pytest-playwright`'s session-scoped `browser` fixture keeps its own event loop running and
breaks `pytest-asyncio` for every async test after it). Person-scoped URLs mean the smoke
tests' navigation paths change; that is a URL edit, **not** a reason to reorganize the suites.

**Phase 3 and `tests/conftest.py`.** It monkeypatches `shared.garmin_client` at module level
with a fake backed by `tests/fixtures/garmin/`. A registry keyed by `person_id` changes that
patch surface, so the fixture layout needs a per-person dimension too — plan for it rather
than discovering it mid-phase.

## Appendix C — Revision log

What the adversarial review changed, by its own finding IDs. Every finding is listed; none is
dismissed without a reason grounded in the code.

| # | Severity | Disposition | Where |
|---|---|---|---|
| 1.1 | CRITICAL | Fixed. Atomicity restated as "the *rebuild* is fully-old-or-fully-new"; step-2's independent commit spelled out; interruption test now asserts the `table_info(weight_log)` delta | §c.6, §c.8, Appendix A |
| 1.2 | CRITICAL | Fixed. `VACUUM INTO` a `.partial`, `PRAGMA integrity_check`, then `os.rename`; fixed-name skip now only ever sees verified files | §c.7 |
| 1.3 | HIGH | Fixed. Failure message names cause, fix and escape hatch (`VITALFORGE_SKIP_MIGRATION_SNAPSHOT`); same-volume limitation stated; §g.2 keeps the separate volume backup | §c.7, §g.2 |
| 1.4 | HIGH | Fixed structurally. `init_db()` closes its connection before the snapshot and `run_migration`; the latent seed-`INSERT` trap is documented in the docstring; a cross-connection gating test added | §c.4, §c.3, §c.8 |
| 1.5 | MEDIUM | Fixed. Shape pre-check added to `_add_columns` (explicitly a latency optimization allowed to be wrong); "moves the threshold, does not remove the loop" stated plainly | §c.3 |
| 1.6 | MEDIUM | Fixed. `run_migration` logs elapsed time at WARNING; §c.6's ~10 s trigger now references it | §c.3, §c.6, §g.2 |
| 1.7 | MEDIUM | Fixed. `_REBUILD_TABLES` reduced to table names; columns derived via `PRAGMA table_info` with a fail-loud shape guard; `CLAUDE.md` convention stays at three places and gets one clarifying sentence | §c.5, Appendix B |
| 1.8 | MEDIUM | Fixed. `shared/database.py:250` becomes the new index; legacy `DROP INDEX` moves into the migration only; schema-parity test added | §b.3, §c.8 |
| 1.9 | LOW | Fixed. Explicit `except BaseException: await db.rollback(); raise` | §c.3 |
| 2.1 | CRITICAL | Fixed. `persons.is_primary` + unique partial index replaces `ORDER BY id LIMIT 1`; archiving the primary is refused | §a.2, §d.5, §f.6, §g.1 |
| 2.2 | HIGH | Fixed. Both lifespans stop calling `authenticate()`; lazy per-person auth via `ensure_authenticated`, inside the token bucket | §d.5, §e.3, §(h) |
| 2.3 | HIGH | Fixed. Token dir resolved in a new async `shared/garmin_registry.py` and passed down as a `Path`; `garmin_client` stays DB-free and synchronous | §d.5 |
| 2.4 | HIGH | Fixed. Link route runs through the token bucket plus a per-user attempt cap; T2 rewritten | §d.3, §e.3 |
| 2.5 | MEDIUM | Fixed. New threat T9: unlink deletes the token directory after commit, failure logged at ERROR; archive unlinks first | §d.3, §f.6, §i Q10 |
| 2.6 | MEDIUM | Fixed. Explicit `mkdir(mode=0o700)` plus `chmod(0o700)` on the parent, with the `exist_ok` caveat in the docstring | §d.5, T1 |
| 2.7 | MEDIUM | Fixed. New threat T10: `last_auth_error` becomes a bounded code plus its own timestamp column | §d.3, §d.4 |
| 2.8 | LOW | Fixed by removing the unverified dependency. Fallback keyed on a shape test, not a filename; verifying the real garth filename is an explicit phase-3 entry task; the "self-resolves on re-link" claim corrected (re-linking needs a password nobody stores) | §d.5, §(h) |
| 3.1 | CRITICAL | Fixed — this was the missing leg, not a correction. New §f.7 specifies ingest attribution; `api_tokens.person_id` promoted from phase 5 to phase 2; the shared-scale limitation named honestly and surfaced as a human decision | §f.7, §(h), §i Q4 |
| 3.2 | HIGH | Fixed. The false "structurally impossible to omit" claim deleted; safety relocated to `Depends(require_person(level))` as the sole supplier of `person_id`; F2 rejustified on cache boundary + no implicit fallback | §f.1, §f.2 |
| 3.3 | HIGH | Fixed. Identity and grant resolved in one query, so the docstring's claim is now true | §f.1 |
| 3.4 | HIGH | Fixed. `_SLUG_RE` + `_RESERVED_SLUGS` mirroring `_RESERVED_USERNAMES`; `_ensure_primary_person` slugifies rather than copies | §f.4, §g.1 |
| 3.5 | MEDIUM | Fixed. Orphaned-person state stated as deliberate and why; `granted_by` added to the cascade as a NULL-out | §f.5, §f.6, §i Q13 |
| 3.6 | HIGH | Fixed at the time by a new §f.8 (time-boxed compatibility aliases that 400 rather than guess). **Superseded on 2026-08-26** — the finding assumed live legacy clients; there are none, so §f.8 is now a direct cutover with no alias layer. See the decisions entry below | §f.8, §(h), §i Q8 |
| 3.7 | LOW | Fixed by deciding. Global slug uniqueness including archived persons, plus a rename path; reasoning stated | §f.4, §i Q13 |
| 3.8 | (restatement) | Adopted. Open-access blast radius stated explicitly and routed to the README | §f.3 |
| 4.1 | HIGH | Fixed. The false mechanical reason deleted and replaced with the real semantic/auditability one; the upstream `00-design.md:1596-1598` error made a phase-0 deliverable; §i Q9 re-argued | §b.3, §i Q9, Appendix A, Appendix B |
| 4.2 | HIGH | Fixed. `sqlite_sequence` reset hazard added to §i Q9, with the `weight_log` id escape path (`app.py:397`, `DELETE /api/weight/{id}`) cited | §i Q9, §b.1 |
| 4.3 | HIGH | Fixed. Appendix B now lists `CLAUDE.md`, `README.md`, `.env.example` and `docs/prp/00-design.md` by phase, with the specific `CLAUDE.md` sentences that become false enumerated | Appendix B, §(h) |
| 4.4 | MEDIUM | Fixed. Snapshot named as a second copy of sensitive data inheriting the PRIVACY rule; deletion given a runbook trigger (verified-good + 7 days) rather than left implicit | §c.7, §g.2 |
| 4.5 | MEDIUM | Fixed. `tests/test_smoke_ui.py` and `tests/live_server.py` named explicitly with the separate-process constraint restated; the `tmp_path` snapshot materialization stated as expected | Appendix B, §c.8 |
| 4.6 | (verified sound) | No change. `docker-compose.yml` has no `depends_on` and both services are `restart: unless-stopped`; the `00-design.md` citations check out | §0.1 |
| 5.1 | (question answered: no) | Adopted. Appendix A's byte-identity claim now explicitly scoped to the rebuild, matching §c.6 | Appendix A |
| 5.2 | CRITICAL | Fixed. Inventory rebuilt as 13 statements in four groups (reads / writes / safe-by-dependency / signature chain), "complete list" claim removed, `weight_log` INSERT and both `UPDATE`s added, "no default `person_id`" made a rule | §0.2 |
| 5.3 | HIGH | Fixed. `recommendations.py` cache keyed by person **in phase 1**, matching its Appendix B placement; §i Q11 rewritten to say the defect was the phasing contradiction, not a leak | §(h), §i Q11 |
| 5.4 | (placeholders) | All resolved: `_rebuild_sync_status` defined; `backoff_until` decided (created in 001, first written in phase 4); round-robin cursor derived from `MIN(last_sync_time)` with no new state; `_REBUILD_TABLES` and `authenticate()` written out in full; the snapshot's placement fixed in `init_db` after the connection closes | §c.5, §b.2, §e.3, §c.4 |

### 2026-08-26 — the thirteen open questions, closed

The deployment's owner answered every question in §(i). That section is no longer a list of
open questions; it is the decision record, with the `Q<n>` labels preserved so existing
cross-references still resolve. Eleven answers confirmed the document's own recommendations
and changed no design text. Two did more:

| Q | Decision | What changed in the document |
|---|---|---|
| Q8 | **No compatibility aliases.** Confirmed that nothing calls `/api/weight` or `/api/metrics/...` today, so phase 2 unmounts the legacy paths in the same change that mounts `/p/{slug}/api/...` — no alias layer, no deprecation window, no alias-removal work | §f.8 rewritten as a direct cutover and the reasoning for why it is *safer*, not merely cheaper; §a.2 and §f.2 lose the alias as a consumer of `default_person_id`, so "no implicit fallback" now has zero exceptions; §f.2's URL table row becomes "unmounted, router 404s"; §(h) phase 2 gains the unmount, phase 5 loses the removal (and **stays a phase** — its remaining scope is dashboard comparison/household views, unrelated to phase 4's scheduler); Appendix B phase 5 row drops the alias removal and the README deprecation notice; finding 3.6 above marked superseded |
| Q12 | **Build the schema-version guard**, in phase 0 — a firm yes, not the earlier "cheap now / pure cost if not needed" framing | `assert_schema_understood` specified at the end of §c.3 and called as `init_db()` step 5 in §c.4; §b.4 states it needs no new table or column (the `schema_migrations` marker set *is* the version); §c.7 mitigation 2 restated as decided; Appendix B phase 0 row names it. §(h) phase 0 already listed it, so this removes a latent contradiction rather than adding scope |

The other eleven, for the record: **Q1** maintenance-window cutover accepted; **Q2** per-person
garth token stores, no encrypted passwords; **Q3** N = 4 exactly (so round-robin gives each
person an 8 h refresh at the default `SYNC_INTERVAL_HOURS`, and the >10-person branch becomes
an if-it-ever-changes note); **Q4** shared scale with manual PWA confirmation, no auto-guessing
— which adds nothing to phase 2, since the person-scoped route *is* the confirmation; **Q5**
primary person named from the first admin's username, `VITALFORGE_PRIMARY_PERSON` still must be
set before the upgrade if that is wrong; **Q6** no access for pre-existing non-admin accounts,
and no such account exists, so nothing is cut off; **Q7** path-based URLs; **Q9**
`weight_log.person_id` stays nullable; **Q10** unlink keeps history and deletes the credential;
**Q11** recommendations cache keyed by person in phase 1; **Q13** all three edge states
accepted.
