# Implementation Report: Split vitalforge-weight/app.py — Phase A (rename)

## Summary
Renamed both service directories to valid Python identifiers
(`vitalforge-weight/` → `vitalforge_weight/`, `vitalforge-dashboard/` →
`vitalforge_dashboard/`) and updated every functional path reference. Rename only: no
logic changed, and both services' route tables are byte-identical before and after.

Phase B (splitting `app.py` into domain modules) is **not** in this run. The plan
requires Phase A to ship and deploy first.

## Assessment vs Reality

| Metric | Predicted (Plan) | Actual |
|---|---|---|
| Complexity | Large (~20 files, Phase A) | Large — 46 files changed (21 renames, 24 modified, 1 added) |
| Confidence | 8/10 | Accurate. One premise error found (below), zero rework needed |
| Files Changed | ~20 | 46 (the plan undercounted docs + CODEMAPS) |

## Tasks Completed

| # | Task | Status | Notes |
|---|---|---|---|
| A1 | Rename both directories | Complete | 21 files tracked as renames by git; `__pycache__` purged |
| A2 | Update both Dockerfiles | Complete | COPY paths + uvicorn target in the printf'd entrypoint |
| A3 | pyproject comment; packages untouched | Complete | `packages = ["shared"]` deliberately unchanged |
| A4 | Compose dockerfile paths | Complete | `docker-compose.prod.yml` had none (pulls images) |
| A5 | CI requirements + matrix paths | Complete | Image `name:` values left alone — verified in CI job names |
| A6 | Tests | Complete | **Deviated** — see below |
| A7 | Comments and docs | Complete | Plus two false claims in CLAUDE.md corrected |
| A8 | End-to-end verification | Complete | All five validation levels |

## Validation Results

| Level | Status | Notes |
|---|---|---|
| Static Analysis | Pass | `ruff check .` clean |
| Unit Tests | Pass | 870 passed, 4 deselected — same count as before the rename |
| Build | Pass | `podman build -f vitalforge_weight/Dockerfile .` succeeds |
| Integration | Pass | Container boots; module loads from `/app/vitalforge_weight/app.py`; `POST /api/weight` → 200; `/health` service string unchanged |
| Edge Cases | Pass | Route parity (37 weight / 46 dashboard, byte-identical); Playwright lane 4 passed; stale `__pycache__` purged |

## Files Changed

| File group | Action | Count |
|---|---|---|
| `vitalforge_weight/*`, `vitalforge_dashboard/*` | RENAMED | 21 |
| Dockerfiles, compose, CI, pyproject | UPDATED | 5 |
| `tests/*.py` | UPDATED | 12 |
| `shared/*.py` | UPDATED | 3 |
| `CLAUDE.md`, `README.md`, `docs/CODEMAPS/*` | UPDATED | 6 |
| Plan document | ADDED | 1 |

Net: 46 files, +610 / -98.

## Deviations from Plan

1. **The plan's core premise was wrong, and this is the important finding.** The plan
   asserted the hyphen *blocked* splitting. It does block `import` statements, but
   `vitalforge_dashboard/app.py` already works around it: it inserts its own directory on
   `sys.path` and imports siblings flat (`import fit_import`, `from goals import ...`). A
   split was therefore always possible without any rename.

   Continued with the rename anyway: it was the chosen option, flat top-level imports put
   names like `sync` and `goals` in the global module namespace where two services can
   collide, and the dashboard's own comment calls it a hack. The rename now makes deleting
   that hack possible — filed as a follow-up, deliberately not bundled here.

2. **Task A6 needed a second pass.** The first replacement pattern only covered
   `vitalforge-*.app`, which missed the dashboard's other importable siblings
   (`.correlations`, `.readiness`, `.recommendations`, `.sync`). Caught by three test
   collection errors, not by review. Fixed by enumerating every remaining hyphen and
   classifying each as functional vs. deliberately-kept.

3. **Two false claims in CLAUDE.md corrected** (not in the plan's scope, but discovered
   while editing it): `shared/` was described as having "no pyproject.toml" and being
   imported "via `sys.path.insert(0, parent_dir)` in both app.py files". Both false —
   `packages = ["shared"]` + `pip install -e .`, and both services use plain
   `from shared.x import y`. The weight service has **zero** `sys.path` references.

## Issues Encountered

- Three test-collection `ModuleNotFoundError`s after the first pass (deviation 2). Resolved
  by exhaustive enumeration rather than pattern-guessing.
- No other issues. No rollbacks, no failed gates.

## Tests Written

None. This is a rename with zero behavior change; the existing 870-test suite is the lock,
and route-table parity is the structural gate. Writing new tests here would have tested the
rename rather than any behavior.

## Deliberately Unchanged

`compose` service keys, CI matrix `name:` (image tags), `/health` service strings,
`sw.js CACHE_NAME`, `pyproject` `packages`, and historical records under `docs/prp/`,
`docs/superpowers/`, `.claude/PRPs/plans/completed/`, `.reports/`, `.agent_native/`.

Verified in CI job output: `build-and-push (vitalforge-weight, vitalforge_weight/Dockerfile)`
— hyphenated image name, underscored path. Exactly the intended split.

## Next Steps
- [ ] Merge PR #47
- [ ] Deploy to 192.168.1.21 and confirm `/health` on both ports before starting Phase B
- [ ] Phase B: split `vitalforge_weight/app.py` into `weight_routes.py`,
      `activity_routes.py`, `garmin_claim.py` (plan section "Phase B")
- [ ] Follow-up (not blocking): delete the dashboard's `sys.path` hack now that
      `from vitalforge_dashboard.goals import ...` is possible
