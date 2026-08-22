<!-- Generated: 2026-08-22 | Files scanned: 24 | Token estimate: ~620 -->
# Dependencies

## External services

| Service | Used by | Purpose | Failure mode |
|---|---|---|---|
| Garmin Connect (via `garminconnect`/`garth`) | `shared/garmin_client.py` | push weight entries, pull sleep/HRV/RHR/stress/body-battery/VO2/training-load/steps/calories/weight-history | caught + logged per-call; endpoints return `garmin_error`/skip rather than raising, except sync itself |
| Anthropic API (`anthropic` SDK) | `vitalforge-dashboard/recommendations.py::get_llm_recommendations` | turns rules-engine findings into natural-language coaching text | optional — falls back to rules-only if `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` unset, package missing, JSON parse fails, or the call errors |
| Docker Hub + GHCR | `.github/workflows/docker.yml` | image publishing on push to `main`/tags | n/a (CI only) |
| jsDelivr CDN (`chart.js`) | both `templates/index.html` | trend/metric charts, loaded client-side | no local fallback bundled |

## Shared internal library

`shared/` — installed as a real package via root `pyproject.toml`
(`[tool.setuptools] packages = ["shared"]`), imported as `from shared.X import Y` in both
services and in `tests/`. Not independently versioned or tested; a blast-radius module,
not a library boundary (see root `CLAUDE.md`).

- `shared/auth.py` — cookie/HMAC session auth (`itsdangerous.URLSafeTimedSerializer`),
  login page HTML, `add_auth_routes(app)` middleware installer.
- `shared/database.py` — `aiosqlite` connection + schema (`get_db()`, `init_db()`).
- `shared/garmin_client.py` — thin wrapper over `garminconnect.Garmin`, module-level
  `_client` singleton, token persistence to `GARTH_TOKEN_DIR`.

`vitalforge-dashboard/sync.py` and `recommendations.py` are sibling modules of `app.py`,
not part of `shared` — imported by bare name via a `sys.path.insert` for that one
directory (see `backend.md`).

## Key third-party packages (per-service `requirements.txt`)

- `fastapi`, `uvicorn` — web framework/server (both services)
- `aiosqlite` — async SQLite driver
- `garminconnect` — Garmin Connect client (garth-based OAuth); pinned `==0.3.11` (was
  `>=0.2.38`) for `add_body_composition` support
- `itsdangerous` — signed session cookies
- `jinja2` — server-rendered templates
- `anthropic` — optional LLM layer (dashboard only)

## Dev/test/CI tooling (`pyproject.toml`, `[dependency-groups] dev`)

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) — test runner
- `httpx` — `AsyncClient` for exercising FastAPI apps in tests
- `ruff` (`line-length = 120`, rules `E,F,W,I`, `E501` ignored) — lint, run via
  `ruff check .`
- `playwright` + `pytest-playwright` — browser-driven UI smoke tests
  (`tests/test_smoke_ui.py`), marked `@pytest.mark.playwright` and excluded from the
  default `pytest -q` run (`addopts` in `pyproject.toml`) — must run as a separate
  `pytest -q -m playwright` invocation, never merged into the same process as the async
  API tests (session-scoped browser fixture breaks pytest-asyncio otherwise)

All wired into `.github/workflows/docker.yml`'s `test` job, which gates
`build-and-push`. No `black`, `mypy`, or `bandit` configured anywhere.
