# Phase 2 — access control and ingest routing

Implementation plan for Phase 2 of the family multi-tenancy design
(`docs/superpowers/specs/2026-08-25-family-multitenancy-design.md`, section (h) line 2076).

Baseline: `main` at `cf07748`. Phase 1 (#32) and the snapshot-race fix (#33) are merged and
Phase 1 is deployed and verified in production.

**Outcome:** multiple persons can exist, be viewed, and receive ingested measurements — but
only one has Garmin data. Per-person Garmin is Phase 3.

---

## Decisions taken before writing this plan

| # | Decision | Rationale |
|---|---|---|
| D6 | **Full sweep** — all 17 person-scoped routes move under `/p/{slug}/`, not only the 10 the spec's table names | §f.8's stated benefit is "one supplier of `person_id`, no second path to audit". Leaving 7 routes on the shim preserves exactly the implicit fallback this phase exists to delete. Half-scoping pays the URL-break cost *and* keeps the audit surface. |
| D7 | **Endpoints + a minimal admin page**, mirroring the existing `/auth/admin/users` pattern | The spec names "person CRUD" and "grant management UI" and then describes neither. Reusing the established admin-page style avoids inventing a second house style ahead of Phase 5. |
| D8 | **Four PRs, middleware first** | `shared/auth.py:1286` is a precondition, not a task (see PR 1). The legacy unmount lands last and atomically. |
| D2 | `goals` rows stay `user_id`-scoped; the **routes** move under `/p/{slug}/` and `require_person`'s `person_id` feeds `_goal_progress` | Cheapest reading consistent with §b.5:451-455. The spec says "revisit **if** one account needs separate goals per person" — it does not. |
| D3 | `scheduled_sync` **keeps** `get_primary_person_id()` through Phase 3 | It has no request, so `require_person` cannot serve it. The round-robin that would is Phase 4 (§h:2094). Writing this down is what stops Phase 4 work leaking forward. |

---

## Spec deltas — the spec is stale in six places

Verified against the code at `cf07748`. **Read these before implementing from the spec text.**

- **A1 — §f.4's placement paragraph (1750-1754) is dead.** Slug helpers shipped in
  `shared/slugs.py` with *public* names `SLUG_RE` / `RESERVED_SLUGS` / `slugify`, not in
  `shared/auth.py` with underscore prefixes. Import from `shared.slugs`; recreate nothing.
- **A2 — §f.7's "works unchanged" claim (1822-1824) is wrong.** Verified: `_Identity`
  (`shared/auth.py:81-85`) is a 4-field NamedTuple with no `person_id`, and
  `_resolve_bearer_token`'s SELECT (`:427-431`) does not fetch `api_tokens.person_id`. Rule 1
  of the ingest order needs both. `_Identity` is constructed **positionally** (e.g. `:181`), so
  adding a field means auditing every construction site.
- **A3 — `_API_TOKENS_ADDITIVE_COLUMNS` does not exist.** It is new. Existing lists are at
  `shared/database.py:20`, `:34`, `:47`.
- **A4 — §f.5's snippet is stale.** The real `admin_delete_user` body
  (`shared/auth.py:1264-1268`) has three deletes, not two — `goals` was added after the audit.
- **A5 — §g.4:2037-2044 is CLOSED, not open.** Phase 1 shipped `ensure_primary_person_grant()`.
  Do not re-litigate it into this phase.
- **A6 — §f.2's URL table is the eventual shape, not Phase 2's deliverable.** It lists
  `garmin/link` and `garmin/unlink`, which §h:2085 puts in **Phase 3**. Same trap in §f.6's
  "archiving unlinks first" — a no-op here.

---

## Global constraints

1. **`require_person` is the only way to obtain a `person_id` on a request path.** §f.1:1610
   is explicit: "there is no module-level helper that returns a `person_id` without
   authorizing, because such a helper is the thing that gets called by mistake." That helper is
   `get_primary_person_id()`. By the end of PR 2 it must have **zero** request-path callers.
2. **404, never 403, for a missing grant** (§f.1:1634). A 403 confirms the person exists and
   leaks household membership. "No such slug" and "no grant" return the *same* 404, deliberately.
   The one exception is ingest rule 1's token/slug mismatch, which **is** 403 (§f.7:1846) —
   the caller demonstrably holds a valid token, so nothing leaks. Keep these straight.
   **The rule is "person existence is never confirmed", not "every response under
   `/p/{slug}/` is a 404".** Account-scoped resources keep an ownership check:
   `_owned_goal_or_404` returns 404 for a missing goal and 403 for someone else's, matching
   `revoke_token`'s existing shape. That confirms a goal id exists inside the account; it says
   nothing about which persons exist or who can reach them, and `require_person` still gates
   the person first. The discriminator is **what kind of resource**, not which route family, so
   it does not generalise into an argument for a person-scoped 403. Stated here because a
   security review read constraint 2 as absolute and flagged the goals 403 as a violation — the
   next reader will do the same otherwise.
3. **One query, not two** (§f.1:1566). Identity and grant resolve in a single statement bound
   to the just-established identity; two queries leave a revoked-grant-still-usable window.
4. **The anonymous check precedes the admin check** (§f.1:1639). `shared/auth.py:181` returns
   `_Identity("anonymous", None, None, None)` when auth is unconfigured — `role` is `None` in
   that mode, so the ordering is load-bearing, not stylistic.
5. **Active persons only**: `archived_at IS NULL` lives in the dependency's query. Admin routes
   that must reach archived persons use `_require_admin` and address **by id**, never through
   this dependency.
6. **Slugs are globally unique including archived** (§f.4:1756). An archived person's slug is
   permanently taken. The alternative lets a stale bookmark or cached service-worker URL resolve
   to a different human's health data — "the worst failure this design can produce." No redirect
   from an old slug; it 404s.
7. **Person-scoped routes address by slug; admin collection routes address by id** (§f.2:1691).
8. **Never reintroduce an alias for the legacy paths** (§f.8:1897). If a forgotten client
   surfaces mid-phase, reconfigure the client.
9. **Playwright stays a separate process.** `addopts = "-m 'not playwright'"` must never be
   removed and the suites must never be merged (§Appendix B:2352). Person-scoped URLs change the
   smoke tests' navigation paths — "that is a URL edit, not a reason to reorganize the suites."

---

## PR 1 — preconditions

Nothing here changes a URL. Everything here must land before anything does.

### 1.1 The auth middleware 401/302 test — the blocker

`shared/auth.py:1286` decides 401-JSON + `WWW-Authenticate: Bearer` vs 302-to-`/auth/login`
using `path.startswith("/api/")`. Under `/p/{slug}/api/...` that is **false**, so every bearer
client the README documents (Tasker, Bascule) receives an HTML login page instead of a
machine-readable 401.

This appears in no phase's task list in the spec. It is a precondition: the moment one route
moves, the break is live.

- Broaden the test to match person-scoped API paths as well as root ones.
- `tests/test_auth_middleware.py:22` mounts a `/api/thing` fixture app specifically to exercise
  this branch. Add a `/p/{slug}/api/...` sibling.
- Assert the 401 body **and** the `WWW-Authenticate` header, not just the status.

### 1.2 `scripts/seed_db.py` is broken on `main` right now

`scripts/seed_db.py:145-155` builds `cols = ["date"] + list(columns.keys())` with no
`person_id`. Every metric table is now `person_id INTEGER NOT NULL`. Verified empirically — it
does **not** write invisible rows, it hard-fails:

```
IntegrityError: NOT NULL constraint failed: steps.person_id
```

A Phase 1 regression: `shared/`, both apps and `tests/` were updated; `scripts/` was missed, and
the suite never exercises that file. This is the documented way to get dashboard data without a
live Garmin account (roadmap item 2).

Fix it here rather than as a standalone PR, and give it a `--person` flag while in there —
Phase 2 needs to seed **two** people to test isolation at all.

### 1.3 Test fixtures

`tests/conftest.py` has no person fixtures. `seed_user()` (`:179-196`) creates a user with no
grant, so `require_person` would 404 every seeded user in the existing suite.

- Add `seed_person(slug, display_name)` and `grant_person(person, user, access)` fixtures.
- **`seed_token()` is NOT touched here.** `api_tokens.person_id` does not exist until PR 4, so
  extending the fixture belongs there, alongside the column — see §4.1. (An earlier draft of
  this plan listed it under PR 1, contradicting its own PR 4 scope.)
- Leave `production_schema_db` (`:113-159`) alone — it deliberately seeds pre-`person_id` rows.

**Gate:** full suite green, no URL changed yet.

---

## PR 2 — `require_person` and the route sweep

### 2.1 The dependency

Implement `require_person(level)` per §f.1:1559-1632 — reproduce the spec's code, it is
correct. `_ACCESS_ORDER = {"view": 0, "manage": 1, "own": 2}`. Access levels per §a.3:230:
`view` reads, `manage` writes and triggers sync, `own` archives and grants. A `users.role` of
`admin` bypasses grant checks entirely, matching `shared/auth.py:1107` — do not invent a
second superuser story.

### 2.2 Move all 17 routes (D6: full sweep)

Weight (`vitalforge-weight/app.py`): `POST /api/weight` (manage), `/api/weight/recent` (view),
`/api/weight/trend` (view), `DELETE /api/weight/{id}` (manage).

Dashboard (`vitalforge-dashboard/app.py`): `POST /api/sync` (manage), `/api/sync/status` (view),
`/api/metrics/{name}` (view), `/api/readiness` (view), `/api/export` (view),
`/api/recommendations` (view), `/api/recommendations/rules-only` (view), `/api/correlations`
(view), `POST /api/import/activity` (manage), `/api/activities` (view),
`/api/activities/{id}` (view), and the four goals routes (view — D2).

`DELETE /api/goals/{id}` stays account-scoped; it takes no `person_id` today.

**Three sites cannot be a `Depends`** — resolve in the route body and close over the value:

- `vitalforge-dashboard/app.py:129` — the `_do_sync()` closure runs via `create_task` *after*
  the response; no request scope remains.
- `:226` — `_export_rows` is an async generator consumed by `StreamingResponse` after the route
  returns. Thread `person_id` through `_export_rows` → `_export_csv` → `_export_json`.
- `shared/database.py:561` — `ensure_primary_person_grant()` is a startup bootstrap. **It stays
  a shim.** Replacing it breaks fresh-install grants.

### 2.3 `GET /` on both services

Redirect to `/p/{slug}/` using `users.default_person_id` (§f.2:1677 — that column builds this
redirect and nothing else). Per §a.2:224, `NULL` means resolve to the single person this user
can reach, **or 400 if ambiguous**. The spec does not cover *zero* reachable persons (D5); render
an explanatory page rather than a bare 400.

### 2.4 Frontend — 11 fetch sites

`vitalforge-dashboard/templates/index.html:410,419,427,523,524,559`,
`vitalforge-dashboard/static/correlations.js:82,89`,
`vitalforge-weight/templates/index.html:304,333,356,394`.

Inject **one base-path constant** from the template context rather than editing 11 sites
independently. Context is built at `vitalforge-dashboard/app.py:114-119` and
`vitalforge-weight/app.py:133-138`. Cross-service nav (`weightLink` / `dashLink`) needs the slug
too.

### 2.5 Service workers — the invisible failure

`vitalforge-dashboard/static/sw.js` and `vitalforge-weight/static/sw.js` both pre-cache `"/"`
and fall back to cache on network failure. **Bump `CACHE_NAME` in both.** If `/` becomes a
redirect and the cache name does not change, returning users get the old shell from cache
against the new backend, and it fetches unscoped URLs that now 404.

This is the one item whose failure mode is invisible in testing — a fresh browser profile never
hits it, only existing users do. Also update `start_url` in both `manifest.json`.

### 2.6 Tests

`tests/test_smoke_ui.py` navigation paths change (URL edit only — constraint 9). Roughly 12 test
files carry hardcoded URLs. `tests/test_fit_import.py:247-303` already contains cross-person
isolation tests and is the closest existing model for the isolation matrix this PR needs.

**Gate:** an isolation matrix test — for each moved route, a user with no grant gets 404, `view`
gets reads, `manage` gets writes, and admin bypasses. Zero request-path callers of
`get_primary_person_id()` remain (assert this with a grep-based test).

---

## PR 3 — persons, grants, and the admin page (D7)

### 3.1 Endpoints

`/api/persons` and `/api/persons/{id}`, admin-addressed **by id** so archived persons are
reachable: create (creator gets an automatic `own` grant, §f.6:1795), list including archived,
`PATCH` for slug and display-name rename under the same `SLUG_RE` / `RESERVED_SLUGS`
validation, and archive.

- **Archive, never delete** (§f.6:1796). Deleting means 11 tables with no FK cascade.
- **The primary person cannot be archived** while `is_primary = 1` — 409, with a message naming
  the fix (promote another person first) (§f.6:1803).
- Grants: `own` on the person, or any admin, may grant/revoke.
- **Revoking your own last `own` grant is permitted** (§f.6:1807) — deliberately *not* modeled
  on the "cannot demote the last admin" guard, because admins can always restore it. The spec
  asks for a comment saying so; the asymmetry otherwise reads as an oversight.
- **Zero-grant persons are a reachable, deliberate state** (§f.6:1812). A "cannot delete the last
  grant" guard is explicitly rejected — it would make deleting a *user* fail for reasons an admin
  cannot see from the users page. State it in code and release notes.

### 3.2 The `admin_delete_user` cascade fix (§f.5, delta A4)

Two statements into the existing `BEGIN IMMEDIATE` at `shared/auth.py:1250`, before
`DELETE FROM users`:

```python
await db.execute("DELETE FROM person_grants WHERE user_id = ?", (user_id,))
await db.execute("UPDATE person_grants SET granted_by = NULL WHERE granted_by = ?", (user_id,))
```

`users.id` is `AUTOINCREMENT`, so a reused id could otherwise resurrect a deleted account's
access, and a dangling `granted_by` becomes "an audit-trail lie." `users.default_person_id`
needs nothing — it is a column on the row being deleted.

`tests/test_api_tokens.py:272` already deletes a user and is the natural home for the regression
test.

### 3.3 Admin page

Mirror `/auth/admin/users`: list, create form, archive control, and per-person grant management.

---

## PR 4 — tokens, ingest, and the unmount

### 4.1 `api_tokens.person_id` (deltas A2, A3)

Also extend `tests/conftest.py`'s `seed_token()` to take an optional person here — it was
deliberately left alone in PR 1 because the column did not exist yet.

New `_API_TOKENS_ADDITIVE_COLUMNS = ["person_id INTEGER"]` — additive, nullable, no
non-constant default, so it needs `_add_columns`, **not** a migration runner entry. Add
`person_id` to `_Identity` and to `_resolve_bearer_token`'s SELECT; cookie identities supply
`None`. Audit every positional `_Identity(...)` construction. Token creation
(`shared/auth.py:1081`) gains optional person selection; `GET /auth/tokens` shows it.

### 4.2 Ingest resolution order (§f.7:1843)

1. If the bearer token has `person_id` set, **that** person is the subject — and the request is
   **403** if `{slug}` resolves to anyone else. A scoped token cannot be re-pointed by editing
   the URL.
2. Otherwise `{slug}` is the subject, authorized by `require_person("manage")`.
3. **There is no third rule.** No body field, no default, no "the primary person."

Per §f.7:1852 and §i Q4, a shared scale needs nothing more: **rule 2 is the confirmation** — the
human picks the person by posting from that person's PWA. No new endpoint, no pending-measurement
queue, no attribution heuristic.

### 4.3 Unmount the legacy paths (§f.8)

Remove the root `/api/weight` and `/api/metrics/...` routes **in the same change**. No alias, no
deprecation window. A forgotten client hits no route and gets a 404 — it never writes, and never
reads another person's data.

### 4.4 Docs

`README.md`: the Tasker/bearer URLs gain `/p/{slug}/`, and the open-access security note needs
the §f.3:1704 obligation — anonymous mode now grants implicit `own` on *several people's* data.
The kind of exposure is unchanged; the scale is not. `tests/test_docs_drift.py:21-30` asserts
README content and will need extending. `CLAUDE.md`'s dashboard-read-endpoints bullet keeps its
meaning but its URLs change (§Appendix B:2349).

---

## Explicitly out of scope

- **Phase 3**: `garmin_links`, `shared/garmin_registry.py`, per-person token stores,
  `/p/{slug}/api/garmin/link` + `/unlink`, step-up auth, removing `authenticate()` from the
  lifespans, §f.6's "archiving unlinks first."
- **Phase 4**: round-robin sync cursor, token bucket, 429 backoff into `backoff_until`,
  per-person error isolation. Note `_sync_lock` (`vitalforge-dashboard/app.py:50`) is a single
  module-level lock — under multi-person, one person's sync serializes everyone's. That is
  Phase 4's problem; do not fix it here.
- **Phase 5**: comparison views, household aggregates, cross-person templates.
- **Never**: any alias or compat layer for the legacy paths (§i Q8);
  `weight_log.person_id NOT NULL` (§i Q9).
- **Already done in Phase 1**: recommendations cache keyed by person; `ensure_primary_person_grant`.

---

## Risks

| Risk | Mitigation |
|---|---|
| A missed call site reads the wrong person **silently** — no crash | Grep-based test asserting zero request-path `get_primary_person_id()` callers; per-route isolation matrix |
| Service-worker cache serves the old shell to existing users only | `CACHE_NAME` bump in both `sw.js`; verify with a warm profile, not a fresh one |
| Bearer clients get HTML instead of 401 | PR 1 lands the middleware fix before any route moves |
| `_Identity` built positionally; a new field silently shifts args | Audit all construction sites; consider keyword-only construction |
| Playwright only runs in CI on this hardware | Treat CI as the authority; never merge the two suites to work around it |
