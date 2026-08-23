# Implementation Report: User accounts & auth model

## Summary
Replaced the single shared `VITALFORGE_USER`/`VITALFORGE_PASS` credential pair with a
real, DB-backed multi-user model: a `users` table, scrypt-hashed passwords, two roles
(`admin`/`user`), live role re-checking on every request (not baked into the session
cookie), a self-service `/auth/account` page, and an admin-only `/auth/admin/users` page
with full CRUD and a last-admin-deletion/demotion guard. `VITALFORGE_USER`/`VITALFORGE_PASS`
now only seed the first admin account on first boot.

## Assessment vs Reality

| Metric | Predicted (Plan) | Actual |
|---|---|---|
| Complexity | Large | Large — confirmed accurate |
| Confidence | 9/10 (from the sibling security-fixes plan's scale; this plan didn't state one explicitly, estimated Large/high-confidence given exact code was specified) | High — every task landed close to the plan's literal IMPLEMENT blocks |
| Files Changed | 9 estimated | 11 actual (2 more than predicted — see Deviations) |

## Tasks Completed

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | `users` table | [done] Complete | Exact match to plan |
| 2 | Password hashing helpers | [done] Complete | Implemented together with Task 3 (see Deviations) |
| 3 | Async `get_current_user`/`require_auth`/`_is_auth_configured`, DB-backed `check_credentials` | [done] Complete | Exact match to plan |
| 4 | Startup bootstrap | [done] Complete | Exact match to plan |
| 5 | `/auth/account` and `/auth/admin/users` routes | [done] Complete | PATCH endpoint (role change/password reset) fully implemented, not left as an exercise |
| 6 | Role-based access tests | [done] Complete | Deviated — see below |
| 7 | Confirm no other call sites | [done] Complete | Grep returned zero hits, exactly as the plan predicted |
| 8 | Docs | [done] Complete | Exact match to plan |

## Validation Results

| Level | Status | Notes |
|---|---|---|
| Static Analysis | [done] Pass | `ruff check .` clean throughout — validated after every single edit, not just at the end |
| Unit Tests | [done] Pass | 285 total (282 default + 3 Playwright), up from 259 baseline — 26 net new tests |
| Build | N/A | No build step in this project (Python, no compilation) |
| Integration | [done] Pass | Playwright suite (real login flow through `weight_live_server`/`dashboard_live_server`) confirmed unaffected — auth stays open in that fixture exactly as the plan predicted, since no user ever gets seeded there |
| Edge Cases | [done] Pass | Live role re-check (demotion/deletion), last-admin guard (both deletion and self-demotion paths), bootstrap idempotency, bootstrap no-op when `VITALFORGE_PASS` empty or a user already exists, malformed stored-hash handling, non-ASCII password handling — all covered with dedicated tests, manually verified against real pre-fix code where practical |

Also did two rounds of manual/structural validation beyond the plan's own checklist:
rendered `/auth/account` and `/auth/admin/users` against a real seeded admin and confirmed
correct HTML/JSON; extracted both new pages' inline `<script>` blocks and ran them through
`node --check` to confirm the embedded JS (including template literals) is syntactically
valid — none of the automated tests exercise real browser JS execution for these two new
pages, so this closes a gap the plan's own Playwright validation command couldn't have
caught (the smoke tests only visit each service's main page, not these new `/auth/*` pages).

## Files Changed

| File | Action | Lines |
|---|---|---|
| `shared/auth.py` | UPDATED | +489/-6 (net) |
| `shared/database.py` | UPDATED | +19 |
| `tests/conftest.py` | UPDATED | +20 (`seed_user` helper) |
| `tests/test_auth_matrix.py` | UPDATED | ~54 changed — not in the plan's file list, see Deviations |
| `tests/test_auth_middleware.py` | UPDATED | ~38 changed |
| `tests/test_auth_token.py` | UPDATED | ~46 changed |
| `tests/test_user_management.py` | CREATED | 251 lines, 25 tests |
| `tests/test_scenarios_e2e.py` | UPDATED | 12 changed — not in the plan's file list, see Deviations |
| `README.md` | UPDATED | +35/-9 |
| `vitalforge-weight/app.py` | VERIFIED, no changes | Grep confirmed no direct call sites, per plan |
| `vitalforge-dashboard/app.py` | VERIFIED, no changes | Same |

## Deviations from Plan

1. **Two test files needed updating that the plan didn't list**: `tests/test_auth_matrix.py`
   (the 40-cell behavior matrix — its entire premise was `VITALFORGE_PASS`-driven, which
   this plan explicitly obsoletes) and `tests/test_scenarios_e2e.py` (a Phase-4-era test
   whose `_configure_auth` helper monkeypatched `_PASS`, which stopped having any effect).
   **Why the gap**: the plan's Mandatory Reading correctly identified these files'
   *patterns* to mirror but didn't flag that `test_auth_matrix.py` specifically would need
   a near-total rewrite of its master-switch dimension, or that `test_scenarios_e2e.py`
   existed as fallout at all (it postdates the plan's own codebase exploration). Both were
   caught immediately by the "validate after every task" discipline — `test_auth_matrix.py`
   failed loudly with `PermissionError` (trying to create `/app/data` since these tests
   never needed DB isolation before), and `test_scenarios_e2e.py` failed one test loudly
   and would have silently passed two others for the wrong reason had I not checked (see
   Issues Encountered).
2. **Task 6's role-access tests landed in `tests/test_user_management.py`, not
   `tests/test_auth_middleware.py`** as the plan specified. Grouped with the rest of the
   new admin-route tests instead, for cohesion — all in one file rather than split across
   two for a few parametrized cases.
3. **Password hashing helpers (Task 2) and the async auth rewrite (Task 3) were implemented
   as one combined edit**, not two sequential ones — they're too intertwined to usefully
   separate (Task 3's functions call Task 2's helpers directly), though both were still
   validated and tested as logically distinct units.

## Issues Encountered

- **Self-inflicted test bug, caught before it shipped**: two new tests in
  `tests/test_user_management.py` (`test_admin_users_list_requires_auth`,
  `test_change_own_password_requires_auth`) initially made unauthenticated requests
  *without first seeding any user* — which means `_is_auth_configured()` was `False` in
  those tests, auth was effectively off, and `require_auth` returned `"anonymous"` instead
  of raising. One failed loudly (403 instead of the expected 401, since "anonymous" then
  failed the *role* check). The other would have passed with a 401, but for the wrong
  reason (a `check_credentials("anonymous", ...)` lookup failing, not `require_auth`
  itself). Fixed both by seeding a user first to genuinely turn auth on before asserting
  the unauthenticated case — this is the exact "empty `users` table = open access,
  everywhere, including admin routes" behavior the plan's Approach section names as
  intentional, encountered directly rather than just reasoned about.
- **One straightforward typo** during a multi-line edit (a `def` missing its `async`
  prefix) caught immediately by the next validation run (`SyntaxError: 'await' outside
  async function`) — fixed in one line, no downstream effect.
- No other issues. No deviation required from the plan's core Approach, GOTCHAs, or Risk
  mitigations — every GOTCHA the plan flagged in advance (circular import direction,
  last-admin guard counting *other* admins not just row count, scrypt parameters being
  fixed constants, `_bearer_token_valid` staying untouched) held exactly as predicted.

## Tests Written

| Test File | Tests | Coverage |
|---|---|---|
| `tests/test_auth_token.py` | +7 net (3 new hash tests, 1 new unknown-username test, 3 rewritten `check_credentials` tests) | `_hash_password`/`_verify_password` round-trip, salt uniqueness, malformed-hash handling, DB-backed `check_credentials` |
| `tests/test_auth_matrix.py` | 40 (unchanged count, rewritten master-switch dimension) | Full behavior matrix re-verified against the new users-table-driven auth-configured check |
| `tests/test_auth_middleware.py` | 14 (unchanged count, several rewritten) | Middleware-level regression coverage for the async conversion |
| `tests/test_user_management.py` | 25 (all new) | Password change, admin CRUD, last-admin guard (both deletion and demotion), bootstrap idempotency and its two no-op conditions, live role re-check on both deletion and demotion |
| `tests/test_scenarios_e2e.py` | 4 (unchanged count, fixed to genuinely exercise auth again) | Cross-service token/cookie scenario tests, now seed a real user instead of a no-op `_PASS` monkeypatch |

## Next Steps
- [ ] Code review via `/code-review`
- [ ] Create PR via `/prp-pr`
- [ ] Once merged, Phase B (`.claude/PRPs/plans/per-user-api-tokens.plan.md`) becomes
      implementable — it depends on this plan's `users` table existing
