# Phase 2 handoff — 2026-08-29 (after PR 3)

State of the multi-tenancy work. Written so a fresh session can pick up cold.

## Where things stand

| | |
|---|---|
| `main` | `10fcdc4` — Phase 1 + Phase 2 PRs 1/4, 2/4 and 3/4, CI green |
| Open PRs | **none** |
| Open issues | **#37** — starlette PYSEC-2026-1942 (pre-existing, needs its own PR) |
| Branches | all merged branches deleted local and remote; tree clean |
| Production | `knowledge` (100.74.76.39), both services healthy, running **Phase 1** |
| Suite | 649 passed, 4 deselected, ruff clean, on `main` |

**Only PR 4/4 remains.**

### Prod is now FOUR merges behind `main`

It lacks #33 (snapshot race), #34 (PR 1), #35 (PR 2) and #36 (PR 3). Nothing is
urgent — migrations 001/002 are already applied there, and #33 protects the
*next* migration rather than one already run.

**A redeploy is still not routine.** PR 2 moved every person-scoped route to
`/p/{slug}/` and unmounts nothing yet, so the deployed PWAs' hardcoded `/api/...`
calls would 404 against new images. Treat a redeploy the way the Phase 1 upgrade
was treated: stop BOTH services, then bring them up together. The service-worker
`CACHE_NAME` bump to `-v2` is already in, so installed clients pick up the new
shell.

## What Phase 2 still needs (PR 4)

Per `2026-08-29-multitenancy-phase2-access-control.md` §PR 4:

- `api_tokens.person_id` (deltas A2, A3) — additive nullable column via
  `_add_columns`, **not** a migration-runner entry. Add `person_id` to
  `_Identity` and to `_resolve_bearer_token`'s SELECT; **audit every positional
  `_Identity(...)` construction**, since it is built positionally. Extend
  `tests/conftest.py`'s `seed_token()` to take an optional person here — it was
  deliberately left alone in PR 1 because the column did not exist yet.
- Ingest resolution order (§f.7): scoped token wins and a `{slug}` mismatch is
  **403**, not 404 (the caller demonstrably holds a valid token, so nothing
  leaks); otherwise `{slug}` is the subject via `require_person("manage")`.
  There is no third rule.
- The legacy unmount (§f.8) — remove root `/api/weight` and `/api/metrics/...`
  in the same change. No alias, no deprecation window.
- README/docs — Tasker/bearer URLs gain `/p/{slug}/`;
  `tests/test_docs_drift.py` will need extending; `CLAUDE.md`'s
  dashboard-read-endpoints bullet keeps its meaning but its URLs change.

## Start here for PR 4

1. Read the plan's **"Decisions taken before writing this plan"** (D2, D3, D6–D8)
   and **"Decisions taken while implementing PR 3"** (D9–D15) — the latter is
   new and includes two deviations from the plan text itself.
2. Read the **spec deltas** at the top of the plan. The design spec is stale in
   six places, and A2 (token identity "works unchanged") is simply wrong — which
   matters directly for PR 4, since §4.1 is the fix for it.
3. Read "Carried forward" below.
4. Branch from `main` at `10fcdc4`.

## Carried forward — read before starting PR 4

1. **An admin's API token is a whole-household read key.** Admins bypass grants
   for every non-archived person, and `_resolve_bearer_token` returns the
   owner's live role. `api_tokens.person_id` is what bounds the blast radius —
   this is PR 4's own §4.1, not a side note.

2. **`garmin_credential_person_id()` will need `int | None` in Phase 3.** It
   currently delegates to `get_primary_person_id()`, which raises. Once
   `garmin_links` exists, "this person has no linked account" is a normal
   answer, not an exception. Its docstring says so.

3. **`is_primary` is overloaded, and Phase 3 must unpick it.** It means both
   "the durable primary person" *and* "whose Garmin account this deployment
   holds", because `garmin_credential_person_id()` **is**
   `get_primary_person_id()`. PR 3 added the first request-path way to move that
   pointer and had to gate it behind `acknowledge_garmin_reassignment` as a
   result (D10). When `garmin_links` lands, the coupling — and that flag —
   should go away together. Do not add a second writer to `is_primary` before
   then.

4. **The `own`-holder grant API has no UI** (D14). The authorization path exists
   and is tested, but `/auth/admin/persons` is admin-gated and its user dropdown
   reads the admin-only `/auth/admin/users/list`. Phase 5's UI work, recorded so
   it is a decision rather than an oversight.

5. **The `users.id` existence oracle on grant writes is accepted, not closed**
   (D15). Revisit if grants ever gain a non-admin UI; addressing the target by
   username rather than id would make it a guess rather than a count.

## Closed since the last handoff

All three of the previous handoff's carried-forward defects landed in #36:

- `SLUG_RE` is now enforced at the creation route, which is the first route that
  mints a slug from external input.
- `_reachable_persons` in **both** services now denies the same grant values
  `require_person` denies, so `GET /` cannot redirect to a `/p/{slug}/` that
  then 404s.
- `sync_status.syncing` and `POST /api/sync` are per-person via `SyncRegistry`
  rather than answering from the module-level `_sync_lock`. The lock's
  *serialization* is still shared — that is Phase 4, deliberately untouched.

## Known issues, out of Phase 2 scope

- **#37 — `starlette 0.41.3` / PYSEC-2026-1942.** A `Range`-header DoS reachable
  **unauthenticated**, because `auth_middleware` skips `/static/` and both
  services mount `StaticFiles` there. Pre-existing. The issue carries the other
  15 `pip-audit` advisories triaged, the suggested pins, and a note to add
  `pip-audit` to CI — there is currently no dependency check at all.
- **Both service workers have the wrong scope.** They register as
  `/static/sw.js` with no `scope` option, so they control `/static/` and never
  navigations. Both PWAs therefore do not do what they appear to. Pre-existing.
  Fixing it needs `sw.js` served from root or a `Service-Worker-Allowed`
  header — a route change.
- **Playwright cannot run on the Fedora dev box.** Root cause is now known:
  `Not implemented` from Skia's fontconfig backend — missing system font libs,
  exactly the case `CLAUDE.md` warns about for non-apt systems. Confirmed
  identical on `main` with all changes stashed. CI is the authority for that
  lane and is green.

## Conventions this session established

- **Review the fix commit.** A commit written in response to a review is, by
  construction, the one nobody has checked — and it is where regressions land.
  The second review round of #36 found fragility that the *fix* commit had
  introduced (a release paired positionally rather than structurally), which no
  amount of re-reviewing the original would have surfaced.
- **A guard that a comment can satisfy is not a guard.**
  `ast.get_source_segment` returns source *including comments*, so a
  substring-over-source test passed against a fix that had been deleted and
  left only in the comment above it. Structural guards must assert over the AST
  or over behaviour, never over raw source text.
- **Parametrize authorization tests over every verb on the route family.** The
  grant routes' authorization could be deleted from both write routes with the
  full suite green, because only the read was exercised.
- **Mutation-test every new guard, and re-run after the review round.** 29
  mutations, all killed. Two of them exist because a reviewer proved the
  corresponding guards were decorative; two more because a later reviewer proved
  they were unbacked.
- **A positive control asserts state, not just status.** A route that 200s while
  writing nothing satisfies a status-only control.

## Things I got wrong, recorded so they are not re-derived

- I shipped a promote endpoint whose only warning was "scheduled syncs follow
  the primary person" — a scheduling-flavoured sentence for what is actually
  data contamination. The coupling to `garmin_credential_person_id()` was found
  by review, not by me, against a green suite and a passing mutation run.
- My first fix for the leaked `syncing` flag used a plain `set`, which a
  reviewer correctly showed did not close the regression: `scheduled_sync` never
  registered in it, so the boot backfill reported idle.
- I claimed "27 mutations, all killed" in a commit that also changed the
  re-archive cleanup — true of the list it enumerated, but that list did not
  cover the change. Enumerated-mutation claims must name what they cover.
- `monkeypatch.undo()` reverts **every** patch a test's fixtures made, including
  `conftest.py`'s temp `DB_PATH`. Restore a single patch by hand in a `finally`.
