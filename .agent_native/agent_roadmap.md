# VitalForge — Agent-Native Roadmap

Goal: an AI agent can take a raw bug report or feature request against this repo and
autonomously reproduce, implement, test, and verify it, with minimal human input.

Ranked by **Human-Attention-Saved per Unit of Effort** (highest leverage first). Top 5 are
scoped to be immediately actionable — concrete files, commands, and acceptance criteria.

## 1. Add a pytest suite with a fake Garmin client — DONE

Implemented: `pyproject.toml` (pytest/pytest-asyncio/httpx, `pythonpath = ["."]`,
`asyncio_mode = "auto"`), `tests/conftest.py` (isolated tmp-path SQLite DB per test via
`monkeypatch.setattr(shared.database, "DB_PATH", ...)`, `FakeGarminClient` patched onto
`shared.garmin_client._client`/`authenticate`, plus per-app-module patches for the two
`vitalforge-*/app.py` files since they import `authenticate`/`push_weight` by name rather
than via the module), `tests/test_weight_api.py` (9 tests), `tests/test_dashboard_api.py`
(19 tests incl. all 13 `METRIC_TABLES` keys). `vitalforge-weight` and `vitalforge-dashboard`
are hyphenated directories, so both are loaded in tests via `importlib.import_module(...)` —
the same mechanism `uvicorn vitalforge-weight.app:app` relies on.

`.github/workflows/docker.yml` now has a `test` job (installs both requirements files +
pytest/ruff, runs `ruff check .` then `pytest -q`) that gates `build-and-push` via `needs: test`.

Verified: `pytest` — 28 passed, 0 failed, no Docker/network/Garmin, run from a fresh venv
against the actual `requirements.txt` files.

**Original rationale, for reference:**

**Why this is #1:** there are zero automated tests, zero lint/type checks, and no CI test
gate anywhere in the repo (`.github/workflows/docker.yml` only builds/pushes images). Right
now an agent has no way to confirm a change works other than a human eyeballing `curl` output
or Docker logs — every change requires a human in the loop to "look at it and say it's fine."

**Effort:** medium (one afternoon). **Attention saved:** every future change, forever.

**Files to add:**
- `pyproject.toml` (repo root) — minimal, adds `pytest`, `pytest-asyncio`, `httpx` as dev deps,
  and fixes the `shared/` import hack by declaring `shared` as a package on `sys.path` via
  `[tool.pytest.ini_options] pythonpath = ["."]`. This also lets item 5 (packaging) piggyback
  on the same file.
- `tests/conftest.py` — fixtures that:
  - set `DB_PATH` and `GARTH_TOKEN_DIR` to a `tmp_path` before importing `shared.database` /
    `shared.garmin_client` (so tests never touch `/app/data` or a real `.garth` token cache),
  - monkeypatch `shared.garmin_client.authenticate` to a no-op and `shared.garmin_client._client`
    to a fake object exposing `add_body_composition`, `get_sleep_data`, `get_user_summary`,
    `get_hrv_data`, `get_body_battery`, `get_stress_data`, `get_max_metrics`,
    `get_weigh_ins`, `get_training_status` — each returning canned dicts shaped like real
    Garmin responses (see item 2 for where those fixtures live).
- `tests/test_weight_api.py` — `httpx.AsyncClient` against `vitalforge-weight/app.py`'s `app`:
  POST `/api/weight` with `lbs` and `kg`, assert conversion math and `synced_to_garmin`;
  GET `/api/weight/recent` and `/api/weight/trend`; DELETE `/api/weight/{id}` including the
  404 case.
- `tests/test_dashboard_api.py` — same pattern against `vitalforge-dashboard/app.py`: seed the
  DB directly (reuse the seed helper from item 2), then hit `/api/metrics/{name}` for each key
  in `METRIC_TABLES`, `/api/sync/status`, and `/api/recommendations/rules-only`.

**Acceptance criteria:** `pytest` runs from repo root with no Docker, no network, and no real
Garmin credentials, and exercises both services' full route tables. Add a `test` job to
`.github/workflows/docker.yml` that runs `pytest` and gates the existing build-and-push job.

## 2. Synthetic Garmin fixtures + a DB seed script — DONE

Implemented: `tests/fixtures/garmin/*.json` (8 files, one per Garmin endpoint shape, all
hand-built/invented values matching the field names `sync.py` reads — e.g.
`sleepScores.overall.value`, `hrvSummary.lastNightAvg`, `latestWeight.weight`/`bmi`/`bodyFat`).
`scripts/seed_db.py` — `python scripts/seed_db.py --days N --db-path PATH [--pattern
normal|declining-hrv|declining-sleep|overtraining] [--seed N]` — inserts synthetic rows across
all 10 metric tables (`sleep`, `resting_hr`, `hrv`, `body_battery`, `stress`, `vo2max`,
`weight_history`, `training_load`, `steps`, `active_calories`) with a configurable linear
trend, and refuses to write to any path named `fitness.db` or containing `.garth`.

Verified end-to-end (confirms the roadmap's key insight — dashboard reads never touch Garmin):
seeded a tmp DB with `--pattern overtraining`, then hit the real `vitalforge-dashboard` app
(via `importlib.import_module` + `httpx.ASGITransport`, zero Garmin) — `/api/metrics/hrv`
returned all seeded points and `/api/recommendations/rules-only` correctly fired
`sleep_declining`, `hrv_below_baseline`, `rhr_trending_up` findings. Repeated with
`--pattern declining-hrv`, confirming `hrv_below_baseline`/`hrv_weekly_drop` fire instead.

**Original rationale, for reference:**

**Why:** bug reports will reference specific data shapes ("my HRV chart is wrong after date
X", "recommendations don't fire for declining sleep"). Because dashboard read endpoints never
call Garmin at request time (they only read `shared/database.py`'s tables — confirmed by
reading `vitalforge-dashboard/app.py`), an agent can reproduce almost any dashboard bug by
seeding the DB directly, with zero Garmin dependency.

**Effort:** small. **Attention saved:** turns "please share your data so I can debug this"
into something an agent can self-serve.

**Files to add:**
- `tests/fixtures/garmin/*.json` — one file per Garmin endpoint shape (`sleep_data.json`,
  `user_summary.json`, `hrv_data.json`, `body_battery.json`, `stress_data.json`,
  `max_metrics.json`, `weigh_ins.json`, `training_status.json`), hand-built from the field
  names `sync.py` actually reads (e.g. `sleepScores.overall.value`, `latestWeight.weight`,
  `bmi`, `bodyFat` — see `vitalforge-dashboard/sync.py:47-56` and `:206-236`). Use invented
  numbers only — never copy values from any real `fitness.db` or Garmin export found on disk.
- `scripts/seed_db.py` — CLI script: `python scripts/seed_db.py --days 90 --db-path /tmp/vf.db`,
  inserts synthetic rows across all tables in `shared/database.py::init_db` (sleep, resting_hr,
  hrv, body_battery, stress, vo2max, weight_history, training_load, steps, active_calories)
  with a configurable trend (e.g. `--pattern declining-hrv`, `--pattern overtraining`) so an
  agent can reproduce a specific reported pattern and then check whether
  `recommendations.py::run_rules` fires the corresponding finding.

**Acceptance criteria:** `python scripts/seed_db.py --days 30` followed by starting the
dashboard against that DB (`DB_PATH=...`) makes every chart and `/api/recommendations/rules-only`
finding populated and inspectable without Garmin credentials.

## 3. `CLAUDE.md` with verified commands and chokepoints — DONE this session

Written to repo root. Documents: which compose file to use for dev vs. prod, that `shared/` is
a shared-blast-radius module imported via a `sys.path` hack, that dashboard reads never hit
Garmin, `DB_PATH`/`GARTH_TOKEN_DIR` overrides for sandboxed runs, the shared-secret cross-service
cookie design (so an agent doesn't "fix" it), and the fact that no lint/test gate exists yet.

## 4. Extract `shared/` into a real installable package — DONE

Implemented: `pyproject.toml` now has a `[build-system]` (setuptools) and
`[tool.setuptools] packages = ["shared"]`. Both Dockerfiles now `COPY pyproject.toml . &&
COPY shared/ /app/shared/ && RUN pip install --no-cache-dir -e .` before copying the service
directory, instead of a raw `COPY shared/`. Removed the `sys.path.insert(0, .../parent.parent)`
"make shared importable" hack from all 4 files that had it: `vitalforge-weight/app.py`,
`vitalforge-dashboard/app.py`, `vitalforge-dashboard/sync.py`,
`vitalforge-dashboard/recommendations.py` (one more file than the roadmap's original "3 files"
estimate — `recommendations.py` had the same hack). `vitalforge-dashboard/app.py` keeps its
*second* `sys.path.insert` (for sibling bare-name imports of `sync`/`recommendations` — a
separate, unrelated pattern, intentionally left alone).

**Verified (no Docker, per hard rule):** in a fresh venv, `pip install -e .` then `cd /tmp &&
python -c "import shared.database"` succeeds from an arbitrary cwd with no path hacks. Full
pytest suite (28 tests) and `ruff check .` both pass afterward. **Not verified:** an actual
`docker build`/`docker compose up` of the updated Dockerfiles — Docker was off-limits for this
task, so treat the Dockerfile edits as reviewed-but-unbuilt and sanity-check them (or run
`docker compose up --build`) before relying on them in production.

## 5. Add ruff + a minimal `pyproject.toml` lint config — DONE

Implemented: `[tool.ruff]` (line-length 120, target py312) and `[tool.ruff.lint]` (select
E/F/W/I, ignore E501) in `pyproject.toml`. Fixed the small number of genuine findings by hand
first (removed two truly-unused imports in `shared/auth.py` and one in
`vitalforge-dashboard/recommendations.py`), then ran `ruff check . --fix` for the remaining
purely-mechanical import-sort (I001) findings across 5 files. Added as a step in the CI `test`
job from item 1 (`ruff check .` runs before `pytest`).

**Verified:** `ruff check .` → `All checks passed!` in a fresh venv; full pytest suite still
green after the import reordering (28 passed).

---

## Other findings (lower priority / informational)

- **No API-level regression fixture for the recommendations engine — DONE.** `recommendations.py`'s
  `run_rules` (526 lines, 18 distinct rule keys across sleep/recovery/stress/body-composition/
  activity/correlations) is now covered directly in `tests/test_recommendations.py` (26 tests):
  one positive trigger test per rule, a couple of boundary tests (e.g. 2 consecutive low-sleep
  days doesn't trigger, 3 does), a "stable healthy data -> zero findings" test, and direct tests
  of the numeric helpers (`avg`, `trend_slope`, `consecutive_below`/`consecutive_above`). Pure
  function, no DB/HTTP/Garmin fixtures needed. The LLM path (`get_llm_recommendations`)
  remains untested, as originally recommended — it should stay mocked/untested against the
  real Anthropic API in CI.
- **No screenshot/visual verification path — DONE.** `tests/test_smoke_ui.py` now runs 3
  Playwright smoke tests (page loads, core elements render, zero console/page errors, one
  real form-submit interaction) against both PWAs, served for real over HTTP via
  `tests/live_server.py::LiveServer` (uvicorn in a background thread, same faked DB/Garmin
  fixtures as the API tests). **Important:** these are marked `@pytest.mark.playwright` and
  excluded from the default `pytest -q` run (`addopts` in `pyproject.toml`) — running them in
  the same process as the async API tests breaks pytest-asyncio's fixture setup, since
  pytest-playwright's browser fixture keeps its own event loop running in the main thread for
  the rest of the session. Run them separately: `pytest -q -m playwright` (needs
  `playwright install chromium` first). See `.github/workflows/docker.yml`'s `test` job for
  the CI wiring.
- **No DB migration mechanism — DONE.** `shared/database.py::init_db` now runs additive
  `ALTER TABLE ADD COLUMN` migrations for `weight_log` (body-composition intake columns) and
  `weight_history` (Garmin-sourced composition read path), plus an `auth_migrations` table that
  durably marks one-time auth data migrations so they don't re-run. Still additive-only (no
  column rename/drop/type-change support) — flag that gap if a future task needs to alter or
  remove an existing column rather than add one.

---

## Feature roadmap — user-facing additions (competitive research, 2026-08-24)

Sourced from a survey of open-source self-hosted health dashboards (garmin-grafana, aurboda,
FIT Dashboard) and commercial wearable platforms (Whoop, Oura, Garmin Connect's own web app,
TrainingPeaks, Apple Health). None of these are started; none have a design doc yet. Verified
against the current codebase first — items that already exist (e.g. training-load/ACWR charting)
were excluded rather than re-proposed. Ordered cheapest-to-build first; the last two are
explicitly heavier and don't fit the current single-account architecture as-is (called out
below), but are included per request.

### Buildable from data already synced — no schema change, no new integration

1. **Data export (CSV/JSON)** — near-universal in comparable products (garmin-grafana's CSV
   export, Health Auto Export's JSON+CSV, Apple Health export tools); VitalForge has none today.
   Shape: `GET /api/export?metric=all&days=N&format=csv|json` in `vitalforge-dashboard`,
   streaming from the existing `METRIC_TABLES` map. Effort: small.

2. **Composite readiness/recovery score (0–100)** — the single headline number Whoop
   (Recovery) and Oura (Readiness) show, blending HRV-vs-personal-baseline, RHR trend, and
   sleep. Every input (`hrv`, `resting_hr`, `sleep`, `body_battery` tables) is already synced;
   VitalForge just has no aggregate. Shape: new `readiness.py` in `vitalforge-dashboard`, pure
   function reusing `recommendations.py`'s `avg`/`trend_slope` pattern, one `/api/readiness`
   endpoint, a headline tile in `index.html`. Effort: small–medium.

3. **Notable-change / anomaly alerts** — Apple Health's "Trends" flags a metric moving outside
   its own rolling baseline, distinct from the rules engine's fixed thresholds. Shape: one more
   rule function reusing the existing `avg`/`trend_slope`/`consecutive_below` helpers already in
   `recommendations.py`. Effort: small–medium.

4. **Ad-hoc cross-metric correlation view** — the open-source `aurboda` project computes
   Pearson correlation across tracked metrics on demand ("does evening exercise affect sleep
   score?") instead of hardcoding specific pairs like the current rules engine does. Shape:
   a Pearson-r stats helper + one endpoint returning a correlation matrix over synced metrics +
   a Chart.js scatter/heatmap (Chart.js is already in the stack). Effort: medium.

### Needs a schema change

5. **Goal / target tracking** — set a target (weight, body-fat %) and see projected
   time-to-goal, as in TrainingPeaks and consumer apps like iGoal Plus. `trend_slope` already
   exists in the rules engine, so ETA math is mostly reuse. Shape: new `goals` table (`user_id`,
   `metric`, `target_value`, `target_date`), small CRUD surface, progress widget. Effort: medium.

### Needs a new external data source / bigger architectural lift

6. **Local FIT-file import without cloud login** — modeled on FIT Dashboard: feed in FIT/TCX/GPX
   files you already own, no Garmin Connect credentials required. This is a genuinely new
   ingestion path alongside the existing Garmin Connect sync (`sync.py`), not an extension of
   it — a file upload endpoint, a FIT/TCX parser dependency, and a mapping from parsed fields
   into the existing metric tables (or a new raw-activity table, if the shape doesn't match
   `METRIC_TABLES`). Real value mainly as a hedge if Garmin API access ever becomes unreliable.
   Effort: large.

7. **Family / multi-person comparison dashboards** — none of the researched products (open-source
   or commercial) do this well for a single-account use case, and it cuts against VitalForge's
   current per-user auth model (`shared/auth.py`, one `users` row = one Garmin-linked account,
   sessions scoped to that user). Building it well would mean deciding whether "family" means
   separate linked Garmin accounts under one household view (multiple `garmin_client` sessions,
   real access-control questions about who can see whose data) or just side-by-side chart
   overlays for accounts that already exist — those are very different scopes and need their own
   design discussion before estimating effort. Flagging as the one item here that isn't
   actionable without a scoping conversation first.

---

## Implementation plans (workflow-generated, 2026-08-24)

Each item below was independently planned against the real codebase, then verified for
feasibility by a second agent that re-read the cited files rather than trusting the plan's
claims. Verdicts below are APPROVE unless a required fix is called out.

### 1. Data export (CSV/JSON)

Add `GET /api/export?metric=all|<name>&days=N&format=csv|json` to `vitalforge-dashboard/app.py`,
reusing `get_metrics()`'s exact query pattern against `METRIC_TABLES`. `metric=all` streams
long/tidy `metric,date,value` rows (the 16 tables don't share a date domain, so a wide per-date
join is out of scope for "small"); single-metric streams `date,value`. Response is a
`StreamingResponse` with `Content-Disposition: attachment`. No schema change, no new dependency
(csv/io stdlib), auth inherited for free from the existing `/api/*` middleware gate. Files:
`vitalforge-dashboard/app.py`, a new `tests/test_export_api.py` (seed_metric pattern), an
optional README row.

**Verified feasible — one required fix.** The plan's `get_db()`/try-finally pattern must be
scoped *inside* the async generator `StreamingResponse` consumes, not around a synchronous call
before `StreamingResponse` is constructed — otherwise the connection closes before any row
streams, or leaks. Everything else (METRIC_TABLES shape, auth inheritance, test helpers) checked
out against the real files. **Effort confirmed: small**, on the order of a few hours.

### 2. Composite readiness/recovery score (0–100)

New `vitalforge-dashboard/readiness.py` scoring three independently-normalized 0-100 components
— HRV-vs-30-day-baseline, RHR level+14-day-trend, and Garmin's native `sleep_score` — combined
40/30/30, renormalizing across whichever inputs have enough trailing data (`MIN_DAYS_BASELINE=5`)
rather than crashing or zeroing out. One `GET /api/readiness` endpoint returning
`{"score": int|None, "components": {...}, "status": "ok"|"partial_data"|"insufficient_data"}`,
plus a headline tile in `templates/index.html` above the existing 4-card grid. No schema change.

**Verified feasible — one required fix.** `readiness.py`'s sibling import
(`from recommendations import avg, trend_slope, get_all_metrics`) needs its own
`sys.path.insert(0, str(Path(__file__).resolve().parent))` (matching `app.py`'s pattern) or a
standalone `tests/test_readiness.py` importing it via `importlib.import_module` will raise
`ModuleNotFoundError` — the plan's claim that this mirrors `sync.py`'s import style was wrong
(`sync.py` never imports `recommendations.py`). Body_battery is deliberately excluded from v1
scoring (correlated with HRV/sleep, not independent signal) — flag to confirm before building.
**Effort confirmed: small–medium**, leaning small; the weighting formula itself is a reasoned
judgment call, not derived from any disclosed Whoop/Oura algorithm.

### 3. Notable-change / anomaly alerts

One new `stdev()` helper plus a generic rule block appended to `run_rules()` in
`recommendations.py`. For each of 10 tracked metrics, z-score the trailing 3-day average against
a 21-day baseline ending 3 days back (excluding the anomaly window from its own baseline);
`|z| >= 2.0` → warning, `|z| >= 3.0` → alert. Requires ≥10 baseline points or the metric is
skipped (guards early-adoption users). No new endpoint, no schema change — flows through the
existing `run_rules` → `/api/recommendations` pipeline and existing `.rec-card.severity-*` CSS.

**Verified feasible — two required fixes.** (1) New finding dicts must include a `category` key
— `_findings_to_recommendations()` hard-indexes `f["category"]` with no default, so omitting it
causes a 500 on `/api/recommendations` for any user without an LLM key configured, precisely on
otherwise-healthy days with few other findings. (2) The metric-lookup loop must use
`data.get(metric, [])` not `data[metric]` — most existing tests pass partial data dicts and would
KeyError otherwise. Also noted: for any 14-point linear-ramp series the z-score algebraically
reduces to a slope-independent constant (~2.21), so several existing trend rules will
systematically co-fire a shadow `_notable_change` finding — cosmetic redundancy, not a bug.
**Effort confirmed: small–medium**, leaning small (~2-4 hours incl. tests).

### 4. Ad-hoc cross-metric correlation view

New `vitalforge-dashboard/correlations.py`: pure-Python `pearson_r()`, plus one
`GET /api/correlations?metrics=a,b,c&days=30&lag=0&min_pairs=5` returning a row-major NxN matrix
(`cells[i][j] = {"r": float|null, "n": int}`). All `METRIC_TABLES` tables are `date TEXT PRIMARY
KEY`, so alignment is a plain dict inner-join; `lag` shifts the column series forward N calendar
days before joining (so "yesterday's steps vs. tonight's sleep" is expressible, and makes the
matrix asymmetric — all N² cells computed, not just the upper triangle). `r` is null (not NaN)
below `min_pairs` or on zero-variance input. UI: a hand-rolled CSS-grid heatmap plus a Chart.js
scatter drill-down on cell click, in a new `static/correlations.js` (index.html is already near
its 800-line file cap). `weight_log` (timestamp-keyed) is deliberately excluded from v1 — it
isn't dashboard-readable today per CLAUDE.md's Garmin-read boundary.

**Verified feasible — no gap found, one cosmetic correction.** All cited line numbers, table
shapes, and the "one connection per request not per metric" design (actually a correctness
*improvement* over `recommendations.py`'s existing per-call-connection pattern) checked out.
Minor: the plan overstated the failure mode of a raw NaN in JSON (Starlette's encoder raises
`ValueError` server-side rather than emitting invalid JSON) — the fix (clamp/null before
serializing) is unchanged either way. **Effort confirmed: medium** — the `lag` parameter forcing
an asymmetric matrix, plus the new heatmap+scatter frontend module, are what keep this above
"small."

### 5. Goal / target tracking

New `goals` table (`id, user_id REFERENCES users(id), metric, target_value, target_date,
created_at`) plus `idx_goals_user_id` — a fresh `CREATE TABLE IF NOT EXISTS`, so none of
`shared/database.py`'s `_add_columns()` ALTER-TABLE interrupted-boot hazard applies. New sibling
module `vitalforge-dashboard/goals.py` (Pydantic models, CRUD, a `compute_progress()` using
`recommendations.trend_slope` for ETA). Five routes: `POST/GET /api/goals`,
`GET/PATCH/DELETE /api/goals/{id}`, with ownership enforcement (404 absent, 403 wrong owner,
admin override) mirroring `revoke_token`'s existing pattern in `shared/auth.py`. `metric` is
validated against `METRIC_TABLES.keys()` at the API layer, not a DB CHECK, to avoid a second
place the metric enum can drift.

**Verified feasible — one required fix.** The plan's proposed `require_user_id(request) -> int`
helper can't support its own admin-override design — ownership checks need `identity.role`, and
the existing `get_current_user_role()` is explicitly documented as unsafe for authorization
(username-reuse races). Fix: widen the new public helper to return the full identity (or a
`(user_id, role)` pair), not a bare int. Also flagged: in the current auth-not-configured dev
mode, goal endpoints will 401 while every other dashboard endpoint works — goals literally
require an owning account. **Effort confirmed: medium** — the schema piece is low-risk, but a
new blast-radius-sensitive `shared/auth.py` accessor, genuinely new UI surface, and an
ownership-matrix test suite with no full existing analog keep it out of "small."

### 6. Local FIT-file import without cloud login

**Central finding that reshapes this item:** `sync.py`'s `upsert()` does
`INSERT OR REPLACE ... WHERE date = ?`, and `scheduled_sync()` re-pulls a 90-day backfill on every
boot plus every `SYNC_INTERVAL_HOURS`. Writing FIT-derived data into any existing
`METRIC_TABLES` table would be silently overwritten within hours — not a risk, a guaranteed data
loss on a timer. A brand-new `activities` table is therefore mandatory, not a style choice:
`id, start_time_utc, sport, duration_seconds, distance_m, calories, avg_hr, max_hr,
elevation_gain_m, source_format CHECK IN ('fit','tcx','gpx'), file_sha256 UNIQUE, imported_at,
raw_summary_json`. New `vitalforge-dashboard/fit_import.py` parses into a frozen `ActivityRecord`
dataclass; `POST /api/import/activity` (multipart, auto-auth-protected) does two-stage dedup
(file-hash exact match, then `(start_time_utc, sport)` natural-key/time-window match for
cross-format duplicates) and never touches `sync.py`'s upsert path. `GET /api/activities[/{id}]`
for reads. Parser must be pure-Python (no build toolchain in this repo's Dockerfiles) —
`fitparse` for FIT, stdlib `xml.etree.ElementTree` for TCX, `gpxpy` for GPX.

**Verified feasible — one required fix.** The two-stage dedup (hash pre-check, then insert) has
the same TOCTOU race this repo already hit and fixed once for `weight_log` — two concurrent
uploads of the same file can both pass the app-level hash check before either commits, and the
second insert then hits a raw, unhandled `IntegrityError` (500) instead of a graceful
`duplicated:true`. Fix: wrap the check-then-insert in `BEGIN IMMEDIATE`, mirroring
`vitalforge-weight/app.py:195`, plus a concurrent-upload test mirroring
`tests/test_dedup_concurrency.py`. Everything else — table design, parser library constraints,
UTC-vs-local-date ambiguity, upload-size caps, content-sniffing over trusting file extensions —
checked out. **Effort: recommend descoping.** The original "large" estimate (FIT+TCX+GPX unified)
is defensible as stated, but a FIT-only first slice (new table, 3 endpoints, no metric-table
writes) is **medium** — the hardest-sounding part of the original ask turned out to be actively
wrong to do. TCX/GPX become small–medium follow-on phases once the scaffolding exists.

### 7. Family / multi-person comparison dashboards — scoping options, not a plan

**This corrects the roadmap item's own framing.** There is no per-user Garmin linkage today —
one global `GARMIN_EMAIL`/`GARMIN_PASSWORD` pair and one module-level `_client` singleton serve
every login; `users` is purely an app-login table, and zero metric tables have a `user_id`/
`person_id` column (confirmed by grep across `sync.py`, `recommendations.py`,
`vitalforge-weight/app.py`). So the roadmap's suggested cheap path — "overlay accounts that
already exist" — isn't buildable as written, because there are no separate per-person datasets
to overlay yet. Three real options, verified against the actual code:

- **Option A — N single-person instances, one host, read-only overlay (small–medium, days).**
  Run the existing Docker image once per person, each with its own `DB_PATH`/`GARTH_TOKEN_DIR`
  and Garmin credentials (already env-overridable, unchanged from today's model). Add one
  `GET /api/compare` route that fans out to each sibling's existing `/api/metrics/{name}` using
  the per-user bearer tokens already shipped (PR #22), overlaid in one Chart.js chart. Zero
  changes to `shared/`, `sync.py`, or any existing table — but this repo has never stored a
  *reversible* credential before (existing tokens are one-way hashed for verifying inbound
  auth), so a new `remote_sources` table holding an outbound-presentable token is a genuinely new
  secret-handling precedent, even at low volume.
- **Option B — same model, federated across separate households (medium).** Architecturally
  identical to A; the added cost is entirely outside this codebase (VPN/reverse-proxy
  reachability between hosts — `nginx/nginx.conf` already exists and could plausibly front this),
  and the reversible-token concern matters more once it crosses the public internet.
- **Option C — true multi-tenant, `person_id` on every metric table (large, multi-week).** The
  only option enabling real cross-person queries/joins. Blocked on a genuine finding, not an
  assumption: every metric table's PK is `date TEXT PRIMARY KEY` alone, and `upsert()`'s
  `INSERT OR REPLACE` is keyed on exactly that — a merely-additive nullable `person_id` column is
  *not* sufficient, since two people syncing the same calendar date would silently overwrite each
  other's row. The PK must become `(person_id, date)`, requiring SQLite's create-copy-drop-rename
  rebuild across ~10 date-keyed tables (`weight_log` is id-keyed and could take a cheaper additive
  column) — this repo's own `shared/database.py` comments describe exactly this rewrite/
  interruption hazard as something deliberately avoided until now. Also needs: a per-person
  Garmin-client registry replacing the current singleton (a second, more sensitive new
  reversible-secret surface — actual Garmin credentials, not just read tokens), staggered sync to
  avoid multiplying the documented Garmin 429 rate-limit risk, and a real access-control/grant
  model in `shared/auth.py` (a "person" isn't necessarily a login — e.g. a parent managing a
  child's data).

**Verified: all three options are grounded** in the actual schema, auth model, and sync
behavior — no corrections needed beyond a footnote (Option C's "~11 tables" loosely includes
`weight_log`, which doesn't actually need the PK rebuild). This remains the one item needing a
human decision — which option, or none — before it gets a real implementation plan.
