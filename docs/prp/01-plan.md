# 01 — Plan: Track A (bearer token auth) + Track B (body-composition intake)

**Phase:** 1 (Planning). **Input:** `docs/prp/00-design.md` (§6 decisions resolved
2026-08-22: D1 = Option A `muscle_pct`, D2 = keep `weight_log` columns, D3 =
include the non-ASCII password fix in Track A).
**Status:** revised post-DA-review; ready for Phase 3 on Track A. Both exit gates
pass. The production schema dump (§4.1) and a real `get_weigh_ins()` response
(§4.2) both landed while this plan was being written. **One package, B5, is now
gated on a Phase 3 live checkpoint** rather than on a merge — see §4.3.

> **Revision note — 2026-08-22, post-DA review.** Revised to incorporate
> `docs/prp/02-validation.md` (13 objections, 2 blocking). Packages materially
> changed: **A1** (startup warning — F6), **A3** (revocation docs — F7),
> **B2** (weight bound → 422, PWA 422 rendering — F8), **B3** (pin
> `garminconnect`, signature conformance test, extended live-checkpoint brief —
> F4, F5), **B4** (atomic dedup + concurrency tests — F1; boundary — F12;
> corrected fallback cost — F9), **B5** (gated on the checkpoint; column naming
> — F4, F11). §5 gains a concurrency-testing note; §7's gate counts are updated.
> Two findings are **deliberately not applied to `main`**: F5's dependency pin
> lands on B3's branch, and F13's commit hygiene is the team lead's to action.
>
> **Second revision — 2026-08-22, post-fix-verification.** `02b-fix-verification.md`
> confirmed 10 of 13 findings cleanly resolved and found the remainder
> concentrated in **B4**. Applied here: F1 re-scoped so the transaction spans the
> `SELECT` and *whichever write follows* (the enrichment `UPDATE` was racing
> identically), plus
> `test_two_concurrent_enrichment_posts_update_once_and_push_once`; **N1**
> resolved by moving the Garmin push after `COMMIT` — the mandated in-transaction
> timeout named a mechanism that does not exist — which also reverses the route's
> write ordering for every request; **N2**'s misquoted 1.00 s stall corrected to
> 5.01 s; F9's note corrected to read in both directions. Track A and
> B1/B2/B3/B5/B6 were verified clean and are untouched by this pass.

Section references like §3.7 point at `00-design.md` unless prefixed with
"plan §".

Nine work packages: **A1–A3** (Track A) and **B1–B6** (Track B). Track A merges
completely and the Bascule contract is handed over before B1 starts — that is a
ground rule, not a scheduling preference.

---

## 1. Ordering and critical path

```
A1 ─→ A2 ─→ A3 ══╗  Track A merges. Contract handed to Bascule. ══╗
                                                                  ↓
                                        B1 ─→ B2 ─→ B3 ─→ B4 ─→ B5 ─→ B6
```

Every arrow is a hard dependency:

| Package | Depends on | Why |
|---|---|---|
| A2 | A1 | wires in the helper A1 builds |
| A3 | A2 | documents behavior A2 makes true |
| B1 | Track A merged | ground rule (the schema-dump dependency is resolved — §4.1) |
| B2 | B1 | writes to columns B1 creates |
| B3 | B2 | maps the DTO fields B2 defines |
| B4 | B2, B3 | dedup enrichment re-pushes via B3's mapping |
| B5 | B1 **+ the B3 live checkpoint** | migrates `weight_history` the same way, but its column names depend on units only the checkpoint can observe (§4.3) |
| B6 | B2–B5 | documents the merged surface |

**B5 must run after the B3 live checkpoint, not merely after B3 merges.** Its
*branch* (primary `weight_history`, no fallback) is settled — the key names are
confirmed (§4.2). Its *schema* is not: the column names depend on units that only
the checkpoint can observe (§4.3). B5 therefore cannot be pulled ahead of B4, and
the checkpoint must be scheduled between B3 and B5 rather than batched with the
others at the end. No other package's design is contingent.

There is no useful parallelism inside a track. The two tracks cannot overlap.

---

## 2. Track A packages

### A1 — Auth helper + constant-time comparison hardening

**Half-day estimate:** ~3h. Pure functions, no wiring, no route changes.

**Files touched**
- `shared/auth.py` — add `_API_TOKEN`, `_bearer_token_valid()`; fix
  `check_credentials()` to compare bytes; **add the startup misconfiguration
  warning** (F6).
- `tests/test_auth_token.py` — new.

**Startup warning (F6).** One statement at import, guarded so it fires only in
the silently-open configuration:

```python
if _API_TOKEN and not _PASS:
    logger.warning(
        "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty — "
        "auth is DISABLED and the token is inert. Set VITALFORGE_PASS to enable auth."
    )
```

It lands here rather than in A2 because A1 is where `_API_TOKEN` is introduced.
It does **not** contradict §2.4's no-logging rule, which the design has now
narrowed in writing to "no logging **on the per-request auth path**" — this fires
once per boot and contains no credential.

**Scope.** Builds the helper and leaves it unwired, so every test here is a
fast pure-function test with no ASGI machinery. Wiring is A2's job; splitting
them keeps A2's 40-cell matrix from also debugging the comparison logic.

**One deliberate deviation from §2.2.** The design sketches two module globals,
`_API_TOKEN` (str) and `_API_TOKEN_BYTES`. **Implement one** — `_API_TOKEN` —
and call `.encode("utf-8")` inside `_bearer_token_valid`. Two globals must be
kept in sync by every test that monkeypatches them, and a test that patches only
one silently tests the wrong thing. One `.encode()` per authenticated request is
free. The security properties in §2.2 are unchanged.

**D3 rides along here.** `check_credentials` and `_bearer_token_valid` are the
same bug (`compare_digest` on `str` raises `TypeError` on non-ASCII → 500) in the
same file with the same fix. They are **separate code paths**, so each gets its
own named test — one passing does not imply the other.

**Named tests** (`tests/test_auth_token.py`)

| Test | Asserts |
|---|---|
| `test_bearer_valid_token_accepted` | correct token → `True` |
| `test_bearer_wrong_token_rejected` | mismatch → `False` |
| `test_bearer_empty_value_rejected` | `Authorization: Bearer ` → `False` (guard 2) |
| `test_bearer_whitespace_only_value_rejected` | `Bearer    ` → `False` |
| `test_bearer_rejected_when_token_unconfigured` | `_API_TOKEN=""` + correct-looking header → `False` (guard 1) |
| `test_bearer_rejected_when_token_configured_whitespace_only` | env `"   "` → inert, identical to unset |
| `test_bearer_scheme_case_insensitive` | `bearer` / `BEARER` / `BeArEr` all accepted |
| `test_bearer_wrong_scheme_rejected` | `Basic <token>`, bare `<token>` → `False` |
| `test_bearer_non_ascii_token_returns_false_not_typeerror` | `Bearer tökén` → `False`, **no exception** |
| `test_bearer_surrounding_whitespace_stripped` | `Bearer  <token>  ` → `True` |
| `test_check_credentials_non_ascii_password_returns_false_not_typeerror` | **D3**, separate path from the token |
| `test_check_credentials_valid_pair_accepted` | regression: the happy path still works |
| `test_check_credentials_rejects_wrong_user_and_wrong_pass` | regression, both operands |
| `test_startup_warns_when_token_set_and_pass_empty` | **F6** — `caplog` at WARNING in the A3 config |
| `test_startup_silent_in_the_other_three_configs` | no warning when `PASS` is set, or when neither is set |
| `test_startup_warning_contains_no_token_value` | the warning names the variables, never their values |

**Test mechanics.** A `set_token` fixture doing
`monkeypatch.setattr(shared.auth, "_API_TOKEN", ...)` — **module attribute, not
`setenv`**. The globals are read at import (§1.7), so `setenv` alone changes
nothing. Same trap `conftest.py` already documents for `DB_PATH` and
`push_weight`.

`_bearer_token_valid` takes a `Request`, so "pure function" here means *no ASGI
app and no HTTP round-trip* — the tests build a minimal
`starlette.requests.Request` from a hand-written scope dict, which is what lets
them stay fast and lets A1 ship unwired. The one case that genuinely needs
control over **raw** headers rather than a dict —
`test_bearer_first_authorization_header_wins`, two `authorization` headers on one
request — is deferred to A2, where the throwaway app already exists and a real
client can send them.

---

### A2 — Wire the bearer check in, and fix the 500→401

**Half-day estimate:** ~4h, most of it the matrix table.

**Files touched**
- `shared/auth.py` — `get_current_user()` step 2; middleware returns
  `JSONResponse(401)` instead of raising.
- `tests/test_auth_matrix.py` — new.
- `tests/test_auth_middleware.py` — new.

**Scope note — this package is not "dependency wiring."** The team lead's
package name comes from the token-auth spec, which assumed a FastAPI dependency
named `require_auth`. §1.1 verified that dependency has **zero call sites** and
that enforcement lives entirely in the middleware. A2 is therefore: the bearer
branch inside `get_current_user()`, plus the middleware's `JSONResponse`
replacement (§2.3). Both land together because the matrix cannot assert a status
code until the 401 exists.

**Named tests — the 40-cell matrix** (`tests/test_auth_matrix.py`)

One parametrized test, `test_behavior_matrix`, over
`(cell_id, config, credential_form, expected_status)` with
**`ids=` set to the cell name** (`A1-C0` … `A4-C9`) so a failure report names
the exact cell in §2.5 rather than an index. Structure:

- `CREDENTIAL_FORMS: dict[str, Callable[[str], tuple[headers, cookies]]]` — ten
  entries, C0–C9, each building the request bits for a given valid token.
- `MATRIX: list[tuple[str, Config, str, int]]` — 40 rows. The 20 A3/A4 rows
  (`PASS` unset → auth off → allow) are generated by a comprehension over the
  ten credential forms with a comment pointing at §2.5's A3 rationale; only the
  20 A1/A2 rows are written out by hand.

Run against a **minimal throwaway FastAPI app** with `add_auth_routes()` and two
stub routes (`/api/thing`, `/page`) — not against either real service. That
isolates auth behavior from route behavior and keeps the matrix fast. Both real
services get one smoke assertion each in `test_auth_middleware.py`.

**Named tests — middleware behavior** (`tests/test_auth_middleware.py`)

| Test | Asserts |
|---|---|
| `test_api_path_returns_401_json_not_500` | **the D1 fix**: status 401, `content-type: application/json`, body `{"detail": "Not authenticated"}` |
| `test_401_includes_www_authenticate_bearer` | header present and equal to `Bearer` |
| `test_401_body_does_not_echo_credentials` | presented token and cookie values absent from the response body and headers |
| `test_html_path_redirects_to_login_not_401` | **regression on the unchanged branch** — 302 to `/auth/login` |
| `test_valid_cookie_still_works_with_token_enabled` | the no-regression case the spec names |
| `test_health_exempt_in_all_four_configs` | `/health` 200 regardless of `PASS`/`TOKEN` |
| `test_auth_and_static_paths_exempt_from_enforcement` | `/auth/login`, `/static/...` never 401 or 302-to-login |
| `test_auth_login_with_valid_bearer_redirects_to_root` | **F7** — `/auth/login` is exempt from *enforcement*, not from `get_current_user()`, which it calls directly (`shared/auth.py:145`). A valid token there yields 302 → `/`, not the login page. Pins the corrected §2.5 claim. |
| `test_bearer_first_authorization_header_wins` | two raw `authorization` headers, valid **second** only → 401; deterministic, documented in §5.2. Deferred here from A1 because it needs raw headers, not a scope dict |
| `test_weight_service_api_401_shape` | one real-service smoke |
| `test_dashboard_service_api_401_shape` | the `shared/` blast-radius check (CLAUDE.md) |

**Live-system checkpoint after Track A merges** (Phase 3, JD): deploy, generate a
token, confirm PWA login still works **and** a `curl` with the bearer header logs
a weight. Recorded in `docs/prp/03-live-validation.md`.

---

### A3 — Env, README, and Tasker section rewrite

**Half-day estimate:** ~2h.

**Files touched**
- `.env.example` — add `VITALFORGE_API_TOKEN=` (empty) with the
  `secrets.token_urlsafe(32)` generation comment.
- `README.md` — env table row (after line 95); **Authentication** section (line
  158) rewritten for two credential types **and both revocation procedures**;
  **Tasker** section (lines 218–250) rewritten to bearer, **deleting** the
  cookie-copying instructions at lines 233 and 247–248 rather than appending to
  them.
- `tests/test_docs_drift.py` — new.

**Revocation asymmetry must be documented here (F7).** Before Track A, rotating
`VITALFORGE_SECRET` was the single "log everyone out" lever. After Track A it is
incomplete: a leaked bearer token survives a secret rotation untouched, and
rotating the token invalidates no cookie. An operator responding to a suspected
compromise by rotating the secret — the obvious move, and the only one the README
currently describes — would leave the longer-lived, more powerful credential
live. The Authentication section must state **both** procedures and that neither
implies the other. Cheap now, expensive after an incident.

**Named tests**

| Test | Asserts |
|---|---|
| `test_env_example_documents_api_token` | `.env.example` contains `VITALFORGE_API_TOKEN` |
| `test_readme_env_table_lists_api_token` | README env table row present |
| `test_readme_tasker_section_uses_bearer` | Tasker section contains `Authorization: Bearer` |
| `test_readme_tasker_section_no_longer_documents_cookie_copying` | `vf_session` absent from the Tasker section — catches an append-instead-of-rewrite |
| `test_readme_documents_both_revocation_procedures` | **F7** — Authentication section mentions rotating both `VITALFORGE_SECRET` and `VITALFORGE_API_TOKEN` |

These are content-drift guards, not behavior tests. **This is a new pattern for
this repo** — no existing test reads a doc file. They are four cheap lines that
make a docs package satisfy the "named tests" exit gate honestly rather than by
exception. Drop them if JD finds them noisy; say so rather than letting them rot.

---

## 3. Track B packages

### B1 — Additive migration + fixture tests

**Half-day estimate:** ~4h. **Unblocked** — JD's schema dump arrived (§4.1).

**Files touched**
- `shared/database.py` — five new columns in `CREATE TABLE weight_log`, plus the
  guarded `ALTER TABLE` loop; a comment recording the no-non-constant-`DEFAULT`
  constraint (§5.4's residual risk).
- `tests/test_migration.py` — new.
- `tests/fixtures/production_schema.sql` — **already present**, JD's dump.
- `tests/conftest.py` — a `production_schema_db` fixture: load the dump into a
  `tmp_path` DB, seed synthetic rows, hand back the path.

**Seed the migrated tables to production counts, not every table.** `weight_log`
= 17 rows and `weight_history` = 34 are the counts that matter — those are the
only two tables Track B migrates, and `test_migration_preserves_row_count` reads
them. The other ten tables load empty: seeding 277 rows of `body_battery` per
test buys nothing for a `weight_log` migration and slows every case. A test that
needs date-keyed data can call `scripts/seed_db.py`'s helpers, which already
cover exactly those tables.

**Fixture loader must filter `sqlite_sequence`.** Loading the dump verbatim
raises `OperationalError: object name reserved for internal use: sqlite_sequence`
(verified). Drop that one statement; SQLite recreates the table from
`weight_log`'s `AUTOINCREMENT`. See §4.1.

**Implementation shape** (§3.3): attempt-and-swallow, never `PRAGMA table_info`.
Swallow only `OperationalError` whose message contains `duplicate column name`;
**let `database is locked` propagate** so a container that cannot migrate fails
its lifespan and is restarted rather than serving against a half-migrated schema.

**Named tests**

| Test | Asserts |
|---|---|
| `test_fresh_db_create_table_includes_composition_columns` | the no-ALTER path |
| `test_init_db_adds_composition_columns_to_existing_weight_log` | the ALTER path against the fixture |
| `test_init_db_is_idempotent_across_two_runs` | second `init_db()` raises nothing, schema identical |
| `test_init_db_converges_after_partial_migration` | **the "killed during first boot" case**: apply 2 of 5 ALTERs by hand, run `init_db()`, assert all 5 present |
| `test_duplicate_column_error_swallowed_but_others_propagate` | a non-duplicate `OperationalError` re-raises |
| `test_existing_rows_have_null_composition_after_migration` | data preservation |
| `test_migration_preserves_row_count` | against the fixture's known count |
| `test_concurrent_init_db_both_succeed` | two connections racing the same ALTER — exercises the **swallow** path, see the caveat below |
| `test_database_locked_propagates_and_does_not_swallow` | another connection holds an exclusive write lock; `init_db()` **raises** rather than continuing against a half-migrated schema |
| `test_migrated_fixture_readable_by_previous_queries` | **the rollback check** — run the pre-change `SELECT`/`INSERT` statements verbatim (`app.py:98`, `:122`, `:146`) against the migrated DB |
| `test_production_schema_fixture_loads_and_matches_init_db` | the dump loads (with `sqlite_sequence` filtered) and its `weight_log` columns equal a fresh `init_db()`'s **pre-migration** set — the drift detector itself |
| `test_seeded_timestamp_format_matches_route_output` | fixture rows use `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, the format `app.py:81` writes and B4's dedup keys on (§4.1's residual) |

**Caveat on the concurrency tests.** `test_concurrent_init_db_both_succeed` runs
two connections from one process and one event loop, so SQLite serializes them:
one `ALTER` completes and the other gets `duplicate column name`. That is a real
test of the **swallow** path and of the two-container race's *outcome*, but it
does **not** exercise `database is locked` — the error §3.3 insists must
propagate. `test_database_locked_propagates_and_does_not_swallow` covers that
separately by holding a write lock on another connection (`BEGIN EXCLUSIVE`) and
asserting `init_db()` raises. Both are needed; neither substitutes for the other.
If the lock test proves flaky under CI timing, mark it rather than deleting it,
and record that the lock path is then unverified.

---

### B2 — Request DTO and validation

**Half-day estimate:** ~4h.

**Files touched**
- `vitalforge-weight/app.py` — `WeightIn` gains `model_config =
  ConfigDict(extra="forbid")`, the four composition fields with bounds, `source`,
  and a **`model_validator(mode="after")`** enforcing `2.0 ≤ weight_kg ≤ 500.0`;
  the route persists the new columns.
- `vitalforge-weight/templates/index.html` — add `source: "pwa"` (line 359) and
  **fix the toast to render an array `detail`** (line 370).
- `tests/test_weight_api.py` — extended.

**The weight bound is a 422, not a 400 (F8).** An earlier draft put it in the
route as a 400 on the reasoning that a kg limit cannot live on a field fed lbs.
That premise is false — `model_validator(mode="after")` is exactly the tool for a
derived cross-field constraint, and it yields a normal 422 with `loc: ["body"]`.
Nothing was traded off: the legacy `unit` 400 is untouched either way. Extending
a legacy quirk into brand-new surface area on a backward-compatibility rationale
that cannot apply to a never-before-existing validation was simply wrong.

**The PWA cannot currently display any 422 (F8c).** `index.html:370` does
`showToast(data.detail || "Failed to log weight", "error")`. On a 400 `detail` is
a string and renders fine; on a 422 it is an **array of objects** and the user
sees `[object Object]`. Track B adds five new 422 sources to this endpoint
(`extra="forbid"`, four range bounds, the `source` `Literal`) plus the weight
bound above — so this package makes the PWA's only error path unusable unless it
also flattens. One line, in a file B2 is already editing. The original design
checked that `extra="forbid"` would not *reject* the PWA; it never checked
whether the PWA could *display* the result.

**Named tests**

| Test | Asserts |
|---|---|
| `test_composition_fields_accepted_and_echoed` | round-trip through the response |
| `test_weight_only_payload_still_succeeds` | **regression** — the PWA/Tasker shape |
| `test_unknown_field_rejected_422` | `extra_forbidden`, `loc == ["body", "bodyFat"]` |
| `test_body_fat_below_floor_rejected_422` / `..._above_ceiling_rejected_422` | 2.9 / 75.1 |
| `test_body_fat_fraction_rejected_422` | `0.20` — the fraction-vs-percent guard |
| `test_body_water_bounds_rejected_422` | parametrized 29.9 / 80.1 |
| `test_muscle_pct_bounds_rejected_422` | parametrized 9.9 / 90.1 |
| `test_bone_mass_kg_bounds_rejected_422` | parametrized 0.4 / 10.1 |
| `test_bone_mass_in_grams_rejected_422` | `3200` — the unit-error case the bound exists for |
| `test_weight_above_500kg_rejected_422` / `test_weight_below_2kg_rejected_422` | **new behavior** (F8) — 422 via `model_validator`, `loc == ["body"]`; previously 200 with `synced_to_garmin: false` |
| `test_weight_bound_applies_after_unit_conversion` | 1200 lbs → 422 (544 kg); the bound is on derived kg, not the raw field |
| `test_invalid_unit_still_returns_400_not_422` | **unchanged** — pins the one legacy 400 so the 422 migration above cannot accidentally sweep it up |
| `test_pwa_toast_flattens_array_detail` | **F8c** — the template's error path renders a 422 `detail` array as readable text, not `[object Object]` |
| `test_source_literal_rejects_unknown_value_422` | `"basucle"` → 422 |
| `test_source_optional_defaults_to_null` | omitted → `NULL` |
| `test_composition_persisted_to_weight_log` | reads the row back |

**Playwright coupling — do not skip.** `tests/test_smoke_ui.py::test_weight_page_logs_an_entry`
drives a **real** `POST /api/weight` through the browser (it fills `#weightInput`
and clicks `#submitBtn`). Adding `source: "pwa"` to the template therefore
exercises `extra="forbid"` end-to-end: if the server does not accept `source`,
that test fails. It runs in a **separate process** — `pytest -m playwright` — per
`pyproject.toml`'s `addopts` and `docker.yml`'s two-step test job. B2 is the one
package where "the suite passed" is insufficient without that second invocation.

---

### B3 — Garmin payload mapping

**Half-day estimate:** ~3h.

**Files touched**
- `tests/conftest.py` — **sub-task 0, do this first** (see below).
- `vitalforge-weight/requirements.txt`, `vitalforge-dashboard/requirements.txt` —
  **pin `garminconnect==0.3.11`** (F5).
- `shared/garmin_client.py` — `push_weight()` gains keyword-only
  `percent_fat`, `percent_hydration`, `muscle_mass_kg`, `bone_mass_kg`.
- `vitalforge-weight/app.py` — derive `muscle_mass_kg` and pass through.
- `tests/test_garmin_mapping.py` — new.

**Pin the dependency here, on this branch — not on `main` now (F5).** Both
requirements files currently say `garminconnect>=0.2.38`: a floor, not a pin, so
two image builds a month apart can ship different library versions from identical
source. Track B's entire value rides on four kwarg names reaching that library.
The ground rule *"Do not modify the Garmin client's auth flow — it is fragile,
reverse-engineered, and working"* is an argument **for** pinning it; a version
pin touches no auth flow and is trivially reversible. It lands with the
conformance test below rather than as a loose edit to `main`, per F13.

**Sub-task 0 (≈5 minutes, blocks everything else in this package).**
`FakeGarminClient.add_body_composition` currently signs as
`(self, timestamp, weight, **kwargs)` and **discards `**kwargs`**
(`conftest.py:45-47`). Until it records them, every mapping assertion below is
vacuously true. Change it to append the kwargs, and add
`test_fake_client_captures_composition_kwargs` as a meta-test so the seam itself
is covered.

Note the second seam: `weight_app_module` replaces `push_weight` wholesale with
`fake_push_weight` (`conftest.py:133-138`), which also drops composition. Extend
that fake in the same sub-task or the route-level mapping tests assert nothing.

**Do not touch `authenticate()`, `get_client()`, or the garth token flow** —
ground rule.

**Named tests**

| Test | Asserts |
|---|---|
| `test_real_client_signature_accepts_our_kwargs` | **F5, the highest-value test in this package** — `set(inspect.signature(garminconnect.Garmin.add_body_composition).parameters) >= {"percent_fat", "percent_hydration", "bone_mass", "muscle_mass"}`. The only test here that exercises something we do not control; a `**kwargs` fake accepts every name, so without this an upstream rename leaves all ten mapping tests green while production silently stops recording body fat. |
| `test_fake_client_captures_composition_kwargs` | the seam works (meta-test) |
| `test_body_fat_maps_to_percent_fat` | kwarg name and value |
| `test_body_water_maps_to_percent_hydration` | kwarg name and value |
| `test_muscle_pct_derives_muscle_mass_kg` | 100 kg @ 40% → `muscle_mass == 40.0` (**D1 Option A**) |
| `test_bone_mass_kg_passes_through_unconverted` | no scaling applied |
| `test_omitted_composition_passed_as_none` | `None`, not `0` — FIT's invalid sentinel |
| `test_push_weight_positional_call_still_works` | **back-compat** — the existing two-arg call site |
| `test_weight_only_push_sends_no_composition_values` | all four kwargs `None` |
| `test_garmin_failure_still_stores_composition_locally` | `synced_to_garmin: false` + row complete |
| `test_no_composition_values_in_log_output` | `caplog` — §2.4's logging audit made executable |

### B3 live checkpoint — extended brief *(gates B5)*

One real weigh-in with composition, run by JD. **This is the only test that
cannot be faked** — schedule it explicitly. Three things must be *recorded*, not
just observed, because later packages depend on the answers:

1. **The composition is visible in Garmin Connect** — the original checkpoint.
2. **The raw `get_weigh_ins()` response, read back after the push, with the
   observed units of `boneMass` and `muscleMass` written down (F4).** This push
   is the first and only event that can make those fields non-null, so it is the
   only opportunity to observe their units. **B5's column names depend on this**
   (`_g` vs `_kg`) and B5 cannot start until it is answered — see §4.3. Working
   hypothesis: grams, by precedent from `weight_history.weight_grams`.
3. **Whether Garmin collapses two same-timestamp uploads**, if a duplicate push
   can be arranged — this settles §3.7's enrichment question and B4's fallback
   (see B4's corrected cost note).

Record all three in `docs/prp/03-live-validation.md`.

---

### B4 — Dedup

**Half-day estimate:** ~5h — revised up. The atomicity work below is not a tweak
to the original package.

**Files touched**
- `vitalforge-weight/app.py` — reorder to
  **read → write → commit → push → update-flag**, with the `SELECT` and the
  write inside one `BEGIN IMMEDIATE` transaction and the Garmin push *outside*
  it; the ±50 g / 60 s lookup; the three sub-cases from §3.7.
- `shared/database.py` — index on `weight_log(timestamp)` (additive).
- `tests/test_dedup.py` — new.
- `tests/test_dedup_concurrency.py` — new.

**Atomicity is the point of this package now (F1, §6 D5).** The original design —
select, then push, then insert, with no transaction — is check-then-act across
`await` points. Two concurrent POSTs both see no duplicate and both push, which
is *exactly* the outcome the feature exists to prevent, on a single uvicorn
worker, via asyncio interleaving alone. The DA demonstrated it against a faithful
model of the route. **Every one of this package's original eleven tests issued
one POST at a time, so the package could have shipped green with the feature
broken.**

JD's call is **Option A**: `BEGIN IMMEDIATE` on one connection. Fix-verification
(`02b-fix-verification.md`) then corrected its **scope** and its **ordering** —
same mechanism, D5 not reopened:

```
1. BEGIN IMMEDIATE
2. SELECT for a duplicate match
3. Branch, and write inside the same transaction:
     no match          -> INSERT the new row
     enrichment match  -> UPDATE the NULL composition columns
     collapse/conflict -> no write
4. COMMIT                    <- lock released; pure DB work, no network inside
5. Push to Garmin            <- outside any lock
6. UPDATE synced_to_garmin / garmin_error from the push outcome
```

**Three implementation constraints:**

- **The transaction spans the write, not the `INSERT`.** An earlier revision
  scoped it to the `SELECT` + `INSERT`, covering one of three sub-cases.
  **Sub-case 2 (enrichment) is a read-modify-write with the identical race**: two
  concurrent composition-bearing POSTs both `SELECT` the same row, both see NULL
  columns, both `UPDATE`, both re-push. That is F1 again, one sub-case over — and
  the first set of concurrency tests would not have caught it, reproducing
  exactly the gap that let F1 through originally.
- **The Garmin push is outside the transaction, and needs no timeout.** The
  previous wording required "an explicit timeout shorter than the SQLite busy
  timeout" — **a mechanism that does not exist.** Verified against the pinned
  `garminconnect==0.3.11`: neither `add_body_composition` nor `Garmin.__init__`
  takes a `timeout`; the call bottoms out in garth's session (adjacent to the
  protected auth flow); and it is **synchronous** — `push_weight` is not a
  coroutine and `app.py:87` calls it without `await` inside an `async def` — so
  `asyncio.wait_for` cannot bound it either. Moving the push after `COMMIT`
  removes the requirement instead of satisfying it. For the record, the cost this
  avoids is 5× what an earlier draft pasted; reproduced against `get_db()` as
  written:

  ```
  default busy_timeout (ms): 5000
  concurrent writer: FAILED after 5.01s -> OperationalError: database is locked
  concurrent READ ok -> 0
  ```

- **Use `julianday(timestamp)`, not string comparison.** `/api/weight/trend`
  (`app.py:146`) compares an ISO-8601 `+00:00` string against `datetime('now')`
  and works only because `'T' > ' '` (§5.6). The DA confirmed `julianday()`
  normalises the stored offset correctly; do not copy the trend endpoint's
  pattern here.

**This reverses the route's ordering for every request, not just duplicates.**
Today the route pushes to Garmin *before* writing to SQLite (§1.4); after this
package it writes and commits first. That is a second, smaller behavior change
riding along with the concurrency fix — externally, a row now exists briefly at
`synced_to_garmin = 0` before the push is attempted. The response shape is
unchanged. Its residual risk: a crash between steps 5 and 6 leaves a row
permanently reading `synced_to_garmin = 0` for data Garmin holds — a sub-second
window, and a far narrower exposure than holding a write lock across an unbounded
network call. The DA's earlier warning against this ordering was **explicitly
retracted by its author** in `02b-fix-verification.md`.

**No new test dependency.** Time is not mocked and `freezegun` is not needed:
tests seed the prior row with a controlled `timestamp` via direct SQL, then POST.
The window is evaluated against `now`, so controlling the stored row is
sufficient and keeps CI's dependency list unchanged.

**Named tests**

| Test | Asserts |
|---|---|
| `test_duplicate_within_window_returns_deduplicated_true` | 200, `deduplicated: true` — **not 409** |
| `test_duplicate_not_pushed_to_garmin_twice` | `pushed_weights` length stays 1 |
| `test_dedup_response_returns_original_row_id_and_timestamp` | the *original* timestamp, not the request's |
| `test_dedup_tolerance_49g_collapses` | boundary, inside |
| `test_dedup_tolerance_exactly_50g_collapses` | **F12** — the boundary itself. The original pair bracketed 50 g from both sides without ever landing on it, so `<` vs `<=` was an implementer coin-flip in a value §4.5 makes a contract guarantee. Pins `abs(delta) <= 50`. |
| `test_dedup_tolerance_51g_creates_second_row` | boundary, outside |
| `test_dedup_ignores_source` | bascule then tasker → collapsed |
| `test_weighins_600s_apart_both_stored` | **the DA's ten-minutes case** |
| `test_enrichment_updates_null_columns_and_repushes` | §3.7 sub-case 2 |
| `test_enrichment_uses_original_timestamp` | the pushed timestamp equals row 1's |
| `test_enrichment_push_failure_sets_synced_to_garmin_zero` | the column means "current contents are in Garmin" |
| `test_conflicting_value_does_not_overwrite_and_flags_conflict` | §3.7 sub-case 3 |

**Named concurrency tests** (`tests/test_dedup_concurrency.py`) — the gap that let
F1 through:

| Test | Asserts |
|---|---|
| `test_two_concurrent_identical_posts_store_one_row` | `asyncio.gather` of two identical POSTs → exactly one row. **This is the test the original package was missing**; without it the feature ships broken. |
| `test_two_concurrent_identical_posts_push_to_garmin_once` | `pushed_weights` length 1 — the second-order damage, a double Garmin record via a path §3.7 never modelled |
| `test_two_concurrent_distinct_posts_both_stored` | the transaction serialises without over-collapsing |
| `test_two_concurrent_enrichment_posts_update_once_and_push_once` | **the gap fix-verification found** — two concurrent composition-bearing POSTs against one stored row: the second serialises, observes the now-non-NULL columns, and falls through to collapse/conflict. Exactly one `UPDATE`, exactly one re-push. Without this the enrichment race ships untested the way F1 did. |
| `test_concurrent_writer_not_blocked_by_garmin_push` | a `sync.py`-style `upsert()` issued while a Garmin push is in flight **succeeds** — proves the push is outside the transaction and the lock never spans the network call |

**The enrichment re-push ships implemented; its fallback is gated on the B3 live
checkpoint — and the fallback is not cheap (F9).** An earlier draft called it
"a one-line change plus deleting two tests." That materially understates it.
§3.7 defines `synced_to_garmin` as "this row's current contents are in Garmin."
Under the fallback an enriched row's contents are by definition **not** in
Garmin, so it must be 0 — and **nothing will ever set it back to 1**, because the
fallback's whole point is that nothing re-pushes. That leaves a permanent class of
rows marked unsynced with no repair path, in a system that has no reconciliation
process at all (§5.3 Path B), while §4.4 tells Bascule what that flag means. And
because §3.5 routes composition to the dashboard only via the Garmin round-trip,
that composition is then invisible everywhere except the `weight_log` columns §6
D2 kept for failure forensics.

So the checkpoint is a real decision between two costly outcomes, not a toggle.
If JD wants a genuinely cheap fallback, the third option §3.7 rejected — store a
second row — deserves reconsideration: "two local rows plus two Garmin records"
is at least self-consistent, whereas "one local row that permanently claims to be
unsynced" is not.

**Note the F1 interaction, in both directions.** An earlier draft recorded only
one of them — that until the atomicity fix lands, two concurrent POSTs may both
miss the duplicate and store separate rows instead of enriching, so the
enrichment path is not reliably reachable in the scenario it targets. True, but
it treated the fix as something that merely *makes* enrichment reachable. The
other direction is the one that mattered: **the enrichment write needs the
transaction too**, and the first revision did not specify it that way. Both are
resolved by scoping the transaction to the `SELECT` plus whichever write
follows.

---

### B5 — Dashboard metrics exposure

**Half-day estimate:** ~3h. **BLOCKED on the B3 live checkpoint** — see §4.3.
Key *names* are confirmed (§4.2); the *units* are not, and the column names
depend on them.

**Files touched**
- `shared/database.py` — `weight_history` gains `body_water` plus two
  **unit-suffixed** mass columns (same guarded-ALTER treatment as B1).
- `vitalforge-dashboard/sync.py` — `sync_weight_history()` reads the new keys off
  `latestWeight`.
- `vitalforge-dashboard/app.py` — three `METRIC_TABLES` entries.
- `tests/fixtures/garmin/weigh_ins.json` — extended with the **real** key names.
- `tests/test_dashboard_api.py` — extended.

**Key names are confirmed, not guessed** (§4.2): `bodyWater`, `boneMass`,
`muscleMass` off `latestWeight`, alongside the `bodyFat` `sync.py` already
reads. The fixture carries them. §3.5's `weight_log` date-aggregation fallback
is **not needed and is not being built** — no contingent branch remains here.

**But the units are unverified, and this package cannot start until they are
(F4).** Names were confirmed; units never were, and could not be — every
composition field in the live account is `null`, because nothing has pushed
composition to Garmin yet. That is what B3 changes. The B3 live checkpoint is
therefore a **hard predecessor**, not just an ordering preference.

This is structurally untestable in advance:
`tests/fixtures/garmin/weigh_ins.json` holds synthetic values (`boneMass: 3.2`
next to `weight: 81200` — kg and grams in one object), and `sync.py` stores
whatever number is there, so `test_sync_populates_composition_from_weigh_ins_fixture`
passes under grams, kg, or any unit at all. **The one thing this package's tests
exist to catch is the one thing they structurally cannot.** If the units are
grams and we assume kg, the dashboard plots bone mass as ~3200 against a
`weight_log` storing 3.2, silently and with no error.

**Column naming (F11).** §3.2 and §4.3 make the unit suffix load-bearing — *"the
`_kg` suffix is the only defense"* against a lbs/kg mixup range validation cannot
catch. An earlier draft of this package then added bare `muscle_mass` /
`bone_mass` columns, stripping that convention for the same quantity in the same
design. Name them for the **observed** units once the checkpoint reports:
**`bone_mass_g` / `muscle_mass_g`** if grams (the hypothesis, by precedent from
`weight_history.weight_grams`), **`_kg`** if kg. `body_water` needs no suffix —
it is a percentage.

The `METRIC_TABLES` **key** stays `"bone_mass"` / `"muscle_mass"` so the URL
surface carries no suffix; only the column name changes. Cost of doing this now:
zero, the columns do not exist yet. Cost of doing it after B5 merges:
`ALTER ... RENAME`, which the ground rules forbid as non-additive.

**Read the null case as the launch-day default.** Every composition field is
`null` in the live account today and stays null until the first Track B push
round-trips through Garmin. `sync.py` stores those as `NULL`, and
`/api/metrics/{name}` already filters `row["value"] is not None`, so the
endpoint returns an empty series rather than erroring — but nothing currently
proves that, because the fixture deliberately carries synthetic non-null values
to exercise parsing. The named test below closes that gap.

All three files change together — per CLAUDE.md, a new synced metric that misses
any one of schema / `sync.py` / `METRIC_TABLES` silently fails to be queryable.

**Named tests**

| Test | Asserts |
|---|---|
| `test_metric_tables_includes_body_water_muscle_bone` | the dict wiring |
| `test_metrics_endpoint_serves_body_water` | parametrized across the three new metrics |
| `test_sync_populates_composition_from_weigh_ins_fixture` | the fixture's real key names parse |
| `test_moving_average_computed_for_new_metrics` | the 7-day window still applies |
| `test_unknown_metric_still_returns_400` | regression on the existing guard |
| `test_weight_history_migration_preserves_existing_rows` | B1's rollback discipline, applied here |
| `test_composition_metrics_return_empty_series_when_garmin_values_null` | all four keys present but `null` — **production's actual state on launch day**: `count: 0`, empty `data`, no error, no crash in the moving-average loop |

---

### B6 — Documentation

**Half-day estimate:** ~2h.

**Files touched**
- `README.md` — API reference table (lines 278–281) with the extended `POST
  /api/weight` schema; the new dashboard metrics.
- `docs/prp/` — the §4 Bascule contract extracted for handoff if Bascule wants it
  as a standalone file.
- `tests/test_docs_drift.py` — extended.

**Named tests**

| Test | Asserts |
|---|---|
| `test_readme_documents_composition_fields` | all four field names present |
| `test_readme_documents_composition_units` | `_pct` / `_kg` naming survives an edit |
| `test_readme_api_table_lists_new_metrics` | `body_water`, `muscle_mass`, `bone_mass` |

---

## 4. Blocking dependencies

### 4.1 JD's production schema dump — **RESOLVED 2026-08-22**

**Received:** `tests/fixtures/production_schema.sql` — structure only, captured
read-only (`file:...?mode=ro`) from the live volume, no row data read or copied,
per CLAUDE.md.

What it settles:

- **`weight_log` DDL confirmed**, column order and `synced_to_garmin INTEGER
  DEFAULT 0` included. It **matches `shared/database.py` exactly — no drift**
  between code and the live schema at capture time. §3.3's migration plan is
  designed against the right starting shape.
- **`weight_history` DDL confirmed** (`date`, `weight_grams`, `bmi`,
  `body_fat`) — B5 migrates the table it expects.
- **Row counts:** `weight_log` = 17, `weight_history` = 34. Small enough that
  `test_migration_preserves_row_count` is trivially cheap.

**Two gotchas the dump introduces — both verified by running them:**

1. **The dump cannot be loaded verbatim.** It contains
   `CREATE TABLE sqlite_sequence(name,seq)` (a by-product of `weight_log`'s
   `AUTOINCREMENT`), and SQLite refuses to create that table directly:
   `OperationalError: object name reserved for internal use: sqlite_sequence`.
   **The fixture loader must filter that statement out.** SQLite then recreates
   `sqlite_sequence` on its own from the `AUTOINCREMENT` declaration — confirmed
   present after a filtered load, with `weight_log`'s six columns intact.
2. **Production runs SQLite 3.46.1; §1.3's behaviors were verified on 3.50.2.**
   The two relevant properties (`ADD COLUMN` auto-commits; duplicate raises
   `duplicate column name`) are long-standing and not version-sensitive, but B1
   should re-confirm them on 3.46.x rather than inherit the claim — one throwaway
   assertion, not a work item.

**Still open, and deliberately not blocking:**

- **The `timestamp` string format of existing rows.** The dump gives
  `timestamp TEXT NOT NULL`, which says nothing about the format. It is
  *derivable* rather than unknown: `app.py:81` writes
  `datetime.now(timezone.utc).isoformat()` →
  `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, and the confirmed no-drift finding means
  the code that wrote those 17 rows is the code we can read. The residual risk is
  only that an early row predates the current format. B4's dedup and the
  `/api/weight/trend` string-comparison quirk (§5.6) both key on this column, so
  **B1 should assert the format of the fixture's seeded rows explicitly** and, if
  JD is willing, confirm against `SELECT timestamp FROM weight_log LIMIT 3` —
  timestamps only, no weights.
- **`PRAGMA journal_mode`.** Not in the dump. §5.4's analysis assumes WAL, which
  `get_db()` sets on every connection (`database.py:14`), so this is
  self-answering for new connections; worth one confirmation at the Phase 3
  checkpoint rather than a blocker now.

**Representative rows are now synthesizable, and that is not the thing the
prompt forbade.** The prohibition was on *guessing the schema* — a fixture built
from `database.py` cannot detect drift from the live DB, which is the only reason
to have one. With the real schema in hand and confirmed drift-free, B1 seeds
synthetic rows against it, which CLAUDE.md's privacy rule actively prefers over
real weigh-in values.

**`scripts/seed_db.py` still cannot produce this fixture.** Verified: it seeds
`weight_history` and the other date-keyed metric tables and **never inserts into
`weight_log`** — the one table Track B migrates.

### 4.2 A real `get_weigh_ins()` response — **RESOLVED 2026-08-22**

A real response was captured from production (values redacted, key structure
real — same privacy discipline as the schema dump). **Confirmed key names:**
`bodyFat`, `bodyWater`, `boneMass`, `muscleMass` — camelCase, nested under
`latestWeight` (and mirrored on `allWeightMetrics`, `totalAverage`,
`previousDateWeight`, `nextDateWeight`). Also present, unused by this design:
`physiqueRating`, `visceralFat`, `metabolicAge`.

This matches what §3.5 assumed. **B5 takes the primary branch.** The
`weight_log` date-aggregation fallback is not needed and is dropped from the
plan — see B5.

`tests/fixtures/garmin/weigh_ins.json` is already updated with the real key
names; suite green at 54 passed (re-verified locally).

**One caveat that creates a real test gap.** Every composition field is `null`
in the live account, because nothing has ever pushed composition to Garmin —
that is exactly what Track B implements. The fixture therefore carries
**synthetic non-null values** so `sync.py`'s parsing is actually exercised.
That is the right call, but it means the suite no longer resembles production's
state at launch: on day one after Track B deploys, every one of these fields
comes back `null` until the first composition push round-trips. B5 covers that
explicitly with `test_composition_metrics_return_empty_series_when_garmin_values_null`
rather than leaving the null path untested by both the fixture and reality.

---

### 4.3 The B3 live checkpoint *(blocks B5)*

Units for `boneMass` / `muscleMass` on Garmin's read path are unverified and
unverifiable until the first real composition push makes those fields non-null
(F4). That push is the B3 live checkpoint. Its brief is extended to record the
observed units (see B3), and **B5 must not start until it reports** — the
`weight_history` column names depend on the answer, and correcting them later
would require a non-additive `ALTER ... RENAME`.

This is the only remaining schedule dependency in the plan, and unlike §4.1 and
§4.2 it cannot be resolved by asking JD for a capture: the data does not exist
yet anywhere, by construction.

---

## 5. Test strategy

**Route behavior: `httpx.AsyncClient` + `ASGITransport`, not `TestClient`.**
The token-auth spec and the Phase 1 brief both say "FastAPI TestClient"; this
repo does not use it. `tests/test_weight_api.py:11-15` and
`tests/test_dashboard_api.py` build an `AsyncClient` over
`ASGITransport(app=...)` under `asyncio_mode = "auto"`. Follow the repo. Note the
consequence: **`ASGITransport` does not run lifespan events**, so `init_db()` is
called by the `initialized_db` fixture instead — which is why B1's migration
tests drive `init_db()` directly rather than through a request.

**Garmin: faked at the seam, always.** Two seams exist and B3 must extend both —
`FakeGarminClient.add_body_composition` (`conftest.py:45`) and the module-level
`push_weight` replacement in `weight_app_module` (`conftest.py:133`). **No test
in any package may contact real Garmin**, and CI has no credentials, so a test
that tried would fail confusingly rather than safely. The only real-Garmin
verification in this whole effort is the Phase 3 live checkpoint, run by JD by
hand.

**Migration: against a fixture DB file, not a constructed schema.** See §4.1.
The fixture is loaded into a `tmp_path` copy per test; no test opens the real
file. `tmp_db_path` already enforces this by monkeypatching
`shared.database.DB_PATH` *and* setting `DB_PATH`/`GARTH_TOKEN_DIR`
(`conftest.py:89-104`).

**Module-attribute monkeypatching is mandatory for auth tests.** `_PASS`,
`_SECRET`, and `_API_TOKEN` are read at import time (§1.7). `monkeypatch.setenv`
alone changes nothing. Any test that varies `_SECRET` must also rebuild
`_serializer`, which is constructed from it at import.

**Playwright stays in its own process.** `pyproject.toml` excludes `-m playwright`
by default because pytest-playwright's session-scoped `browser` fixture keeps an
event loop in the main thread and breaks every pytest-asyncio fixture set up
afterward. CI already runs it as a second step (`docker.yml:41-42`). B2 is the
package that needs it (plan §3.2).

**Concurrency must be tested, not reasoned about.** F1 was a blocking defect that
eleven single-POST tests could not have caught, in a package whose motivating
scenario is two clients racing. Any package whose correctness depends on ordering
between requests needs at least one `asyncio.gather` test that drives the real
route (B4). The repo already has the pattern — `test_concurrent_init_db_both_succeed`
— so this is an omission to correct, not a new capability. Note the limit of the
technique: `ASGITransport` interleaves coroutines in one event loop, which is
sufficient here (a single uvicorn worker is exactly that), but it does not model
multiple processes.

**No new test dependencies.** Everything above uses pytest, pytest-asyncio,
httpx, and playwright — all already installed. CI pins nothing and installs an
explicit list at `docker.yml:26` that currently matches `pyproject.toml`'s `dev`
group in membership (versions are pinned only in `pyproject.toml`). No package
needs that list edited; if a future one does, both places must change together.

**CI is already sufficient.** `ruff check .` → `pytest -q` → `pytest -q -m
playwright`, as a `test` job gating `build-and-push`. Phase 2's "CI pinned" item
is effectively already met. `mypy` is not configured; adding it is out of scope
("extend, don't replace").

---

## 6. Rollback plan

**Code-only packages — A1, A2, A3, B2, B3, B4, B6.** No schema change, so
rollback is redeploying the previous image. Nothing to verify beyond CI. Stated
once here rather than repeated seven times.

Two nuances worth naming:
- **A2 changes an existing response** (500 → 401 on unauthenticated `/api/*`).
  Rolling back restores the 500. Bascule is told to treat the 401 as
  authoritative from the first Track A deploy (§4.4's compatibility note), so a
  rollback degrades it to the pre-existing ambiguity rather than breaking it.
- **B2 changes an existing response** (absurd weight: 200 → **422**, per F8).
  Same shape of risk, same resolution.
- **B4 changes the route's write ordering** (push-before-write →
  write-commit-push-update). Rolling back restores the old ordering *and* the F1
  race; the schema is untouched either way, so rollback is still a plain image
  redeploy with no data step.

**Schema packages — B1 and B5.** Rollback is still "deploy the previous image,
no data step", because every column added is nullable with no default and every
existing query names its columns explicitly (`app.py:98`, `:122`, `:146`;
`sync.py:27-30`). The previous image reads and writes a migrated database without
noticing the extra columns.

This is **verified, not assumed**, by a named test in each package:
`test_migrated_fixture_readable_by_previous_queries` (B1) and
`test_weight_history_migration_preserves_existing_rows` (B5), each running the
pre-change SQL verbatim against a migrated fixture.

The forward-only constraint that makes this hold: **no future migration may add
a column with a non-constant `DEFAULT`**, which would rewrite the table and
reintroduce a real interruption window (§5.4). B1 writes that constraint into
`shared/database.py` as a comment.

---

## 7. Exit gate assessment

**Gate 1 — every package has named tests: PASS.**
All nine packages name their tests, and each test name identifies a specific
assertion that could be written without re-reading the design doc.

Counts below are **test functions, not test cases** — several are parametrized,
most visibly A2's `test_behavior_matrix`, which is *one function* covering all 40
cells of §2.5 via `ids=` set to the cell name:

A1 = 16, A2 = 11 (one of them the 40-cell matrix), A3 = 5, B1 = 12, B2 = 17,
B3 = 11, B4 = 17 (11 behavioral + 5 concurrency + 1 boundary), B5 = 7, B6 = 3 —
**99 total**, up from 86 before the DA review.

The thirteen added tests are not padding; each pins a specific review finding:
F6 (3, A1), F7 (2, A2/A3), F8 (3, B2), F5 (1, B3), F1 (5, B4 — four from the
original finding plus `test_two_concurrent_enrichment_posts_update_once_and_push_once`
from fix-verification), F12 (1, B4).

The two docs packages (A3, B6) are covered by content-drift guards rather than
behavior tests, flagged as a new pattern for this repo rather than slipped in
silently.

**Gate 2 — migration fixture exists and matches production schema: PASS
(2026-08-22, on JD's dump arriving mid-plan).**

`tests/fixtures/production_schema.sql` is in the repo: structure captured
read-only from the live volume, no row data. It **matches `shared/database.py`
exactly — no drift** at capture time, which is the property the gate exists to
establish. `weight_log`'s DDL, `weight_history`'s DDL, and both row counts
(17 / 34) are confirmed rather than assumed, and nothing was guessed.

I verified the dump loads as a fixture before calling this PASS, which surfaced
one concrete gotcha: it must be loaded with the `CREATE TABLE sqlite_sequence`
statement filtered out, or SQLite refuses it outright (§4.1). B1 carries a named
test for the loader.

Two residuals, neither of which reopens the gate:
- The **`timestamp` string format** of the 17 existing rows is not in a
  structure-only dump. It is derivable from `app.py:81` given the confirmed
  no-drift finding, and B1 asserts it explicitly
  (`test_seeded_timestamp_format_matches_route_output`).
- **Production is SQLite 3.46.1**; §1.3's two behavioral claims were verified on
  3.50.2. Neither is version-sensitive, but B1 re-confirms rather than inherits.

**B1 is unblocked.** So is the entire Track A critical path, which was never
gated on this and which Bascule is waiting on.

---

## 8. Phase 2 outcome, and what carries into Phase 3

**The DA pass is complete** — `docs/prp/02-validation.md`, 13 objections (2
blocking, 3 high, 6 medium, 2 low), run cold by a fresh session. All six
mandatory targets got a real attempt and a disposition. Every finding is
incorporated into this plan and `00-design.md`; the revision notes at the top of
each file map findings to sections.

**How the targets landed:**

- **Auth ordering** — attacked directly and **upheld**. The reviewer tried to
  construct a raise inside `_bearer_token_valid` that would escape into a 500 and
  could not: Starlette decodes headers as latin-1, so `.encode("utf-8")` cannot
  raise. A weakening *was* found on a different axis — revocation (F7), now
  documented in A3.
- **Partial Garmin success** — the design's correction over-claimed and was
  itself falsified (F3). §5.3 now says "cannot fail independently *within a
  single upload*", which is what the evidence supports. The conclusion (no
  weight-only retry fallback) survives.
- **Dedup vs. two weigh-ins ten minutes apart** — safe, as designed. But the
  review found the feature **did not work under concurrency at all** (F1,
  blocking) and silently loses data on a burst flush (F2, blocking). Both are
  resolved in §6 D4/D5 and rebuilt into B4.
- **A3 cell group** — upheld; the missing mitigation (F6) is now in A1.
- **400/422 inconsistency** — keeping the legacy `unit` 400 is fine; extending it
  to a new bound was not, and the PWA could not render a 422 at all (F8). Fixed
  in B2.
- **Enrichment re-push** — decision upheld, cost corrected (F9). B4 now states
  what the fallback really costs.

**Red contract tests** (Phase 2 item 2, a separate persona — the DA writes no
code) come from A2's matrix and B2's validation table, both specified here in
enough detail to write red before any implementation exists. **Add B4's four
concurrency tests to that set**: they are the ones that would have caught F1, and
they should be red before the atomicity work starts.

**Carried into Phase 4 rather than resolved now:** the token grants
`DELETE /api/weight/{id}` while Bascule only POSTs — a scope-narrowing follow-up
the DA flagged as non-blocking, recorded in §4.1.
- **`ble-scale-sync` — closed, not a constraint on B2.** The token-auth spec
  names it as a live client whose payload is not in this tree, so
  `extra="forbid"` could in principle start returning 422 to it. JD's answer: he
  is unsure it is still needed, since it required keeping the scale physically
  near the server, which is not sustainable — he may be moving off it regardless.
  **B2 ships `extra="forbid"` with no special-casing.** If it turns out to still
  be in use and to send extra fields, the fix is to add the field or drop the
  client, not to weaken the validation.
