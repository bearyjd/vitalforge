<!-- Generated: 2026-08-22 | Files scanned: 24 | Token estimate: ~1050 -->
# Backend

FastAPI, `@asynccontextmanager` lifespans (not `@app.on_event`). No router modules — all
routes are defined flat in each service's `app.py`. No ORM — raw `aiosqlite` queries,
per-request connection via `shared.database.get_db()` / `try/finally: await db.close()`.

## Middleware chain (both services, identical)

`shared/auth.py::add_auth_routes(app)` registers, in order:
1. `GET /auth/login`, `POST /auth/login`, `GET /auth/logout` routes
2. `@app.middleware("http") auth_middleware` — skips `/auth/*`, `/health`, `/static/*`;
   if `VITALFORGE_PASS` unset, auth is a no-op (`get_current_user` returns `"anonymous"`);
   otherwise `get_current_user` checks, in order: (a) `Authorization: Bearer
   <VITALFORGE_API_TOKEN>` header, constant-time compare, only valid if
   `VITALFORGE_API_TOKEN` is set — the Bascule/machine-client path; (b) the `vf_session`
   HMAC cookie. Either grants full API access (no scoping between them); failure is 401 on
   `/api/*`, redirect elsewhere. `VITALFORGE_API_TOKEN` set without `VITALFORGE_PASS` is a
   misconfiguration warned at import time — the token is inert (auth is fully off) in that
   case.

## vitalforge-weight (`vitalforge-weight/app.py`, :8085)

```
GET  /health                    -> {"status":"ok","service":"vitalforge-weight"}
GET  /                          -> templates/index.html (Jinja2)
POST /api/weight                -> dedup check -> INSERT or enrich weight_log
                                    -> garmin_client.push_weight() (add_body_composition)
GET  /api/weight/recent         -> SELECT weight_log ORDER BY timestamp DESC LIMIT 10
GET  /api/weight/trend          -> SELECT weight_log WHERE timestamp >= -30 days
DELETE /api/weight/{weight_id}  -> DELETE weight_log WHERE id = ?
```
`WeightIn` (Pydantic, `extra="forbid"`): `weight`, `unit` ("lbs"/"kg"), optional
`body_fat_pct` (3-75), `body_water_pct` (30-80), `muscle_pct` (10-90), `bone_mass_kg`
(0.5-10), `source` (`"pwa"`/`"bascule"`/`"bridge"`/`"tasker"`). A model validator rejects
weight outside 2-500kg after unit conversion.

`POST /api/weight` detail — atomic dedup + enrich, then Garmin push outside the
transaction:
1. `BEGIN IMMEDIATE`; look up an existing row within +-60s and +-50g (sargable
   `timestamp >=` prefilter + `idx_weight_log_timestamp`, authoritative `julianday()`
   bounds for the real window).
2. No match -> `INSERT` a new row. Match with new composition fields the existing row
   lacks -> `UPDATE` just those fields ("enriched"). Match with no new info -> no-op,
   return the existing row (`"deduplicated": true`). A field present on both sides with
   different values is logged and flagged `"conflict": true`, existing value wins.
3. `COMMIT`, then (outside the transaction, since the Garmin call is synchronous with no
   timeout) push weight + composition to Garmin only for the paths that changed the row
   (new insert or enrichment) — `muscle_pct` is converted to `muscle_mass_kg` before the
   push. Push failure is caught, never raises; recorded as `synced_to_garmin: false` +
   `garmin_error` in the response, row is not rolled back.

lifespan: `init_db()` then `garmin_client.authenticate()` (failure logged, not fatal —
retried on first `/api/weight` POST that reaches the Garmin push step).

## vitalforge-dashboard (`vitalforge-dashboard/app.py`, :8086)

```
GET  /health                          -> {"status":"ok","service":"vitalforge-dashboard"}
GET  /                                -> templates/index.html (Jinja2)
POST /api/sync?days=1..90             -> asyncio.create_task(run_sync) if not already
                                          running (asyncio.Lock); returns immediately
GET  /api/sync/status                 -> SELECT sync_status WHERE id=1
GET  /api/metrics/{metric_name}       -> METRIC_TABLES[name] lookup -> SELECT + 7d
                                          moving average; 400 if name not in METRIC_TABLES
GET  /api/recommendations?refresh=    -> recommendations.get_recommendations(force=)
GET  /api/recommendations/rules-only  -> recommendations.get_rules_only()
```
lifespan: `init_db()`, `garmin_client.authenticate()` (non-fatal), then
`asyncio.create_task(scheduled_sync())` — background task, cancelled on shutdown.
`sync.py`/`recommendations.py` are sibling modules imported by bare name via a
`sys.path.insert` for the app.py directory itself (only `shared` is a real installed
package — see `dependencies.md`).

## Service -> repository mapping

| Route logic | Reads/writes via | Table(s) |
|---|---|---|
| weight POST/GET/DELETE | `shared.database.get_db()` direct SQL | `weight_log` |
| `sync.py::sync_date` | `upsert()` helper, `INSERT OR REPLACE` | 9 metric tables (see `data.md`) |
| `sync.py::sync_weight_history` | same `upsert()` | `weight_history` |
| `/api/metrics/*` | direct SELECT, keyed by `METRIC_TABLES` dict | any of the 9 |
| `recommendations.py::get_all_metrics` | direct SELECT (`_get_metric`) | same 9, minus `weight_history` duplication |
| Garmin I/O | `shared.garmin_client` module-level `_client` singleton | n/a (external API) |

`METRIC_TABLES` (`vitalforge-dashboard/app.py:31-45`) is the single source of truth for
which metric names are queryable — adding a new metric requires updating this dict,
`shared/database.py` schema, and `sync.py` together (see root `CLAUDE.md`).

## Background jobs

- `scheduled_sync()` (`sync.py`): on startup runs a 90-day backfill, then loops
  `run_sync(days=3)` every `SYNC_INTERVAL_HOURS` (default 2h). Errors are logged, loop
  continues. `run_sync` skips dates already present in every metric table (incremental),
  except "today" which is always re-fetched.
