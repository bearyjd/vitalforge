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
  database.py             # aiosqlite connection + schema (CREATE TABLE IF NOT EXISTS, no migrations)
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
- **Auth is fully disabled when `VITALFORGE_PASS` is empty** (`shared/auth.py:25`,
  `_is_auth_configured`). An agent testing locally without setting `VITALFORGE_PASS` will see
  open, unauthenticated access — this is expected dev behavior, not a vulnerability to "fix"
  unless the task is specifically about auth.
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
```

There is currently **no lint, format, or test command configured anywhere in this repo**
(no `pytest`, `ruff`, `black`, `mypy`, or `pyproject.toml`; `.github/workflows/docker.yml`
only builds and pushes Docker images — it runs no tests). Do not claim "tests pass" or "lint
clean" — no such gate exists yet. See `.agent_native/agent_roadmap.md` item 1 to close this
gap. If you add tests, wire them into `.github/workflows/docker.yml` as a separate job that
gates the build/push job.

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
