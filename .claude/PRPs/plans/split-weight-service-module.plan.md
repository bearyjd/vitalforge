# Plan: Split vitalforge-weight/app.py into domain modules

## Summary
`vitalforge-weight/app.py` is 2,056 lines (1,259 excluding comments) against this repo's
own 800-line ceiling, with two route bodies at 199 and 208 code lines against a 50-line
guidance. The file cannot currently be split, because the service directory's hyphenated
name makes `from vitalforge-weight.x import y` a syntax error. This plan renames both
service directories to valid Python identifiers (Phase A), then splits the weight service
into domain modules (Phase B).

## User Story
As the maintainer of VitalForge,
I want the weight service split into modules that fit in my head,
So that the next feature lands in a 400-line domain file instead of a 2,000-line module
where the two largest routes are 200 lines each.

## Problem → Solution
One 2,056-line module holding two unrelated domains (weight logging, strength sessions)
plus the app shell → three domain modules of ~250-900 lines each, each independently
readable, behind an unchanged HTTP surface.

## Metadata
- **Complexity**: Large (Phase A touches ~20 files mechanically; Phase B restructures 1 file into 4)
- **Source PRD**: N/A (free-form request)
- **PRD Phase**: N/A
- **Estimated Files**: Phase A ~20, Phase B ~6

---

## UX Design

Internal change — no user-facing UX transformation. Every HTTP route, request model,
response shape, status code and header is unchanged. The `/health` payloads
(`{"status":"ok","service":"vitalforge-weight"}`) are unchanged, which matters because
deployment verification greps them.

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| All HTTP routes | unchanged | unchanged | Pure refactor; route table must be byte-identical |
| `/health` service string | `vitalforge-weight` | `vitalforge-weight` | MUST NOT change — deploy checks depend on it |
| Docker image name | `bearyj/vitalforge-weight` | unchanged | MUST NOT change — prod pulls this tag |
| Compose service name | `vitalforge-weight` | unchanged | MUST NOT change — nginx upstreams + container names |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `vitalforge-weight/app.py` | all 2056 | The subject. Read fully before moving anything. |
| P0 | `vitalforge-weight/Dockerfile` | 1-24 | `pip install -e .` at L12 runs BEFORE the service COPY at L14 |
| P0 | `pyproject.toml` | 11-20 | `packages = ["shared"]` + the comment documenting the hyphen decision being reversed |
| P0 | `tests/conftest.py` | 1-30, 215-230, 325-450 | `import_service_module` and every fixture that names a service module |
| P1 | `.github/workflows/docker.yml` | 20-95 | Requirements paths AND the build matrix (`name:` is the image, not the dir) |
| P1 | `docker-compose.yml` / `docker-compose.prod.yml` | all | `dockerfile:` is a path; the service key is not |
| P1 | `tests/test_landing_parity.py` | 28, 87 | Both a file-path list and a dotted-module parametrize |
| P1 | `tests/test_no_unscoped_person_access.py` | 22, 40 | File-path list used for source scanning |
| P2 | `CLAUDE.md` | Repo layout + chokepoints | Documents the layout being changed |

## External Documentation

| Topic | Source | Key Takeaway |
|---|---|---|
| PEP 420 namespace packages | python.org | A directory without `__init__.py` is importable as a namespace package when its parent is on `sys.path`. This is how `vitalforge-weight.app` resolves today and how `vitalforge_weight.app` will resolve after. No `__init__.py` needed. |

No other external research needed — this uses established internal patterns only.

---

## Discovery: how the service module resolves today

Verified in the running production container:

```
sys.path contains ''   # CWD, which is WORKDIR /app
importlib.import_module("vitalforge-weight.app").__file__ == /app/vitalforge-weight/app.py
```

`shared` is importable because `pip install -e .` installs it (`packages = ["shared"]`).
The **service** module resolves purely through CWD. Tests get the same effect from
`[tool.pytest.ini_options] pythonpath = ["."]`.

**Consequence:** after the rename, `from vitalforge_weight.weight import ...` inside
`vitalforge_weight/app.py` resolves through that same CWD entry. Nothing new is needed
on `sys.path`.

---

## Patterns to Mirror

### MODULE_IMPORT_OF_SHARED
// SOURCE: vitalforge-weight/app.py:27-50
```python
from shared.auth import (
    ...
)
from shared.database import (
    ...
)
from shared.garmin_client import (
    ...
)
from shared.persons_admin import add_person_routes
```
Plain absolute imports. No `sys.path` manipulation (CLAUDE.md still describes a
`sys.path.insert` hack — that is stale and should be corrected in Phase A).

### ROUTE_REGISTRATION_ON_AN_APP_OBJECT
// SOURCE: shared/persons_admin.py:218
```python
def add_person_routes(app):
    """Register the person-collection admin surface on a FastAPI app."""
```
The repo ALREADY has the pattern for registering routes defined outside `app.py`:
a module exposes `add_*_routes(app)` and `app.py` calls it. `shared/auth.py`'s
`add_auth_routes(app)` is the same shape. **Phase B must mirror this, not invent a
router-object pattern.**

### DB_ACCESS_PER_REQUEST
// SOURCE: vitalforge-weight/app.py post_weight
```python
db = await get_db()
try:
    await db.execute("BEGIN IMMEDIATE")
    ...
    await db.commit()
finally:
    await db.close()
```
Open per request, `try/finally: await db.close()`, no pooling. Preserve exactly.

### ERROR_HANDLING
// SOURCE: vitalforge-weight/app.py (Garmin paths)
```python
garmin_error = None
try:
    ...
except Exception as e:
    logger.error("Post-commit sync-flag update failed for row %s: %s", row_id, e)
    if garmin_error is None:
        garmin_error = f"sync status update failed: {e}"
```
Garmin failures are caught and reported in the response body
(`synced_to_garmin: false` / `garmin_error`), never raised.

### LOGGING_PATTERN
// SOURCE: vitalforge-weight/app.py:52-53
```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
```
`%`-style lazy args, never f-strings, in log calls.

### TEST_SERVICE_MODULE_IMPORT
// SOURCE: tests/conftest.py:218-223
```python
def import_service_module(dotted_path: str):
    """Import a module from a hyphenated service directory, e.g.
    `import_service_module("vitalforge-weight.app")`.
    """
    return importlib.import_module(dotted_path)
```
After Phase A the hyphen rationale disappears. Keep the helper (fixtures call it in five
places) but update its docstring; do NOT inline `importlib` at every call site.

### TEST_STRUCTURE
// SOURCE: tests/test_activity_garmin_guard.py:35-45
```python
pytestmark = pytest.mark.usefixtures("no_real_garmin_client")

@pytest.fixture
async def client(weight_app_module):
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

---

## Files to Change

### Phase A — rename (behavior-neutral)

| File | Action | Justification |
|---|---|---|
| `vitalforge-weight/` → `vitalforge_weight/` | RENAME (`git mv`) | Makes the dir a valid Python identifier |
| `vitalforge-dashboard/` → `vitalforge_dashboard/` | RENAME (`git mv`) | Same constraint; leaving one hyphenated is inconsistent and blocks the dashboard's own 945-line file later |
| `vitalforge_weight/Dockerfile` | UPDATE | `COPY` paths (L7, L14) and the `uvicorn` target in the entrypoint (L19) |
| `vitalforge_dashboard/Dockerfile` | UPDATE | Same |
| `docker-compose.yml` | UPDATE | `dockerfile:` paths only (L5, L17) |
| `docker-compose.prod.yml` | UPDATE | Any `dockerfile:`/path references |
| `.github/workflows/docker.yml` | UPDATE | Requirements paths (L25, L68-69) and matrix `dockerfile:` (L91, L93). **Leave `name:` alone.** |
| `pyproject.toml` | UPDATE | The L12-15 comment describing hyphenated dirs is now wrong |
| `tests/conftest.py` | UPDATE | 5 `import_service_module(...)` strings + docstrings at L7, L9, L221, L330, L390, L401, L403, L410, L439 |
| `tests/test_landing_parity.py` | UPDATE | L28 `SERVICES` file paths, L87 dotted-module parametrize |
| `tests/test_no_unscoped_person_access.py` | UPDATE | L22 `SERVICES`, L40 `sync.py` path |
| `tests/test_page_rendering.py`, `test_dedup_boundary_precision.py`, `test_goals.py`, `test_weight_api.py`, `test_correlations_api.py` | UPDATE | Comment/docstring path references only |
| `shared/database.py`, `shared/garmin_client.py`, `shared/auth.py` | UPDATE | Comment path references only |
| `vitalforge_dashboard/sync.py`, `vitalforge_dashboard/app.py` | UPDATE | Comment path references only |
| `CLAUDE.md`, `README.md`, `docs/CODEMAPS/*.md` | UPDATE | Describe current state; must stay accurate |

### Phase B — split (weight service only)

| File | Action | Justification |
|---|---|---|
| `vitalforge_weight/weight_routes.py` | CREATE | `WeightIn`, `post_weight`, dedup constants, `_push_composition`, weight read routes (~900 lines) |
| `vitalforge_weight/activity_routes.py` | CREATE | `ActivityIn`, `ActivityExerciseIn`, `ActivityPushOutcome`, `post_activity`, `_push_activity`, `_reconcile_activity`, `_activity_name`, `_normalise_since`, `_record_activity_garmin_outcome`, activity read routes (~900 lines) |
| `vitalforge_weight/garmin_claim.py` | CREATE | `_GARMIN_CLAIM_TIMEOUT_SECONDS` + `_garmin_claim_is_live`, imported by both route modules (they already share one definition after PR #46) |
| `vitalforge_weight/app.py` | UPDATE | Retains lifespan, `index`, `_reachable_persons`, templates, static mount, `add_*_routes(app)` calls (~250 lines) |

## NOT Building

- **No behavior change of any kind.** No route renames, no response-shape edits, no new
  validation, no changed status codes.
- **Not splitting `vitalforge_dashboard/app.py`** (945 lines). Phase A unlocks it; the
  split is a separate plan.
- **Not touching `shared/`** beyond comment path fixes. It is a documented blast-radius
  module imported by both services.
- **Not adding `__init__.py`.** Namespace-package resolution already works and is what
  production uses; adding one changes packaging behavior for no gain.
- **Not adding the service to `pyproject.toml` `packages`.** See GOTCHA in Task A3.
- **Not rewriting historical records** — `docs/prp/*`, `docs/superpowers/*`,
  `.claude/PRPs/plans/completed/*`, `.reports/*`, `.agent_native/*`,
  `vitalforge-claude-code-prompts.md` describe past work and should keep their original
  paths.
- **Not renaming compose services, Docker image names, nginx upstreams, `/health`
  service strings, or the `sw.js` `CACHE_NAME`.** All are independent of the directory.

---

## Step-by-Step Tasks

### Task A1: Rename both service directories
- **ACTION**: `git mv vitalforge-weight vitalforge_weight` and
  `git mv vitalforge-dashboard vitalforge_dashboard`
- **IMPLEMENT**: Rename only. No file content edits in this task.
- **MIRROR**: N/A
- **GOTCHA**: Use `git mv` so history follows. Delete stale `__pycache__` directories
  afterwards (`find . -name __pycache__ -prune -exec rm -rf {} +`) — a stale
  `vitalforge-weight/__pycache__` can make an import appear to still work locally.
- **VALIDATE**: `git status` shows renames, not delete+add.

### Task A2: Update both Dockerfiles
- **ACTION**: Fix `COPY` paths and the uvicorn target.
- **IMPLEMENT**: In `vitalforge_weight/Dockerfile`: L7
  `COPY vitalforge_weight/requirements.txt .`, L14
  `COPY vitalforge_weight/ /app/vitalforge_weight/`, L19 entrypoint
  `uvicorn vitalforge_weight.app:app`. Mirror for dashboard (port 8086).
- **MIRROR**: Existing Dockerfile structure; change paths only.
- **GOTCHA**: The entrypoint is written by a `printf` inside a `RUN`. Edit the string
  carefully — the `\n` escapes and the inner double quotes must survive.
- **VALIDATE**: `docker compose build` succeeds; `grep -c "vitalforge-" Dockerfile` is 0.

### Task A3: Leave pyproject packages alone; fix its comment
- **ACTION**: Update the stale comment at `pyproject.toml:12-15`.
- **IMPLEMENT**: Keep `packages = ["shared"]`. Rewrite the comment to say the service
  dirs are importable-but-not-installed, resolved via CWD/`pythonpath`.
- **GOTCHA**: **Do NOT add the service dirs to `packages`.** The Dockerfile runs
  `pip install -e .` at L12, BEFORE the service directory is copied at L14. Adding them
  makes the image build fail on a missing directory.
- **VALIDATE**: `pip install -e .` still succeeds; `docker compose build` succeeds.

### Task A4: Update compose files
- **ACTION**: Change `dockerfile:` paths in both compose files.
- **IMPLEMENT**: `dockerfile: vitalforge_weight/Dockerfile` (and dashboard).
- **GOTCHA**: **Do not touch the service keys** (`vitalforge-weight:`) — nginx upstreams
  proxy to those names and prod containers are named from them
  (`vitalforge-vitalforge-weight-1`). Renaming them orphans the running containers.
- **VALIDATE**: `docker compose config` parses; service names unchanged in output.

### Task A5: Update CI workflow
- **ACTION**: Fix requirements and dockerfile paths.
- **IMPLEMENT**: L25 and L68-69 requirements paths; matrix `dockerfile:` at L91/L93.
- **GOTCHA**: **Leave `name: vitalforge-weight` / `name: vitalforge-dashboard`
  untouched** — those become the pushed image tags (`bearyj/vitalforge-weight`), which
  production pulls. Changing them silently publishes to a new repo and prod keeps pulling
  the old one.
- **VALIDATE**: `python3 -c "import yaml;yaml.safe_load(open('.github/workflows/docker.yml'))"`.

### Task A6: Update tests
- **ACTION**: Fix module strings and file-path lists.
- **IMPLEMENT**: `tests/conftest.py` — five `import_service_module("vitalforge_weight.app")` /
  `("vitalforge_dashboard.app")` calls plus docstrings; `tests/test_landing_parity.py`
  L28 + L87; `tests/test_no_unscoped_person_access.py` L22 + L40.
- **MIRROR**: TEST_SERVICE_MODULE_IMPORT above.
- **GOTCHA**: `test_landing_parity.py` and `test_no_unscoped_person_access.py` hold
  **filesystem paths** that are opened and scanned as source text. A missed entry makes
  the test silently scan nothing rather than fail loudly — assert the files exist.
- **VALIDATE**: `pytest -q` — 870 passed.

### Task A7: Fix comments and docs
- **ACTION**: Update path references in `shared/*.py`, remaining test docstrings,
  `CLAUDE.md`, `README.md`, `docs/CODEMAPS/*`.
- **GOTCHA**: While in `CLAUDE.md`, correct the stale claim that `shared/` is imported
  "via `sys.path.insert(0, parent_dir)` in both `app.py` files" — verified false; both use
  plain `from shared.x import y` against the editable install.
- **VALIDATE**: `grep -rn "vitalforge-weight/\|vitalforge-dashboard/" --include="*.py" .`
  returns only historical-doc hits.

### Task A8: Verify Phase A end to end
- **ACTION**: Full gates + a real container boot.
- **VALIDATE**: See Validation Commands. **Phase A ships as its own PR** and should be
  deployed and confirmed healthy before Phase B starts.

### Task B1: Extract the shared claim helper
- **ACTION**: Create `vitalforge_weight/garmin_claim.py`.
- **IMPLEMENT**: Move `_GARMIN_CLAIM_TIMEOUT_SECONDS` and `_garmin_claim_is_live`
  verbatim, keeping the full comment block (it documents the synchronous-push
  precondition).
- **IMPORTS**: `from datetime import datetime, timedelta`, `import logging`.
- **GOTCHA**: PR #46 already folded two duplicate definitions into one. Do not
  reintroduce a second copy.
- **VALIDATE**: `pytest -q tests/test_dedup_concurrency.py tests/test_activity_concurrency.py`.

### Task B2: Extract the activity domain
- **ACTION**: Create `vitalforge_weight/activity_routes.py` exposing `add_activity_routes(app)`.
- **IMPLEMENT**: Move `ActivityExerciseIn`, `ActivityIn`, `ActivityPushOutcome`,
  `post_activity`, `get_activity`, `list_strength_sessions`, `_push_activity`,
  `_reconcile_activity`, `_record_activity_garmin_outcome`, `_activity_name`,
  `_normalise_since`, `_exercise_sets_enabled`, `_push_outcome_is_ambiguous`,
  `_person_display_name`, and the activity constants (`_SESSION_MARKER_CHARS`,
  `_ACTIVITY_CONFLICT_FIELDS`, `_STRENGTH_SESSION_COLUMNS`, `GARMIN_EXERCISE_CATEGORIES`,
  `_AMBIGUOUS_TRANSPORT_ERRORS`).
- **MIRROR**: ROUTE_REGISTRATION_ON_AN_APP_OBJECT (`add_person_routes` in
  `shared/persons_admin.py:218`).
- **GOTCHA (read this twice)**: `tests/conftest.py`'s `weight_app_module` fixture does
  `monkeypatch.setattr(module, "push_activity", fake_push_activity)` against **app.py's
  own namespace** (conftest.py:328-366), and `no_real_garmin_client` then asserts the
  bound name is not the real function (conftest.py:386-392).

  Moving `push_activity` / `push_activity_sets` / `find_activities_by_date` out of
  `app.py` makes that `setattr` raise `AttributeError` — `raising=True` is the default.
  **That is the safe outcome: it fails loudly.** The danger is entirely in how it gets
  made green again. Two wrong fixes:
    - adding `raising=False` — the patch silently no-ops and every activity test hits
      real Garmin with the deployment's live credential;
    - re-importing the names into `app.py` purely to satisfy the fixture — the patch
      then lands on a binding no route uses, while `activity_routes.py` calls its own.
  The right fix is to patch the module that actually owns the binding, and to extend
  `no_real_garmin_client` to assert against that module too.
- **VALIDATE**: `pytest -q tests/test_activity_*.py` — all pass; then mutation-check one
  guard (revert `effective_display_name = stored["garmin_name_prefix"]`) and confirm
  `test_rename_between_push_and_retry_does_not_duplicate` goes red.

### Task B3: Extract the weight domain
- **ACTION**: Create `vitalforge_weight/weight_routes.py` exposing `add_weight_routes(app)`.
- **IMPLEMENT**: Move `WeightIn`, `post_weight`, `get_recent_weights`, `get_weight_trend`,
  `delete_weight`, `_push_composition`, and the weight constants (`LBS_PER_KG`,
  `GRAMS_PER_KG`, `DEDUP_*`, `ENRICHABLE_FIELDS`, `COMPOSITION_FIELDS`,
  `_WEIGHT_LOG_EXISTING_ROW_COLUMNS`, `CAPTURED_AT_FUTURE_TOLERANCE_SECONDS`).
- **MIRROR**: Same `add_*_routes(app)` shape as B2.
- **GOTCHA**: Same namespace-patching trap as B2, for `push_weight` and `authenticate`.
- **VALIDATE**: `pytest -q tests/test_weight_api.py tests/test_dedup*.py tests/test_client_id_idempotency.py`.

### Task B4: Reduce app.py to the shell
- **ACTION**: Leave lifespan, `index`, `_reachable_persons`, `_NO_PERSONS_PAGE`,
  templates, static mount, exception handler, and the `add_*_routes(app)` calls.
- **GOTCHA**: Route order was checked and is **not** currently a hazard — verified by
  dumping the live route table. The literal weight paths are GET
  (`/api/weight/recent`, `/api/weight/trend`) while the parameterised one is DELETE
  (`/api/weight/{weight_id}`), so no same-method literal-vs-param pair exists to shadow.
  Register weight before activity anyway to match today's definition order, and let the
  route-table diff (Task B5) be the actual gate rather than trusting this analysis.
- **VALIDATE**: Compare the route table before and after — see Validation Commands.

### Task B5: Confirm the HTTP surface is byte-identical
- **ACTION**: Diff the OpenAPI schema captured before and after.
- **VALIDATE**: See Validation Commands. Any diff is a defect, not an improvement.

---

## Testing Strategy

No new behavior, so no new behavioral tests. The existing 870-test suite is the lock.
One structural test is worth adding:

| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `test_no_module_exceeds_the_line_ceiling` | each `.py` under `vitalforge_weight/` | every file ≤ 800 lines | No |
| Route-table parity | app before/after | identical `(path, methods)` set | Yes |
| OpenAPI parity | `/openapi.json` before/after | identical JSON | Yes |

### Edge Cases Checklist
- [ ] Stale `__pycache__` from the old directory name removed
- [ ] Playwright lane (`pytest -q -m playwright`) passes — it boots real uvicorn via `tests/live_server.py`
- [ ] `docker compose build` succeeds from a clean context (no cached layers hiding a bad COPY)
- [ ] Container boots and `/health` returns the unchanged service string
- [ ] Route shadowing: `/api/weight/recent` still resolves before `/api/weight/{weight_id}`
- [ ] `no_real_garmin_client` still fails loudly if a module's Garmin helper is unpatched

---

## Validation Commands

### Capture the baseline BEFORE any change
```bash
python3 - <<'PY' > /tmp/routes-before.txt
import importlib
m = importlib.import_module("vitalforge-weight.app")
for r in sorted(m.app.routes, key=lambda r: (getattr(r,'path',''), str(getattr(r,'methods','')))):
    print(getattr(r,'path',''), sorted(getattr(r,'methods',[]) or []))
PY
```
EXPECT: a stable route list to diff against later.

### Static analysis
```bash
ruff check .
```
EXPECT: All checks passed.

### Full test suite
```bash
pytest -q
```
EXPECT: 870 passed, 4 deselected. Any drop in the passed count is a regression.

### Playwright lane (separate process — never merge with the above)
```bash
pytest -q -m playwright
```
EXPECT: pass. Required because it boots real uvicorn and would catch a broken module path.

### Route-table parity
```bash
python3 - <<'PY' > /tmp/routes-after.txt
import importlib
m = importlib.import_module("vitalforge_weight.app")
for r in sorted(m.app.routes, key=lambda r: (getattr(r,'path',''), str(getattr(r,'methods','')))):
    print(getattr(r,'path',''), sorted(getattr(r,'methods',[]) or []))
PY
diff /tmp/routes-before.txt /tmp/routes-after.txt && echo "ROUTE TABLE IDENTICAL"
```
EXPECT: no diff.

### Container build + boot
```bash
docker compose build
docker compose up -d
curl -s http://localhost:8085/health
curl -s http://localhost:8086/health
```
EXPECT: `{"status":"ok","service":"vitalforge-weight"}` and the dashboard equivalent,
with the service strings **unchanged**.

### Line-ceiling check
```bash
wc -l vitalforge_weight/*.py | sort -rn | head
```
EXPECT: every module ≤ 800 lines.

### Manual validation
- [ ] `git status` shows renames, not delete+add
- [ ] `grep -rn "vitalforge-weight/\|vitalforge-dashboard/" --include="*.py" --include="*.yml" --include="Dockerfile*" .` returns nothing functional
- [ ] CI green on the Phase A PR before starting Phase B
- [ ] Deployed and `/health` confirmed on 192.168.1.21 after Phase A

---

## Acceptance Criteria
- [ ] Phase A: both dirs renamed, all functional path references updated, 870 tests pass, CI green, deployed and healthy
- [ ] Phase B: `vitalforge_weight/app.py` ≤ 800 lines, no sibling module over 800
- [ ] Route table and OpenAPI schema byte-identical before and after
- [ ] `/health` service strings, image names, compose service names, nginx upstreams, `sw.js` CACHE_NAME all unchanged
- [ ] No behavior change of any kind

## Completion Checklist
- [ ] Code follows discovered patterns (`add_*_routes(app)`, per-request DB, `%`-style logging)
- [ ] Error handling unchanged (Garmin failures reported, never raised)
- [ ] Tests follow existing structure; `no_real_garmin_client` updated for moved modules
- [ ] No hardcoded values introduced
- [ ] `CLAUDE.md` updated, including the stale `sys.path.insert` claim
- [ ] No scope additions beyond the rename + split

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Garmin fixture is made green the wrong way after the move (`raising=False`, or re-importing names into `app.py` for show), silently sending activity tests at the live account | Medium | **Critical** | The move itself fails LOUD (`monkeypatch.setattr` defaults to `raising=True`) — verified. Risk is the fix, not the break. Patch the module that owns the binding and extend `no_real_garmin_client` to assert against it; never add `raising=False` |
| Renaming the compose service or CI image `name:` orphans prod / publishes to a new tag | Medium | Critical | Explicit NOT-changing list; `docker compose config` diff; verify pushed tag before deploying |
| Route shadowing after re-registration | **Low** (checked) | High | Verified against the live route table: the literal weight paths are GET, the parameterised one is DELETE, so no same-method collision exists today. Route-table diff remains a hard gate in case a future route changes that. |
| Adding service dirs to `pyproject` packages breaks the image build | Medium | High | Documented in Task A3; `pip install -e .` runs before the service COPY |
| Stale `__pycache__` masks a broken import locally | Medium | Medium | Purge caches right after `git mv` |
| Large mechanical diff hides a real edit during review | Medium | Medium | Phase A and Phase B ship as separate PRs; Phase A contains no logic change |
| 2,000-line refactor destabilises code verified end-to-end today | Low | High | Zero behavior change; route/OpenAPI parity gates; deploy Phase A alone first |

## Notes
- Phase A is a **rename-only** PR. Reviewers should be able to confirm no logic changed by
  checking that every hunk is a path string.
- Phase A reverses a decision `pyproject.toml` documents deliberately ("hyphenated names,
  not meant to be pip-installed"). The comment must be rewritten, not just left stale, or
  the next reader will think the rename was an accident.
- `post_weight` (199 code lines) and `post_activity` (208) stay long even after the split.
  They are linear, not branchy — neither trips `ruff C901` — and their top-to-bottom order
  IS the correctness argument (claim inside the transaction, push outside it, record
  after). Splitting their bodies is explicitly **not** part of this plan.
- Production is `192.168.1.21` (host `knowledge`), compose dir `/home/user/docker/vitalforge`,
  deploy is `docker compose pull && docker compose up -d`. Merging does not deploy.
