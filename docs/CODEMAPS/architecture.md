<!-- Generated: 2026-08-22 | Files scanned: 24 | Token estimate: ~800 -->
# Architecture

Two independent FastAPI microservices sharing one Python package (`shared/`) and one
SQLite database file, deployed together via Docker Compose. No message queue, no
separate backend framework — each service is a single-process `uvicorn` app.

## Services

```
vitalforge-weight (:8085)          vitalforge-dashboard (:8086)
  PWA: log a weight entry            PWA: view synced metrics + recommendations
  writes -> Garmin + weight_log      reads <- 9 metric tables (populated by sync.py)
       |                                   |
       +------------------+----------------+
                           |
                      shared/ package
                (auth.py, database.py, garmin_client.py)
                           |
                 SQLite: /app/data/fitness.db
                 (Docker named volume: vitalforge-data)
                           |
                 Garmin Connect (via garth token cache
                 at /app/data/.garth, garminconnect lib)
```

## Data flow

- **Weight entry**: client -> `POST /api/weight` (weight svc) -> atomic dedup check
  (+-60s / +-50g window against `weight_log`; a match enriches the existing row instead of
  inserting) -> commit -> push weight + body composition (`body_fat_pct`/`body_water_pct`/
  `muscle_pct`/`bone_mass_kg`) to Garmin via `garmin_client.push_weight`
  (`add_body_composition`). Garmin push failure is caught and reported as `garmin_error`;
  the local DB write always happens first and is never rolled back for a Garmin failure.
- **Dashboard sync**: `POST /api/sync` (dashboard svc) or the background
  `scheduled_sync()` loop -> pulls sleep/HRV/RHR/stress/body-battery/VO2/training-load/
  steps/calories/weight-history from Garmin -> upserts into 9 per-metric tables.
- **Dashboard reads**: `/api/metrics/{name}`, `/api/recommendations*` never call Garmin
  directly — they only read what `sync.py` already wrote to SQLite. This means dashboard
  bugs are reproducible by seeding the DB (`scripts/seed_db.py`), no live Garmin account
  needed.
- **Recommendations**: rules engine (`recommendations.py::run_rules`) always runs first;
  results are optionally handed to the Anthropic API for natural-language coaching text,
  with a rules-only fallback if no API key/base URL is configured. 6h in-process cache.

## Service boundaries

- `shared/` is **not** a versioned library — no independent tests, imported into both
  services via `pyproject.toml` package install (see `dependencies.md`). Any change to
  `shared/auth.py`, `database.py`, or `garmin_client.py` affects both services at once.
- The two `vitalforge-*` directories are self-contained apps (own `Dockerfile`,
  `requirements.txt`, `templates/`, `static/`); they do not import each other. Coupling is
  entirely through the shared SQLite file and the shared session-cookie secret.
- Auth (`shared/auth.py`) is a FastAPI HTTP middleware applied identically in both apps'
  `add_auth_routes(app)` call — same cookie name (`vf_session`), same HMAC secret
  (`VITALFORGE_SECRET`), so one login covers both services behind the same domain. `/api/*`
  additionally accepts a `Authorization: Bearer <VITALFORGE_API_TOKEN>` header (checked
  before the cookie) — the machine-client path for the parallel Bascule Android app, which
  only ever POSTs `/api/weight` and doesn't hold a browser session.

## Deployment

- `docker-compose.yml` — dev, builds both images from source (repo-root build context).
- `docker-compose.prod.yml` — prod, pulls prebuilt images from Docker Hub / GHCR, no build.
- `nginx/nginx.conf` — optional reverse proxy mapping two subdomains to :8085/:8086; not
  referenced by either compose file, applied manually if used.
- `.github/workflows/docker.yml` — on push to `main`/tags: `test` job (ruff + pytest) gates
  a `build-and-push` job (matrix over both services, pushes to GHCR + Docker Hub).
