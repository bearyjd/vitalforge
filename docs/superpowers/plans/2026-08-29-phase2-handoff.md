# Phase 2 handoff — 2026-08-29

State of the multi-tenancy work at the end of this session. Written so a fresh
session can pick up cold.

## Where things stand

| | |
|---|---|
| `main` | `24f6acb` — Phase 1 + Phase 2 PRs 1/4 and 2/4, CI green |
| Open PRs | **none** |
| Branches | all merged branches deleted local and remote; tree clean |
| Production | `knowledge` (100.74.76.39), both services healthy, running **Phase 1** |
| Suite | 561 passed, 4 deselected, ruff clean, on `main` |

**Phase 2 is half done.** PRs 1/4 and 2/4 are merged; 3/4 and 4/4 remain.

### Prod is now THREE merges behind `main`

It lacks #33 (snapshot race), #34 (PR 1) and #35 (PR 2). Nothing is urgent —
migrations 001/002 are already applied there, and #33 protects the *next*
migration rather than one already run.

**But a redeploy is no longer routine.** PR 2 moved every person-scoped route
to `/p/{slug}/` and unmounts nothing yet, so the deployed PWAs' hardcoded
`/api/...` calls would 404 against new images. Treat a redeploy the way the
Phase 1 upgrade was treated: stop BOTH services, then bring them up together.
The service-worker `CACHE_NAME` bump to `-v2` is already in, so installed
clients will pick up the new shell.

## What Phase 2 still needs (PRs 3 and 4)

Per `2026-08-29-multitenancy-phase2-access-control.md`:

- **PR 3** — persons/grants CRUD, admin page mirroring `/auth/admin/users`,
  and the `admin_delete_user` cascade fix (`DELETE FROM person_grants` plus
  nulling `granted_by`).
- **PR 4** — `api_tokens.person_id`, the ingest resolution order (§f.7), the
  legacy unmount, and README/docs.

## Start here for PR 3

1. Read `2026-08-29-multitenancy-phase2-access-control.md` §PR 3 for scope, and
   the spec deltas at the top of it — the design spec is stale in six places
   against what shipped, and A2 (token identity "works unchanged") is simply
   wrong.
2. Read "Carried forward" below before writing anything. Item 1 is a
   requirement of PR 3 itself, not a nicety.
3. Branch from `main` at `24f6acb`.

## Carried forward — read before starting PR 3

These are findings from this session that PR 3 must handle. Each is a real
defect or a live requirement, not a nicety.

1. **`SLUG_RE` is enforced only in `shared/migrations.py` and
   `scripts/seed_db.py`.** PR 3 adds the first *route* that creates a person,
   which is exactly where an unvalidated slug enters. Jinja autoescape catches
   it at render time (verified: a forced payload renders as `&#34;` with no
   breakout), but validation belongs at creation.

2. **`_reachable_persons` and `require_person` disagree on an unrecognised
   grant value.** Both apps' `_reachable_persons` join `person_grants` without
   inspecting `access`; `require_person` denies it via `.get(granted, -1)`.
   Such a grant makes `GET /` redirect to a `/p/{slug}/` that 404s — a dead
   end. Unreachable without `PRAGMA ignore_check_constraints`, but both
   docstrings claim they mirror `require_person`.

3. **`sync_status.syncing` leaks across persons.** `app.py` returns
   `_sync_lock.locked()` — one module-level lock — on an otherwise
   person-scoped response, so one person's sync is visible from another
   person's status endpoint. The lock's *serialization* is Phase 4; the leaked
   flag is not.

4. **An admin's API token is a whole-household read key.** Admins bypass
   grants for every non-archived person, and `_resolve_bearer_token` returns
   the owner's live role. `api_tokens.person_id` (PR 4) bounds the blast
   radius.

5. **`garmin_credential_person_id()` will need `int | None` in Phase 3.** It
   currently delegates to `get_primary_person_id()`, which raises. Once
   `garmin_links` exists, "this person has no linked account" is a normal
   answer, not an exception. Its docstring says so.

## Known issues, out of Phase 2 scope

- **Both service workers have the wrong scope.** They register as
  `/static/sw.js` with no `scope` option, so they control `/static/` and never
  navigations. Both PWAs therefore do not do what they appear to. Pre-existing,
  unrelated to this phase. Fixing it needs `sw.js` served from root or a
  `Service-Worker-Allowed` header — a route change.
- **Playwright cannot run on the Fedora dev box.** `TargetClosedError` in
  pytest-playwright teardown, reproduces with all changes stashed. CI is the
  authority for that lane and is green.

## Conventions this session established

- **Structural guards over behavioural tests for scoping.**
  `tests/test_no_unscoped_person_access.py` asserts over source (AST, not
  substring) because this phase's failure mode is silent: a route on the old
  shim behaves perfectly with one person. All nine guards were confirmed to
  fail against the pre-sweep tree, so none can pass vacuously.
- **Mutation-test every new guard.** Three defects this session were found by
  mutation against a fully green suite, not by review. A guard that cannot be
  made to fail is decorative.
- **Assert the positive control.** Two vacuous positive controls shipped this
  session before being caught — one in a fixture test, one in the sync test.
  A negative test without a working positive control passes when the route is
  entirely broken.
- **404 hides person existence; account-scoped resources keep 403.** The
  discriminator is what kind of resource, not which route family. Written into
  the plan next to constraint 2.

## Things I got wrong, recorded so they are not re-derived

- I claimed the service-worker `CACHE_NAME` bump guarded a stale-shell failure
  mode. It doesn't — scope is `/static/`, so no install ever served a
  navigation from that cache. The bump is still correct hygiene.
- I claimed `require_person`'s anonymous-before-admin ordering was
  load-bearing. Mutation proved it isn't: the sentinel's role is `None`, so
  both orders behave identically. It is defensive, and the real invariant is
  pinned by `test_anonymous_sentinel_has_no_role`.
- I relayed one agent's prediction that ~35 validation tests would need grants.
  They didn't — those tests seed no users and run in open-access mode.
