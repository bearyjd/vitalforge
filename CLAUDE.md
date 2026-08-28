# CLAUDE.md — VitalForge

Personal health metrics platform: two independent FastAPI microservices sharing a Python
package (`shared/`) and one SQLite database, deployed via Docker Compose.

**PRIVACY:** This repo's Docker volume and `.garth` token cache can contain real personal
health data (weight, sleep, HRV, heart rate) and live Garmin Connect credentials. Never read,
log, print, or copy the contents of `.env`, `/app/data/fitness.db`, or `.garth/` token files
into commits, PRs, issues, or chat output. Treat any local `fitness.db` you find as sensitive
and out of scope for inspection beyond schema.

## Stack

- Python 3.12, FastAPI + uvicorn, Jinja2 server-rendered templates, vanilla JS (Chart.js on
  the dashboard), PWA manifest + service worker on each service.
- `aiosqlite` — one SQLite file (`/app/data/fitness.db` in containers) shared by both services
  via a Docker named volume (`vitalforge-data`).
- `garminconnect` (garth-based) for all Garmin Connect reads/writes. Tokens persist to
  `/app/data/.garth`.
- `anthropic` SDK — optional LLM layer on top of a rules engine for health recommendations.
  Falls back to rules-only output if no `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` is set.
- No frontend build step, no JS package manager — templates and static JS are hand-written and
  served directly by FastAPI's `StaticFiles`/`Jinja2Templates`.

## Repo layout

```
shared/                   # imported by BOTH services via sys.path.insert hack (no pyproject.toml)
  auth.py                 # cookie/HMAC session auth + login page HTML + FastAPI middleware
  database.py             # aiosqlite connection + schema; migrations.py runs one-shot schema
                          # migrations on top of it (001-person-id-rebuild, Phase 1)
  garmin_client.py        # thin wrapper over garminconnect.Garmin, module-level singleton `_client`
vitalforge-weight/        # port 8085 — weight entry PWA, writes to Garmin + weight_log table
vitalforge-dashboard/     # port 8086 — reads synced metrics, runs sync.py + recommendations.py
nginx/nginx.conf          # optional reverse proxy for subdomain routing (not used by docker-compose*.yml directly)
docker-compose.yml        # DEV — builds images from source (context: repo root, per-service Dockerfile)
docker-compose.prod.yml   # PROD — pulls prebuilt images from Docker Hub / GHCR, no build step
.github/workflows/docker.yml  # CI: builds + pushes images on push to main / tags. No test step exists.
```

## Human-judgment chokepoints (now codified)

- **Which compose file to use:** `docker-compose.yml` builds from local source — use this any
  time you've changed `shared/`, `vitalforge-weight/`, or `vitalforge-dashboard/` and need to
  verify the change. `docker-compose.prod.yml` only pulls published images and is for
  deployment — it will silently ignore local edits. Never use `docker-compose.prod.yml` to
  validate a code change.
- **`shared/` is a blast-radius module, not a library boundary.** It has no `pyproject.toml`,
  no version, no independent tests, and is imported via `sys.path.insert(0, parent_dir)` in
  both `app.py` files (see `vitalforge-weight/app.py:13`, `vitalforge-dashboard/app.py:12-13`,
  `vitalforge-dashboard/sync.py:8`). Any change to `shared/auth.py`, `shared/database.py`, or
  `shared/garmin_client.py` affects both services simultaneously — always re-check both
  `vitalforge-weight` and `vitalforge-dashboard` after touching `shared/`.
- **`VITALFORGE_SECRET` and the session cookie are shared across both services.** The same
  serializer secret and cookie name (`vf_session`) are used in both apps. This is intentional
  (single login covers both services when behind the same domain), not a bug — don't
  "fix" it by giving each service its own secret.
- **Auth is fully disabled only while the `users` table is empty**
  (`shared/auth.py`, `_is_auth_configured`). `VITALFORGE_USER`/`VITALFORGE_PASS` are one-time
  bootstrap inputs for the first admin, not the ongoing auth switch. An empty table with no
  bootstrap password still gives local development open access; once any account exists,
  auth remains enabled even if those environment variables are later removed.
- **Dashboard read endpoints do not call Garmin at request time.** `/api/metrics/{name}`,
  `/api/recommendations`, and `/api/recommendations/rules-only` only read from the local
  SQLite tables populated by `sync.py`. Garmin Connect is only contacted during
  `POST /api/sync` (dashboard) and `POST /api/weight` (weight service, via
  `shared/garmin_client.push_weight`). This means most dashboard bugs can be reproduced by
  seeding the local DB directly — no live Garmin account needed (see roadmap item 2).
- **`DB_PATH` and `GARTH_TOKEN_DIR` are env-overridable** (`shared/database.py:6`,
  `shared/garmin_client.py:10`), defaulting to `/app/data/...`. Point these at a scratch
  directory to run either service against an isolated database without touching the real
  `/app/data` volume.

## Verified run/build commands (from Dockerfile / README — do not invent alternatives)

```bash
# Dev, from repo root, builds from source:
cp .env.example .env        # fill in real values only when actually needed; never invent credentials
docker compose up --build
curl http://localhost:8085/health   # {"status": "ok", "service": "vitalforge-weight"}
curl http://localhost:8086/health   # {"status": "ok", "service": "vitalforge-dashboard"}

# Running a single service without Docker (matches Dockerfile CMD, from repo root):
pip install -r vitalforge-weight/requirements.txt
DB_PATH=/tmp/vf-test.db GARTH_TOKEN_DIR=/tmp/vf-garth \
  uvicorn vitalforge-weight.app:app --host 0.0.0.0 --port 8085

# Lint and test (repo root; mirrors .github/workflows/docker.yml's `test` job).
# Use a venv, not the system/global Python — installing these into a shared interpreter
# that other tools (e.g. an LLM proxy, MCP servers) also use WILL downgrade packages like
# starlette/jinja2 out from under them:
python3 -m venv .venv && source .venv/bin/activate
pip install -r vitalforge-weight/requirements.txt -r vitalforge-dashboard/requirements.txt
pip install pytest pytest-asyncio httpx ruff playwright pytest-playwright
pip install -e .
ruff check .
pytest -q

# UI smoke tests (Playwright) — excluded from the default `pytest -q` run, see below:
playwright install --with-deps chromium   # `--with-deps` needs apt; on non-apt systems
                                           # (e.g. Fedora) run `playwright install chromium`
                                           # and ensure browser system libs are present
pytest -q -m playwright
```

`pyproject.toml` installs `shared/` as a real package (`pip install -e .`) and lists
`pytest`, `pytest-asyncio`, `httpx`, `ruff`, `playwright`, `pytest-playwright` in a
`[dependency-groups] dev` block (PEP 735 — informational for now; CI installs them
directly rather than via a `--group` flag). `[tool.pytest.ini_options]` sets
`pythonpath = ["."]`, `testpaths = ["tests"]`, `asyncio_mode = "auto"`, and
`addopts = "-m 'not playwright'"` — **`playwright`-marked tests are excluded from the
default `pytest -q` run on purpose**: `pytest-playwright`'s session-scoped `browser`
fixture keeps its own event loop running in the main thread for the rest of the process,
which breaks `pytest-asyncio`'s fixture setup for any async test that runs afterward in
the same session (`RuntimeError: Runner.run() cannot be called from a running event
loop`). Never remove that `addopts` line or merge the two suites into one `pytest`
invocation — run `pytest -q -m playwright` as a genuinely separate process instead (see
`tests/live_server.py` and `tests/test_smoke_ui.py`).
`.github/workflows/docker.yml` has a `test` job (`ruff check .` then `pytest -q`, then a
separate Playwright-browser-install step and `pytest -q -m playwright`) that gates
`build-and-push` via `needs: test` — a failing lint or test blocks the image push.
Tests live in `tests/` and never touch real infrastructure: `tests/conftest.py` points
`shared.database.DB_PATH` at a per-test `tmp_path` and monkeypatches
`shared.garmin_client` to a fake client backed by canned fixtures in
`tests/fixtures/garmin/` — no live Garmin account or `/app/data` access required. The
Playwright smoke tests reuse the same faked DB/Garmin setup but serve the app for real
over HTTP (`tests/live_server.py::LiveServer`, real `uvicorn` in a background thread)
since a browser needs an actual socket, not `httpx.ASGITransport`. There is still no
`black`, `mypy`, or `bandit` configured. See `.agent_native/agent_roadmap.md` item 1 for
the original test-suite rationale (marked DONE).

## Conventions observed in the existing code (follow these, don't impose new house style)

- FastAPI apps use `@asynccontextmanager` lifespan for startup (`init_db()`, Garmin
  `authenticate()`), not `@app.on_event`.
- DB access pattern: open a connection with `get_db()`, use `try/finally: await db.close()`
  per-request — no connection pooling. Preserve this pattern in new endpoints.
- Errors from Garmin calls are caught and logged, never allowed to crash a request; endpoints
  return `synced_to_garmin: false` / `garmin_error` rather than raising, except where the
  Garmin sync itself is the whole point of the request.
- Type hints are used inconsistently (present in `shared/` and `sync.py`, sparser elsewhere).
  Prefer adding hints to new/changed functions per the global Python style rule, but don't do a
  drive-by rewrite of untouched code just to add hints.
- Table names in `shared/database.py` map to metric keys via `METRIC_TABLES` in
  `vitalforge-dashboard/app.py:30-44` — when adding a new synced metric, you must update
  `shared/database.py` (schema), `sync.py` (populate), and this `METRIC_TABLES` dict
  (expose via `/api/metrics/{name}`) together, or the metric silently won't be queryable.
  If the new metric table is created before a future schema rebuild ships, it must also be
  added by name to `shared/migrations.py`'s `_REBUILD_TABLES` list — that list derives its
  column shapes from the live schema rather than duplicating them, so it only ever needs the
  table's name, not its columns.
- **`_REBUILD_TABLES` only covers `(person_id, date)`-keyed metric tables.** A table that keeps
  its own `id` primary key, or carries `NOT NULL`/`DEFAULT`/`CHECK`/`UNIQUE` columns, cannot go
  in that list — `_rebuild_columns` refuses exactly those shapes rather than silently dropping
  the constraint. Such tables get a hand-written rebuild instead (`_rebuild_sync_status`,
  `_rebuild_activities`). If you add one, also add it to
  `tests/test_migrations.py`'s parity checks: the generic
  `test_schema_parity_fresh_vs_migrated` only iterates `_REBUILD_TABLES`, so a bespoke rebuild
  whose DDL drifts from `shared/database.py`'s is invisible to it.
- **Migrations are immutable once written — never add work to an existing marker.** A database
  that already committed a marker skips that migration wholesale forever, so anything appended
  to it silently never runs there while the app code assumes it did. Add a new marker instead
  (`002-activities-person-id` exists because the `activities` gap was found after 001 had
  already run on dev databases), and list it in `shared/migrations.py`'s `_KNOWN_MIGRATIONS` in
  the same commit — an applied marker missing from that tuple boot-loops the container.
- **A `CREATE INDEX` in `init_db` cannot reference a column that only a migration adds.**
  The whole DDL block runs before any migration, so `activities`'s person-scoped index is
  guarded by a `PRAGMA table_info` check and re-created inside `_rebuild_activities` for the
  upgrade path. `weight_log` avoids this only because `_add_columns` gives it `person_id`
  earlier in the same block.
