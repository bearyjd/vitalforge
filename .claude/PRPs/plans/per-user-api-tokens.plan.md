# Plan: Per-user API tokens (Phase B of settings-menu project)

## Summary
Replaces the single, shared `VITALFORGE_API_TOKEN` env var with per-user, named, DB-backed
bearer tokens: any user can create/revoke their own; an admin can see and revoke everyone's.
**Hard dependency: `.claude/PRPs/plans/user-accounts-auth-model.plan.md` (Phase A) must be
merged first** — this plan's `api_tokens` table has a foreign key into the `users` table
Phase A creates, and this plan's bearer-auth resolution reuses Phase A's async
`get_current_user`/role-lookup machinery directly.

## User Story
As a VitalForge user (self) or the admin (on behalf of anyone),
I want to generate a named bearer token for a specific unattended client (Tasker, Bascule,
a bridge script), see it exactly once, and revoke it independently of every other token,
So that a compromised or retired client's access can be cut off without invalidating every
other automation, and without editing `.env` and restarting the containers.

## Problem → Solution
**Today**: one `VITALFORGE_API_TOKEN` env var (`shared/auth.py:17`), one value, shared by
every unattended client. Revoking it (by changing the env var and restarting) breaks every
client at once. No UI, no visibility into which clients are using it, no way to tell which
integration a request came from beyond the free-text `source` field on `/api/weight`
payloads (`vitalforge-weight/app.py`'s `WeightIn.source`). → **A DB-backed `api_tokens`
table, one row per issued token, each owned by a user row (Phase A), each independently
revocable from a settings-page UI, with the raw value shown exactly once at creation.**

## Metadata
- **Complexity**: Medium (5-6 files, follows Phase A's just-established patterns closely,
  no new architectural concept beyond what Phase A already introduced)
- **Source**: Design approved via `superpowers:brainstorming` in this session — the
  original token-management brainstorm (multiple named tokens, hash-only/shown-once
  storage, full env-var replacement, step-up auth, no expiry) revised mid-conversation to
  per-user ownership once real multi-user support was requested; this plan captures the
  final, approved shape.
- **PRD Phase**: Phase B of 2 (Phase A: user accounts & auth model, must merge first)
- **Estimated Files**: 6 (`shared/database.py`, `shared/auth.py`,
  `tests/test_auth_token.py`, `tests/test_auth_middleware.py`, a new
  `tests/test_api_tokens.py`, `README.md`)

---

## UX Design

### Before
```
┌──────────────────────────────┐
│  .env: VITALFORGE_API_TOKEN  │
│  One value. Set by hand,     │
│  requires a restart to       │
│  change. No UI. No way to    │
│  tell which client is using  │
│  it, or to revoke just one.  │
└──────────────────────────────┘
```

### After
```
┌──────────────────────────────────────┐
│  /auth/account (extends Phase A's    │
│  page with a new section)            │
│  API Tokens                          │
│    [+ New token]  Label: [Bascule]   │
│    -> shown ONCE: vf_ab12cd34...     │
│       "Copy it now, you won't see    │
│        it again."                    │
│    Bascule       created 2026-08-23  │
│      last used 3h ago      [Revoke]  │
│    Tasker        created 2026-08-01  │
│      never used            [Revoke]  │
│  (Revoke and creation both require   │
│   re-entering your password)         │
└──────────────────────────────────────┘
┌──────────────────────────────────────┐
│  /auth/admin/users (Phase A's admin  │
│  page, extended)                     │
│  All tokens (every user's), each     │
│  labeled with its owner, admin can   │
│  revoke any of them                  │
└──────────────────────────────────────┘
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| Bearer auth on `/api/*` | Compared against one static env var | Presented token is hashed (SHA-256) and looked up in `api_tokens`; on match, request proceeds *as the owning user* (their role applies to any role-gated route, same as a cookie session) | See Approach — this is the one real behavioral subtlety in this plan |
| `/auth/account` | Password change only (Phase A) | Password change + token list/create/revoke | Same page, new section |
| `/auth/admin/users` | User CRUD only (Phase A) | User CRUD + a view of every user's tokens, admin can revoke any | Same page, new section |
| Startup, `VITALFORGE_API_TOKEN` set + `api_tokens` empty | N/A | Migrated into one token row owned by the bootstrapped admin, labeled `"migrated-from-env"` | One-time, idempotent, mirrors Phase A's user-bootstrap exactly |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `.claude/PRPs/plans/user-accounts-auth-model.plan.md` | Tasks 1-5 | This plan's prerequisite — the `users` table shape, the async `get_current_user`/`require_auth`/`get_current_user_role` functions this plan calls directly, the `/auth/account` and `/auth/admin/users` page constants this plan extends rather than replaces |
| P0 | `shared/auth.py` (POST-Phase-A state) | `_bearer_token_valid` region | The function this plan replaces — read it as it exists *after* Phase A merges, not the pre-Phase-A version quoted here for reference: |
| P0 | (reference only — pre-Phase-A) `shared/auth.py:75-92` | — | ```python\ndef _bearer_token_valid(request: Request) -> bool:\n    if not _API_TOKEN:\n        return False\n    header = request.headers.get("authorization", "")\n    scheme, _, value = header.partition(" ")\n    if scheme.lower() != "bearer":\n        return False\n    value = value.strip()\n    if not value:\n        return False\n    return hmac.compare_digest(value.encode("utf-8"), _API_TOKEN.encode("utf-8"))\n``` — the exact header-parsing logic (scheme check, whitespace-stripping, empty-value guards) to preserve unchanged; only the final comparison step becomes a DB hash lookup |
| P1 | `tests/test_auth_token.py` | full file | Every existing bearer-auth test (`test_bearer_valid_token_accepted`, `test_bearer_wrong_token_rejected`, `test_bearer_empty_value_rejected`, `test_bearer_whitespace_only_value_rejected`, `test_bearer_scheme_case_insensitive`, `test_bearer_non_ascii_token_returns_false_not_typeerror`, `test_bearer_surrounding_whitespace_stripped`) needs an equivalent under the new DB-backed model — same edge cases, new backing store |
| P1 | `tests/test_dedup.py` | `seed_row` (lines 26-61) | The direct-SQL test-seeding helper pattern — this plan's tests need an equivalent `seed_token(user_id, label) -> (id, raw_token)` |

## External Documentation
No external research needed — SHA-256 (`hashlib.sha256`, stdlib) for the token hash, same
`secrets.token_urlsafe(32)` generation already used identically elsewhere in this repo
(README's own secret-generation instructions, and the sibling
`phase5-security-fixes.plan.md`). Deliberately *not* `hashlib.scrypt` here (that's Phase A's
choice for *passwords*, which are low-entropy and need slow, memory-hard hashing to resist
brute force) — a `token_urlsafe(32)` value is already ~256 bits of entropy, so a fast hash
is correct and necessary (every request needs a fast lookup; scrypt-ing an already-random
token on every API call would be pure overhead with no security benefit).

---

## Patterns to Mirror

### TABLE_CREATION_PATTERN
// SOURCE: shared/database.py (Phase A's `users` table, Task 1 of that plan) — same idiom,
one more `CREATE TABLE IF NOT EXISTS` in `init_db()`.

### GENERATE_AND_HASH_PATTERN
// SOURCE: README.md's existing secret-generation instructions +
`phase5-security-fixes.plan.md` Task 1's `_resolve_secret`
```python
import secrets
raw = secrets.token_urlsafe(32)
```
New here: `token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()` — store the hash,
return `raw` to the caller exactly once (the route response), never persist it.

### STEP_UP_AUTH_PATTERN
// SOURCE: `user-accounts-auth-model.plan.md` Task 5, `change_own_password`
```python
async def change_own_password(request: Request):
    user = await require_auth(request)
    body = await request.json()
    current = body.get("current_password", "")
    ...
    if not await check_credentials(user, current):
        raise HTTPException(status_code=401, detail="Current password incorrect")
```
Token create/revoke reuse this exact shape: pull `current_password` from the request body,
verify via `check_credentials` (Phase A) before doing anything else, 401 on mismatch.

### ADMIN_GUARD_PATTERN
// SOURCE: `user-accounts-auth-model.plan.md` Task 5, `admin_delete_user`
```python
user = await require_auth(request)
if await get_current_user_role(user) != "admin":
    raise HTTPException(status_code=403, detail="Admin only")
```
Reused verbatim for the admin-sees-all-tokens view; token *ownership* checks (a non-admin
can only see/revoke their own) are additional, layered on top — see Task 3.

### CONSTANT_TIME_COMPARISON — NOT NEEDED HERE, AND WHY
Unlike password/secret comparison, token verification here is a DB *lookup by hash*, not a
value-vs-value comparison — there's nothing to time-attack (an attacker can't observe
whether a guessed token's hash "almost matches" a stored row; either the exact SHA-256
digest is present as a table row or it isn't, and the lookup is a standard indexed
`WHERE token_hash = ?`). `hmac.compare_digest` is unnecessary and not used in Task 2.

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `shared/database.py` | UPDATE | New `api_tokens` table |
| `shared/auth.py` | UPDATE | Remove `_API_TOKEN` env-var reading; rewrite `_bearer_token_valid` as an async, DB-backed, user-resolving function; add token CRUD routes; extend Phase A's `ACCOUNT_PAGE_HTML`/`ADMIN_USERS_PAGE_HTML`; add `bootstrap_migrated_token` |
| `tests/test_auth_token.py` | UPDATE | Port every existing bearer-auth edge case to the new model (see Mandatory Reading P1) |
| `tests/test_auth_middleware.py` | UPDATE | A bearer-authenticated request now resolves to a real username/role — extend the existing role-access matrix (Phase A, Task 6) to include a token-authenticated case per role |
| `tests/test_api_tokens.py` | CREATE | Token CRUD, per-user isolation, step-up auth enforcement, shown-once semantics, migration idempotency |
| `README.md` | UPDATE | Remove `VITALFORGE_API_TOKEN` from "set this to enable bearer auth" framing (env var still read once, migration-only); document `/auth/account`'s token section |

## NOT Building
- **Token expiry** — explicitly rejected in the original design (no-expiry-only, matching
  today's behavior).
- **Scoped/limited-permission tokens** (e.g. "read-only" or "weight-service-only" tokens) —
  not discussed, not requested; every token authenticates as its owning user with that
  user's full role, same as a cookie session. Out of scope.
- **Rate limiting per token** — not requested, unrelated to this plan's purpose.
- **Changing `.claude/PRPs/plans/user-accounts-auth-model.plan.md`'s scope retroactively**
  — this plan only adds to what Phase A ships, never modifies Phase A's own tasks.

---

## Approach

**Bearer tokens resolve to a username, not a boolean.** Today, `get_current_user` returns
the placeholder string `"api-token"` on any valid bearer match — no notion of *which*
client, and (before Phase A) no notion of roles at all. Post-Phase-A, `get_current_user`
already needs a real username for role lookups to mean anything for cookie sessions; this
plan makes bearer auth consistent with that by resolving a valid token to its **owning
user's actual username**. A request authenticated via Bascule's token, for example, is
authorized exactly as if that Bascule-owning user had logged in with a cookie — including
being blocked from admin-only routes if that user's role is `user`, not `admin`. This is a
deliberate, meaningful improvement over today (where a leaked bearer token has no role
concept to constrain it at all) and requires no new authorization logic — it reuses Phase
A's `get_current_user_role` unchanged.

**Alternative considered and rejected: keep bearer auth "roleless" (always full access,
independent of an owning user's role).** Rejected — it would mean a `user`-role person's
token grants *more* access than their own cookie session does, which is backwards and a
real privilege-escalation surface (create a token under a locked-down account, use it to
reach admin routes the account itself can't reach via login). Resolving to the owner's live
role closes this by construction.

**`last_used_at` updates are best-effort, never block or fail a request.** Wrapped in its
own `try/except`, logged (not raised) on failure — token verification succeeding is what
matters for the request; a failed timestamp update is telemetry, not correctness.

---

## Step-by-Step Tasks

### Task 1: `api_tokens` table
- **ACTION**: Add to `shared/database.py`, after Phase A's `users` table.
- **IMPLEMENT**:
  ```python
  await db.execute("""
      CREATE TABLE IF NOT EXISTS api_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id),
          label TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          last_used_at TEXT
      )
  """)
  ```
- **MIRROR**: TABLE_CREATION_PATTERN above.
- **IMPORTS**: None new in `shared/database.py`.
- **GOTCHA**: SQLite doesn't enforce foreign keys by default (`PRAGMA foreign_keys` is off
  unless explicitly turned on, and this repo doesn't turn it on anywhere — confirmed via
  `shared/database.py:56`'s `get_db()`, which sets `PRAGMA journal_mode=WAL` but no
  `PRAGMA foreign_keys=ON`). The `REFERENCES users(id)` is documentation of intent, not an
  enforced constraint — deleting a user (Phase A's `admin_delete_user`) will silently leave
  that user's token rows orphaned unless Task 4 explicitly deletes them first. Do not rely
  on `ON DELETE CASCADE` working; it won't fire.
- **VALIDATE**: `pytest -q tests/test_migration.py -v`

### Task 2: DB-backed bearer resolution
- **ACTION**: Replace `_bearer_token_valid` in `shared/auth.py` with an async,
  user-resolving version.
- **IMPLEMENT**:
  ```python
  import hashlib  # already added by Phase A Task 2 if implemented first; otherwise add here

  async def _resolve_bearer_token(request: Request) -> str | None:
      """Same header-parsing rules as before (Mandatory Reading P0's
      pre-Phase-B reference), but resolves to the token's OWNING
      username instead of a bare boolean -- so bearer-authenticated
      requests carry a real role via get_current_user_role, same as a
      cookie session."""
      header = request.headers.get("authorization", "")
      scheme, _, value = header.partition(" ")
      if scheme.lower() != "bearer":
          return None
      value = value.strip()
      if not value:
          return None
      token_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
      db = await get_db()
      try:
          row = await (
              await db.execute(
                  "SELECT api_tokens.id, users.username FROM api_tokens "
                  "JOIN users ON users.id = api_tokens.user_id "
                  "WHERE api_tokens.token_hash = ?",
                  (token_hash,),
              )
          ).fetchone()
          if row is None:
              return None
          try:
              await db.execute(
                  "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                  (datetime.now(timezone.utc).isoformat(), row["id"]),
              )
              await db.commit()
          except Exception as e:
              logger.warning("Failed to update last_used_at for token %s: %s", row["id"], e)
          return row["username"]
      finally:
          await db.close()
  ```
  Update `get_current_user` (Phase A, already async) to call this instead of the old sync
  `_bearer_token_valid`:
  ```python
  resolved = await _resolve_bearer_token(request)
  if resolved is not None:
      return resolved
  ```
  replacing the old:
  ```python
  if _bearer_token_valid(request):
      return "api-token"
  ```
  Remove `_API_TOKEN = os.environ.get("VITALFORGE_API_TOKEN", "").strip()`
  (`shared/auth.py:17`) as an ongoing-auth global — the env var is still read, but only
  inside `bootstrap_migrated_token` (Task 4), once, at startup.
- **MIRROR**: GENERATE_AND_HASH_PATTERN above for the hashing call; the
  `get_db()`/`try/finally` shape used throughout Phase A's Task 3.
- **IMPORTS**: `hashlib` (already present if Phase A's Task 2 landed first, since Phase A
  also needs it for password hashing — check before adding a duplicate import).
- **GOTCHA**: Preserve every existing edge-case behavior from the old `_bearer_token_valid`
  exactly: scheme-case-insensitivity (`.lower()`), whitespace-stripping on the token value,
  empty-value rejection, and — critically — non-ASCII token handling. The old function's
  docstring (`shared/auth.py:76-82`, pre-Phase-B) explicitly notes it compares *bytes*, not
  *str*, specifically so a non-ASCII presented token returns `False` instead of raising
  `TypeError`. `value.encode("utf-8")` before hashing preserves this — `hashlib.sha256`
  requires bytes input anyway, so this isn't optional, but call it out: do not accidentally
  hash the `str` and let Python's implicit encoding raise on an edge case a dedicated test
  (`test_bearer_non_ascii_token_returns_false_not_typeerror`) already pins.
- **VALIDATE**: `pytest -q tests/test_auth_token.py -v`

### Task 3: Token CRUD routes
- **ACTION**: Add routes to `add_auth_routes(app)` in `shared/auth.py`.
- **IMPLEMENT**:
  ```python
  @app.get("/auth/tokens")
  async def list_own_tokens(request: Request):
      user = await require_auth(request)
      db = await get_db()
      try:
          user_row = await (await db.execute("SELECT id FROM users WHERE username = ?", (user,))).fetchone()
          rows = await (
              await db.execute(
                  "SELECT id, label, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY created_at",
                  (user_row["id"],),
              )
          ).fetchall()
      finally:
          await db.close()
      return [dict(row) for row in rows]

  @app.post("/auth/tokens")
  async def create_own_token(request: Request):
      user = await require_auth(request)
      body = await request.json()
      current_password = body.get("current_password", "")
      label = body.get("label", "").strip()
      if not await check_credentials(user, current_password):
          raise HTTPException(status_code=401, detail="Current password incorrect")
      if not label:
          raise HTTPException(status_code=422, detail="Label required")
      raw = secrets.token_urlsafe(32)
      token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
      db = await get_db()
      try:
          user_row = await (await db.execute("SELECT id FROM users WHERE username = ?", (user,))).fetchone()
          await db.execute(
              "INSERT INTO api_tokens (user_id, label, token_hash, created_at) VALUES (?, ?, ?, ?)",
              (user_row["id"], label, token_hash, datetime.now(timezone.utc).isoformat()),
          )
          await db.commit()
      finally:
          await db.close()
      return {"token": raw, "label": label}

  @app.delete("/auth/tokens/{token_id}")
  async def revoke_token(request: Request, token_id: int):
      user = await require_auth(request)
      body = await request.json()
      if not await check_credentials(user, body.get("current_password", "")):
          raise HTTPException(status_code=401, detail="Current password incorrect")
      db = await get_db()
      try:
          row = await (
              await db.execute(
                  "SELECT api_tokens.id, users.username AS owner FROM api_tokens "
                  "JOIN users ON users.id = api_tokens.user_id WHERE api_tokens.id = ?",
                  (token_id,),
              )
          ).fetchone()
          if row is None:
              raise HTTPException(status_code=404, detail="Token not found")
          if row["owner"] != user and await get_current_user_role(user) != "admin":
              raise HTTPException(status_code=403, detail="Not your token")
          await db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
          await db.commit()
      finally:
          await db.close()
      return {"success": True}

  @app.get("/auth/admin/tokens")
  async def admin_list_all_tokens(request: Request):
      user = await require_auth(request)
      if await get_current_user_role(user) != "admin":
          raise HTTPException(status_code=403, detail="Admin only")
      db = await get_db()
      try:
          rows = await (
              await db.execute(
                  "SELECT api_tokens.id, api_tokens.label, api_tokens.created_at, "
                  "api_tokens.last_used_at, users.username AS owner FROM api_tokens "
                  "JOIN users ON users.id = api_tokens.user_id ORDER BY users.username, api_tokens.created_at"
              )
          ).fetchall()
      finally:
          await db.close()
      return [dict(row) for row in rows]
  ```
- **MIRROR**: STEP_UP_AUTH_PATTERN and ADMIN_GUARD_PATTERN above.
- **IMPORTS**: `import secrets` in `shared/auth.py` (new to this file — Phase A doesn't
  need it; check it's not already added before duplicating).
- **GOTCHA**: `revoke_token`'s ownership check (`row["owner"] != user and ... != "admin"`)
  is the one place in this plan where "own it OR be admin" logic lives — get the boolean
  direction right: block (403) only when BOTH "not the owner" AND "not an admin" are true.
  A DELETE request body carrying JSON (`current_password`) has **no existing precedent in
  this codebase** — checked: `vitalforge-weight/app.py`'s `DELETE /api/weight/{id}`
  (`vitalforge-weight/app.py:449-460`) takes no body at all, just the path parameter. FastAPI
  supports a JSON body on DELETE without issue, and this is a first-party UI-driven `fetch()`
  call, not a public contract — but this is a genuinely new pattern for this repo, not a
  mirrored one. If that asymmetry is undesirable, the alternative is a separate
  `POST /auth/tokens/{token_id}/revoke` route instead of a body-bearing DELETE — equally
  valid, purely a naming/HTTP-verb preference, not a functional difference. Pick one and be
  consistent.
- **VALIDATE**: `pytest -q tests/test_api_tokens.py -v`

### Task 4: Extend the Phase A pages, migration
- **ACTION**: Add a token section to `ACCOUNT_PAGE_HTML` and `ADMIN_USERS_PAGE_HTML`
  (Phase A); add `bootstrap_migrated_token`.
- **IMPLEMENT**: HTML/JS additions follow INLINE_HTML_PAGE_PATTERN (Phase A's Mandatory
  Reading) — a token list rendered from `fetch("/auth/tokens")`, a "New token" form
  (label input + submit, prompting for current password inline or via a second field in the
  same form — designer's choice, not prescribed further here since it's presentation, not
  logic), and a one-time reveal of the raw token value with a "copy" affordance and an
  explicit "you won't see this again" message. Migration function:
  ```python
  async def bootstrap_migrated_token():
      """Mirrors bootstrap_first_admin (Phase A): if api_tokens is empty
      and VITALFORGE_API_TOKEN is set, migrate it into a token owned by
      the first admin user, so nothing already configured with the old
      env-var token breaks on upgrade."""
      legacy_token = os.environ.get("VITALFORGE_API_TOKEN", "").strip()
      if not legacy_token:
          return
      db = await get_db()
      try:
          row = await (await db.execute("SELECT 1 FROM api_tokens LIMIT 1")).fetchone()
          if row is not None:
              return
          admin_row = await (
              await db.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
          ).fetchone()
          if admin_row is None:
              return  # no admin exists yet to own it -- nothing to migrate onto
          token_hash = hashlib.sha256(legacy_token.encode("utf-8")).hexdigest()
          await db.execute(
              "INSERT INTO api_tokens (user_id, label, token_hash, created_at) VALUES (?, 'migrated-from-env', ?, ?)",
              (admin_row["id"], token_hash, datetime.now(timezone.utc).isoformat()),
          )
          await db.commit()
          logger.warning(
              "Migrated VITALFORGE_API_TOKEN into a DB-backed token owned by the "
              "admin account -- the env var is no longer read for ongoing auth. "
              "Manage tokens from /auth/account or /auth/admin/users."
          )
      finally:
          await db.close()
  ```
  Call this from `shared/database.py`'s `init_db()`, right after Phase A's
  `bootstrap_first_admin()` call (same local-import placement, same reasoning about
  circular imports).
- **MIRROR**: STARTUP_WARNING_PATTERN (Phase A); Phase A's `bootstrap_first_admin` almost
  exactly — same idempotency check shape, same "only run if the seed condition is met"
  guard.
- **IMPORTS**: None new beyond what Task 2/3 already add.
- **GOTCHA**: `bootstrap_migrated_token` depends on an admin already existing — it must run
  *after* `bootstrap_first_admin` in `init_db()`, not before, or `admin_row` is always
  `None` on a fresh database and the migration silently no-ops even when it shouldn't.
  Order the two calls explicitly and comment why.
- **VALIDATE**: `pytest -q tests/test_api_tokens.py -k migration -v`

### Task 5: Tests
- **ACTION**: Port bearer-auth edge cases (Task 2), add CRUD/isolation/migration tests
  (Task 3/4).
- **IMPLEMENT**: In `tests/test_auth_token.py`, replace/adapt every existing
  `test_bearer_*` test to seed a real `api_tokens` row (via a new `seed_token` helper
  mirroring `test_dedup.py`'s `seed_row`) instead of monkeypatching `shared_auth._API_TOKEN`
  — that global no longer exists post-Task-2. In `tests/test_api_tokens.py`: token creation
  requires correct current password; a user cannot see or revoke another user's token
  (403); an admin can revoke anyone's token; `GET /auth/admin/tokens` lists every user's
  tokens with correct owner attribution; a bearer-authenticated request carrying a
  `user`-role token is blocked from `/auth/admin/users` exactly like a `user`-role cookie
  session (Phase A's Task 6 matrix, extended with a token-auth row); migration idempotency
  (`bootstrap_migrated_token` called twice inserts exactly one row); migration no-ops when
  no admin exists yet.
- **MIRROR**: `tests/test_dedup.py`'s `seed_row` pattern for `seed_token`; Phase A's
  TEST_STRUCTURE (parametrized matrix) for the role-access extension.
- **IMPORTS**: `hashlib`, `from shared.database import get_db` in the new test file.
- **GOTCHA**: `seed_token` must store a real SHA-256 hash of a known raw value (return both
  from the helper) so tests can present the *raw* value as a bearer header and assert
  against it — don't insert a fake/arbitrary hash string, or every "valid token accepted"
  test becomes untestable.
- **VALIDATE**: `pytest -q tests/test_auth_token.py tests/test_api_tokens.py tests/test_auth_middleware.py -v`

### Task 6: Docs
- **ACTION**: Update `README.md`.
- **IMPLEMENT**: Remove the "set `VITALFORGE_API_TOKEN` to enable bearer auth" framing from
  the Authentication section (`README.md:169` pre-Phase-B) — replace with: tokens are
  created per-user from `/auth/account`, each independently named and revocable;
  `VITALFORGE_API_TOKEN` is read once at first boot only, to migrate an already-configured
  token so upgrading doesn't break existing clients. Update the Tasker integration section
  (`README.md`'s "Auth with Tasker" subsection) to describe generating a token from
  `/auth/account` instead of setting an env var and restarting.
- **MIRROR**: `tests/test_docs_drift.py`'s existing philosophy — not adding a new drift
  guard is acceptable (see Phase A's Task 8 note), but if one already exists asserting
  `VITALFORGE_API_TOKEN` appears in the Tasker section (check `test_readme_tasker_section_uses_bearer`
  and its neighbors before editing — they currently assert `"Authorization: Bearer"` is
  present, which stays true; they do NOT currently assert the env-var *name* appears there,
  so this edit should not break that specific guard, but verify).
- **IMPORTS**: N/A
- **GOTCHA**: `tests/test_docs_drift.py:26-28`
  (`test_readme_tasker_section_no_longer_documents_cookie_copying`) asserts `"vf_session"`
  does NOT appear in the Tasker section — this remains true after this edit (tokens have
  nothing to do with cookies), but re-run the drift suite to be certain the rewrite doesn't
  accidentally introduce a `vf_session` mention while explaining the new flow.
- **VALIDATE**: `pytest -q tests/test_docs_drift.py -v`

---

## Testing Strategy

### Unit Tests
| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `_resolve_bearer_token`, valid token | correct raw bearer value | owning username | — |
| `_resolve_bearer_token`, wrong/unknown token | garbage value | `None` | mirrors old `test_bearer_wrong_token_rejected` |
| `_resolve_bearer_token`, non-ASCII value | e.g. `Bearer tökén` | `None`, no `TypeError` | direct port of `test_bearer_non_ascii_token_returns_false_not_typeerror` |
| Token create, correct current password | valid label + correct password | 200, raw value returned, DB row created | — |
| Token create, wrong current password | valid label + wrong password | 401, no row created | step-up auth enforcement |
| Token revoke, own token | token id owned by requester | 200, row deleted | — |
| Token revoke, someone else's token, non-admin | token id owned by a different user | 403 | isolation |
| Token revoke, someone else's token, admin | token id owned by a different user, requester is admin | 200, row deleted | admin-manages-all |
| `GET /auth/admin/tokens`, non-admin | — | 403 | — |
| Bearer-authenticated request to an admin-only route, token owner is `user` role | valid token, `user`-role owner | 403 | the core "bearer inherits owner's role" property this plan introduces |
| `bootstrap_migrated_token` idempotency | called twice | exactly one `api_tokens` row | — |
| `bootstrap_migrated_token`, no admin exists | `VITALFORGE_API_TOKEN` set, empty `users` table | no-op, no row created | ordering dependency on Task 4's GOTCHA |

### Edge Cases Checklist
- [x] Empty label on create — 422
- [x] Revoke a nonexistent token id — 404
- [x] Non-owner revoke attempt — 403 (covered above)
- [x] Non-ASCII bearer token value — covered (ported from existing test)
- [ ] Concurrent token creation — no new race beyond what `UNIQUE(token_hash)` already
      guards at the DB level (a hash collision is cryptographically negligible, not a
      realistic concurrency scenario to test)
- [ ] Maximum size input — N/A, no documented bound on `label` length, not introducing one

---

## Validation Commands

### Static Analysis
```bash
source .venv/bin/activate && ruff check .
```
EXPECT: All checks passed

### Unit Tests (affected files)
```bash
source .venv/bin/activate && pytest -q tests/test_auth_token.py tests/test_api_tokens.py tests/test_auth_middleware.py tests/test_migration.py tests/test_docs_drift.py -v
```
EXPECT: All new and existing tests pass, zero failures

### Full Test Suite
```bash
source .venv/bin/activate && pytest -q
```
EXPECT: No regressions — in particular, every existing test anywhere in the suite that
relied on bearer auth's old `"api-token"` placeholder return value (grep for
`"api-token"` across `tests/` before implementing — if any test asserts that literal
string, it needs updating to expect a real username instead)

### Playwright (separate invocation, per CLAUDE.md)
```bash
source .venv/bin/activate && pytest -q -m playwright
```
EXPECT: 3 passed — unaffected (no template changes to either service's own `index.html`,
only to `shared/auth.py`'s inline pages)

### Manual Validation
- [ ] `docker compose up --build` with a pre-existing `VITALFORGE_API_TOKEN` set — confirm
      it migrates into a token on first boot, and a client already using that raw value
      (e.g. a saved Tasker HTTP action) keeps working without any change on the client side
- [ ] From `/auth/account`, create a new token, confirm the raw value is shown exactly once
      and a page refresh no longer displays it
- [ ] Use the new token's raw value in a `curl -H "Authorization: Bearer ..."` request to a
      protected endpoint — confirm it authenticates as the owning user
- [ ] Revoke that token from the UI, confirm the same `curl` request now gets 401

---

## Acceptance Criteria
- [ ] All 6 tasks completed
- [ ] All validation commands pass
- [ ] New/ported tests written and passing (estimate 15-18 across the three affected/new
      test files)
- [ ] No lint errors
- [ ] Matches UX design (token section added to Phase A's existing pages, not a new
      top-level page)

## Completion Checklist
- [ ] Code follows discovered patterns (GENERATE_AND_HASH_PATTERN, STEP_UP_AUTH_PATTERN,
      ADMIN_GUARD_PATTERN)
- [ ] Error handling matches codebase style (401 for bad step-up password, 403 for
      not-your-token/not-admin, 404 for missing token, 422 for empty label)
- [ ] Logging follows codebase conventions — raw token values never logged anywhere,
      including in the `last_used_at` update's failure-path warning (log the token *id*,
      never the hash or raw value)
- [ ] Tests follow test patterns (`seed_token` helper mirroring `seed_row`, parametrized
      role-access matrix extended rather than duplicated)
- [ ] No hardcoded values
- [ ] Documentation updated (README's Authentication and Tasker sections)
- [ ] No unnecessary scope additions (no expiry, no scoped permissions, no rate limiting —
      see NOT Building)
- [ ] Self-contained — no questions needed during implementation (beyond confirming Phase A
      has actually merged first)

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Some existing test elsewhere in the suite asserts the old `get_current_user` bearer-auth return value `"api-token"` literally | Medium — this string is a plausible thing to have been asserted on in passing | Low — a straightforward test update, not a design problem | Full-suite validation command explicitly calls out grepping for `"api-token"` before implementing, not just running the affected-file subset |
| `PRAGMA foreign_keys` is off (Task 1's GOTCHA) — deleting a user via Phase A's `admin_delete_user` leaves that user's tokens orphaned in `api_tokens` | Certain, if not handled | Medium — orphaned rows are inert (no user to authenticate as), not a security hole, but is DB clutter and a confusing admin-tokens-list entry ("owner" join would return no rows for an orphan, silently hiding it from `GET /auth/admin/tokens` rather than erroring) | Recommend (not mandating a specific fix, since it touches Phase A's already-approved `admin_delete_user`) that Phase A's delete route also `DELETE FROM api_tokens WHERE user_id = ?` in the same transaction when implementing this plan — flag this to whoever implements Phase A's Task 5 if Phase B is already known about at that point; otherwise add it here as an explicit follow-up task during Phase B implementation |
| Step-up auth on `DELETE /auth/tokens/{id}` requires a JSON body on a DELETE request, which is valid HTTP but less common — some HTTP clients/proxies handle it inconsistently | Low | Low — this is a first-party UI-driven action (the settings page's own `fetch()` call controls the exact request shape), not a public API contract other systems depend on | Task 3's GOTCHA already flags checking `DELETE /api/weight/{id}`'s existing precedent in this codebase before implementing, to stay consistent either way |

## Notes
- Depends entirely on Phase A (`user-accounts-auth-model.plan.md`) merging first — do not
  attempt to implement this plan against a `main` that doesn't yet have the `users` table,
  async `get_current_user`/`require_auth`/`get_current_user_role`, or the
  `/auth/account`/`/auth/admin/users` pages this plan extends.
- Source: `superpowers:brainstorming` design approved in this session, same two sections as
  Phase A (the token-ownership fork was resolved as part of "Section 1 — Data model & auth
  flow"'s approval).
