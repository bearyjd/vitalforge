# Plan: User accounts & auth model (Phase A of settings-menu project)

## Summary
Replaces VitalForge's single shared credential pair (`VITALFORGE_USER`/`VITALFORGE_PASS`)
with a real, DB-backed multi-user model: a `users` table, properly hashed passwords
(`hashlib.scrypt`, stdlib — today's code doesn't hash at all), two roles (`admin`/`user`),
a self-service account page, and an admin user-management page. This is Phase A of a
two-phase project (Phase B, planned separately, adds per-user API tokens on top of the
`users` table this phase creates). Approved via `superpowers:brainstorming` earlier this
session — this plan is the direct output of that approved design, not a new design pass.

## User Story
As the operator of a household VitalForge deployment,
I want each person to have their own login, with one admin who can create/manage accounts
and every user able to manage their own password,
So that VitalForge stops requiring one shared password for the whole household, and a
compromised or departing user's access can be individually revoked instead of forcing
everyone to share a new password.

## Problem → Solution
**Today**: `shared/auth.py:15-16` reads `VITALFORGE_USER`/`VITALFORGE_PASS` once at import
time; `check_credentials` (`shared/auth.py:69-72`) does a plaintext `hmac.compare_digest`
against those two env-var globals — no hashing, no per-user identity, one password for
every person who uses the app. → **A `users` table with scrypt-hashed passwords and a
`role` column; `check_credentials` becomes an async DB lookup; a self-service `/auth/account`
page and an admin-only `/auth/admin/users` page, both new inline-HTML routes added via the
same `add_auth_routes(app)` mechanism that already serves `/auth/login`.**

## Metadata
- **Complexity**: Large (8-10 files, several hundred lines, one architectural change: sync
  auth checks become async/DB-backed)
- **Source**: Design approved via `superpowers:brainstorming` in this session (no PRD file
  — free-form design captured directly in this plan's Approach section)
- **PRD Phase**: Phase A of 2 (Phase B: per-user API tokens, separate plan, depends on this
  one merging first)
- **Estimated Files**: 9 (`shared/database.py`, `shared/auth.py`, `vitalforge-weight/app.py`,
  `vitalforge-dashboard/app.py`, `tests/test_auth_token.py`, `tests/test_auth_middleware.py`,
  a new `tests/test_user_management.py`, `tests/conftest.py`, `README.md`)

---

## UX Design

### Before
```
┌──────────────────────────────┐
│  /auth/login                 │
│  [username] [password]       │
│  -> checked against ONE      │
│     hardcoded VITALFORGE_*   │
│     pair, same for everyone  │
│  No account page. No user    │
│  management UI at all.       │
└──────────────────────────────┘
```

### After
```
┌──────────────────────────────┐  ┌──────────────────────────────┐
│  /auth/login (same layout)   │  │  /auth/account (any user)    │
│  [username] [password]       │  │  Change my password           │
│  -> checked against the      │  │  (requires current password)  │
│     users table, per-person  │  └──────────────────────────────┘
└──────────────────────────────┘  ┌──────────────────────────────┐
                                   │  /auth/admin/users (admin)   │
                                   │  List all users               │
                                   │  + Create user (name/pw/role) │
                                   │  Edit role / reset password   │
                                   │  Delete user (blocked if last │
                                   │  remaining admin)             │
                                   └──────────────────────────────┘
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| `/auth/login` | Checks one hardcoded username/password | Checks `users` table, scrypt-verified | Same HTML form, same fields — only the backend check changes |
| Startup, empty `users` table | N/A (concept doesn't exist) | If `VITALFORGE_USER`/`VITALFORGE_PASS` are set, auto-seed one admin from them; otherwise table stays empty (auth stays open, matching today's documented dev behavior) | One-time, idempotent (checked via "table empty," not a version flag) |
| Session cookie | Signed `{"user": username}`; role has no meaning today | Signed `{"user": username}` (unchanged shape); role is **looked up fresh from the DB on every request**, never trusted from the cookie | So demoting/deleting a user takes effect on their very next request, not after a 30-day cookie expiry |
| Nothing (new) | — | `/auth/account`: change own password (current password required) | New |
| Nothing (new) | — | `/auth/admin/users`: full CRUD on users, admin-only | New |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `shared/auth.py` | 1-93 | Everything being replaced or made async: `_SECRET`/`_USER`/`_PASS` globals, `get_current_user`, `require_auth`, `check_credentials`, `_bearer_token_valid` |
| P0 | `shared/auth.py` | 95-223 | `LOGIN_PAGE_HTML` (the exact dark-theme inline-HTML/CSS/vanilla-JS style every new page must match) and `add_auth_routes` (where new routes get added, and the middleware that calls `get_current_user`) |
| P0 | `shared/database.py` | 1-70 | `init_db()`'s exact `CREATE TABLE IF NOT EXISTS` pattern, `_add_columns`'s additive-migration idiom (not needed for brand-new tables, but the file's conventions — no `DEFAULT` on nullable columns, comments explaining why — should carry over), `get_db()`'s connection/WAL setup |
| P1 | `tests/test_auth_matrix.py` | full file | The existing behavior-matrix test style (`_PASS`/`_API_TOKEN` combinations) — new role-based tests should read like siblings of these, not a different style |
| P1 | `tests/test_auth_middleware.py` | 1-50 | `_build_matrix_app()`/`matrix_client`/`configured_auth` fixtures — reused for the new account/admin route tests |
| P1 | `tests/conftest.py` | 92-107, 160-166 | `tmp_db_path`/`initialized_db` fixtures — the new `users` table needs a seeding fixture in the same style |
| P2 | `vitalforge-weight/app.py` | 38-41 | `add_auth_routes(app)` call site — confirms both services wire this identically, so new routes appear on both automatically |
| P2 | `CLAUDE.md` | "Human-judgment chokepoints" section | The existing note on `shared/` being a blast-radius module and the auth-disabled-when-`VITALFORGE_PASS`-empty behavior — both get updated by this plan (see NOT Building and Approach) |

## External Documentation

```
KEY_INSIGHT: hashlib.scrypt(password: bytes, *, salt: bytes, n: int, r: int, p: int,
  maxmem: int = 0, dklen: int = 64) -> bytes -- stdlib since Python 3.6, requires OpenSSL
  1.1+ built with scrypt support (already true for any environment where `hashlib` itself
  works normally; no new dependency, no pip install).
APPLIES_TO: Task 2 (password hashing helpers)
GOTCHA: `n` must be a power of 2. OWASP's current minimum recommendation for
  interactive/low-throughput logins on modest hardware is n=2**14 (16384), r=8, p=1
  (~16 MB memory, well under 100ms on typical hardware) -- appropriate here since this is a
  personal app with infrequent logins, not a high-throughput auth service. Store the salt
  alongside the hash (there's no separate salt column -- see Task 2's format) since scrypt
  needs the exact same salt to re-derive and compare on login.
```

No web research needed beyond confirming the stdlib API shape above — no new third-party
dependency, no version-specific gotchas beyond the power-of-2 `n` requirement.

---

## Patterns to Mirror

### TABLE_CREATION_PATTERN
// SOURCE: shared/database.py:64-79 (weight_log), 147-157 (weight_history)
```python
await db.execute("""
    CREATE TABLE IF NOT EXISTS weight_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        weight_lbs REAL NOT NULL,
        ...
    )
""")
```
Plain `CREATE TABLE IF NOT EXISTS` inside `init_db()`, before the function's single trailing
`await db.commit()`. No separate migration helper needed for a brand-new table (the
`_add_columns` machinery is only for adding columns to a table that might already exist
without them) — `users` and `api_tokens` (Phase B) are net-new tables, so a plain
`CREATE TABLE IF NOT EXISTS` is the correct and sufficient pattern.

### INLINE_HTML_PAGE_PATTERN
// SOURCE: shared/auth.py:95-172 (LOGIN_PAGE_HTML), 178-182 (login_page route)
```python
LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    ...
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, ...; background: #1a1a2e; color: #e0e0e0; ... }
        .login-box { background: #16213e; border-radius: 12px; padding: 2rem; width: 320px; }
        input { ...; background: #1a1a2e; color: #e0e0e0; ... }
        button { ...; background: #5c6bc0; ...; }
        button:hover { background: #7c4dff; }
        .error { color: #ef5350; ...; }
    </style>
</head>
<body>
    <div class="login-box">
        ...
        <script>
            async function doLogin(e) {
                e.preventDefault();
                const res = await fetch("/auth/login", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({...})
                });
                if (res.ok) { window.location.href = "/"; }
                else { document.getElementById("error").textContent = "Invalid credentials"; }
                return false;
            }
        </script>
    </div>
</body>
</html>"""


@app.get("/auth/login")
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(LOGIN_PAGE_HTML)
```
Every new page (`ACCOUNT_PAGE_HTML`, `ADMIN_USERS_PAGE_HTML`) is a Python string constant
in this exact style: same color palette (`#1a1a2e`/`#16213e`/`#5c6bc0`/`#7c4dff`/`#ef5350`),
same `fetch()`-based form submission pattern (no page reload, JSON body, JSON response),
same error-message `<div>` pattern. Served via `HTMLResponse(...)`, registered as a route
inside `add_auth_routes(app)` exactly like `login_page`.

### AUTH_CHECK_PATTERN (being changed from sync to async)
// SOURCE: shared/auth.py:51-66
```python
def get_current_user(request: Request) -> str | None:
    if not _is_auth_configured():
        return "anonymous"
    if _bearer_token_valid(request):
        return "api-token"
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    return validate_session(cookie)


def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
```
Both become `async def`, both keep their exact signatures otherwise (still take `Request`,
still return `str | None` / raise `HTTPException`). `_is_auth_configured()` changes from
`bool(_PASS)` to an async DB check (Task 3). The two existing call sites
(`shared/auth.py:180`, `shared/auth.py:212`) are both already inside `async def` functions
— add `await` at both.

### CONSTANT_TIME_COMPARISON_PATTERN
// SOURCE: shared/auth.py:69-72, 90-92
```python
def check_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username.encode("utf-8"), _USER.encode("utf-8")) and hmac.compare_digest(
        password.encode("utf-8"), _PASS.encode("utf-8")
    )
```
The new scrypt-verification helper (Task 2) uses the same `hmac.compare_digest` idiom to
compare the freshly-derived hash against the stored one — never `==` on hash bytes, for the
same timing-attack reason this file already documents elsewhere (`_bearer_token_valid`'s
docstring, `shared/auth.py:76-82`).

### TEST_STRUCTURE (behavior-matrix style)
// SOURCE: tests/test_auth_matrix.py (whole file — parametrized `test_behavior_matrix`)
New role-based access tests (Task 6) follow this file's parametrization style: one test
function, `@pytest.mark.parametrize` over the meaningful cells (role × route combinations),
`ids=` naming each cell, rather than one test function per combination.

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `shared/database.py` | UPDATE | New `users` table (`CREATE TABLE IF NOT EXISTS`), inside `init_db()` |
| `shared/auth.py` | UPDATE | Core of this plan — see Tasks 1-5 |
| `vitalforge-weight/app.py` | UPDATE (verify only) | No route changes expected — `add_auth_routes(app)` already wires in whatever `shared/auth.py` adds. Task 7 confirms no direct (non-middleware) call sites of `get_current_user`/`require_auth` exist here that need an `await` added (grep confirms none today) |
| `vitalforge-dashboard/app.py` | UPDATE (verify only) | Same as above |
| `tests/test_auth_token.py` | UPDATE | New tests for the scrypt hash/verify helpers |
| `tests/test_auth_middleware.py` | UPDATE | New tests: `/auth/account` and `/auth/admin/users` route access by role; async `get_current_user`/`require_auth` still behave identically for the existing bearer/cookie/anonymous cases (regression) |
| `tests/test_user_management.py` | CREATE | New: user creation, password change (self-service, requires current password), role edit, deletion, last-admin-deletion guard, migration/bootstrap idempotency |
| `tests/conftest.py` | UPDATE | New fixture(s) to seed a test admin/user pair (mirrors `tmp_db_path`/`initialized_db`'s style) |
| `README.md` | UPDATE | Env var table note that `VITALFORGE_USER`/`VITALFORGE_PASS` now only matter for the one-time bootstrap seed, not ongoing auth; new Authentication subsection describing `/auth/account` and `/auth/admin/users` |

## NOT Building
- **Per-user API tokens** — Phase B, a separate plan, depends on the `users` table this
  phase creates (`api_tokens.user_id REFERENCES users(id)`). Do not add the `api_tokens`
  table in this phase.
- **Self-registration / signup page** — explicitly rejected in design; admin creates every
  account.
- **More than two roles**, or granular per-feature permissions — explicitly rejected;
  `admin`/`user` only.
- **Password reset via email** — no email sending exists anywhere in this repo, and adding
  it is out of scope. Admin resets a user's password directly from `/auth/admin/users`
  instead.
- **Removing `VITALFORGE_USER`/`VITALFORGE_PASS` from `.env.example`/README entirely** —
  they're kept as the one-time bootstrap-seed mechanism (see Approach), not deleted.
- **Changing `VITALFORGE_SECRET` or the TLS/cookie behavior** — that's the separate,
  already-planned `.claude/PRPs/plans/phase5-security-fixes.plan.md`. If both plans are
  implemented in either order, they touch overlapping regions of `shared/auth.py` (module
  header, imports) — see Risks for the merge-order note.

---

## Approach

**Auth checks move from sync/env-var to async/DB-backed.** This is the one real
architectural change. `get_current_user`, `require_auth`, and `_is_auth_configured` all
become `async def`. This was scoped as small as possible: no new dependency-injection
pattern, no change to how `add_auth_routes`'s middleware calls them (just add `await`) — the
only ripple is that anything calling these functions must itself be `async`, and a grep
(Task 7) confirms every existing call site already is.

**Alternative considered and rejected: cache users/roles in memory, refresh periodically.**
Would avoid a DB hit per request, but reintroduces exactly the "stale privilege" problem the
approved design explicitly rejected (a cache refresh interval is just a shorter, configurable
version of "wait for the cookie to expire"). SQLite reads are fast and this is a low-traffic
personal app — a DB lookup per request is not a real performance concern here. Rejected as
premature optimization.

**Role storage: fresh DB lookup, never trusted from the signed cookie.** Confirmed in the
approved design. The signed cookie payload stays exactly `{"user": username, "t": ...}` —
unchanged shape, so `create_session_cookie`/`validate_session` need no changes at all. Only
`get_current_user` changes, to look up the *current* role after getting the username back
from the cookie.

**Master switch: `_is_auth_configured()` becomes "does the `users` table have any rows,"
not `bool(_PASS)`.** `VITALFORGE_PASS` (and `VITALFORGE_USER`) stop being read for
per-request auth decisions entirely after this ships — they're read exactly once, at
startup, purely to seed the first admin row if the table is empty (Task 4). This is a
deliberate behavior change from today's `_PASS`-is-the-switch model, confirmed in the
approved design (Section 1) — flagged again here because it's the kind of thing easy to
miss mid-implementation: **do not** keep `_PASS`/`_USER` as a persistent auth path
alongside the `users` table; they're bootstrap-only, single-use, one-directional.

**Password hash format: `f"{salt_hex}${derived_hash_hex}"` in one `TEXT` column**, not two
columns. Simpler schema, standard practice (bcrypt/similar itself embeds the salt in its
output string this same way), and there's exactly one place that ever parses it (the verify
helper), so the marginal clarity of a separate `salt` column isn't worth the extra column.

---

## Step-by-Step Tasks

### Task 1: `users` table
- **ACTION**: Add the table definition to `shared/database.py`.
- **IMPLEMENT**:
  ```python
  await db.execute("""
      CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
          created_at TEXT NOT NULL
      )
  """)
  ```
  Place it after the `weight_history` block and its `_add_columns` call
  (`shared/database.py:162`), before the `training_load` table — grouping is otherwise
  chronological-by-feature in this file, so this is a reasonable, low-conflict insertion
  point. Exact position doesn't matter functionally.
- **MIRROR**: TABLE_CREATION_PATTERN above.
- **IMPORTS**: None new in `shared/database.py`.
- **GOTCHA**: No `DEFAULT` on any column (matches this file's own stated convention at
  `shared/database.py:8-12` for *additive* columns — less critical for a brand-new table
  since there's no existing-row backfill concern, but keep the habit for consistency).
- **VALIDATE**: `pytest -q tests/test_migration.py -v` (confirms `init_db()` still runs
  clean and idempotent with the new table added)

### Task 2: Password hashing helpers
- **ACTION**: Add `_hash_password`/`_verify_password` to `shared/auth.py`.
- **IMPLEMENT**:
  ```python
  import hashlib  # new import, alongside hmac/logging/os/time

  _SCRYPT_N = 2**14
  _SCRYPT_R = 8
  _SCRYPT_P = 1


  def _hash_password(password: str) -> str:
      salt = os.urandom(16)
      derived = hashlib.scrypt(
          password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
      )
      return f"{salt.hex()}${derived.hex()}"


  def _verify_password(password: str, stored_hash: str) -> bool:
      try:
          salt_hex, derived_hex = stored_hash.split("$", 1)
          salt = bytes.fromhex(salt_hex)
      except ValueError:
          return False
      candidate = hashlib.scrypt(
          password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
      )
      return hmac.compare_digest(candidate.hex(), derived_hex)
  ```
- **MIRROR**: CONSTANT_TIME_COMPARISON_PATTERN above (the `hmac.compare_digest` call at the
  end).
- **IMPORTS**: `import hashlib` (stdlib, new to this file).
- **GOTCHA**: `hashlib.scrypt` raises `ValueError` for a malformed `n` (not a power of 2) —
  not a concern here since `_SCRYPT_N` is a fixed module constant, but if these parameters
  ever become configurable, validate before calling. Also: `stored_hash.split("$", 1)` must
  use `maxsplit=1` — a scrypt hex digest cannot itself contain `$`, but being explicit here
  avoids ever depending on that being coincidentally true.
- **VALIDATE**: `pytest -q tests/test_auth_token.py -k password -v`

### Task 3: Async `get_current_user`/`require_auth`/`_is_auth_configured`, DB-backed `check_credentials`
- **ACTION**: Rewrite the four functions in `shared/auth.py:35-72`.
- **IMPLEMENT**:
  ```python
  async def _is_auth_configured() -> bool:
      db = await get_db()
      try:
          row = await (await db.execute("SELECT 1 FROM users LIMIT 1")).fetchone()
          return row is not None
      finally:
          await db.close()


  async def get_current_user(request: Request) -> str | None:
      if not await _is_auth_configured():
          return "anonymous"
      if _bearer_token_valid(request):
          return "api-token"
      cookie = request.cookies.get(_COOKIE_NAME)
      if not cookie:
          return None
      username = validate_session(cookie)
      if username is None:
          return None
      db = await get_db()
      try:
          row = await (
              await db.execute("SELECT 1 FROM users WHERE username = ?", (username,))
          ).fetchone()
      finally:
          await db.close()
      return username if row is not None else None


  async def require_auth(request: Request) -> str:
      user = await get_current_user(request)
      if user is None:
          raise HTTPException(status_code=401, detail="Not authenticated")
      return user


  async def check_credentials(username: str, password: str) -> bool:
      db = await get_db()
      try:
          row = await (
              await db.execute(
                  "SELECT password_hash FROM users WHERE username = ?", (username,)
              )
          ).fetchone()
      finally:
          await db.close()
      if row is None:
          return False
      return _verify_password(password, row["password_hash"])


  async def get_current_user_role(username: str) -> str | None:
      """Separate from get_current_user (which only confirms a session's
      owner still exists) so route handlers that need the role for an
      authorization decision (e.g. /auth/admin/* -- admin-only) fetch it
      explicitly, rather than every request paying for a role lookup it
      doesn't need."""
      db = await get_db()
      try:
          row = await (
              await db.execute("SELECT role FROM users WHERE username = ?", (username,))
          ).fetchone()
      finally:
          await db.close()
      return row["role"] if row is not None else None
  ```
  Import `get_db` from `shared.database` at the top of `shared/auth.py` (new import — this
  file currently has zero dependency on `shared.database`, confirmed by
  `shared/auth.py:1-16`'s import block).
- **MIRROR**: AUTH_CHECK_PATTERN above for the async conversion shape;
  `shared/database.py`'s `get_db()`/`try...finally: await db.close()` pattern (used
  throughout `vitalforge-weight/app.py` and `vitalforge-dashboard/app.py` already) for every
  new DB access in this task.
- **IMPORTS**: `from shared.database import get_db` — new. **GOTCHA below explains why this
  is safe despite `shared/database.py` having no existing reverse dependency on
  `shared/auth.py`.**
- **GOTCHA**: This makes `shared/auth.py` depend on `shared/database.py`, a new
  cross-module dependency within `shared/`. Confirmed safe: `shared/database.py` imports
  nothing from `shared/auth.py` (checked — it only imports `os`, `pathlib`, `aiosqlite`), so
  this is a one-directional dependency, not a cycle. Also: `check_credentials`'s signature
  changes from sync to async — its two call sites are `shared/auth.py:189`
  (`login` route, already `async def`) and nowhere else (grepped: no external callers)
  — add `await`.
- **VALIDATE**: `pytest -q tests/test_auth_matrix.py tests/test_auth_middleware.py tests/test_auth_token.py -v` (the existing bearer/cookie/anonymous behavior-matrix tests are the regression suite for this task — they must all still pass with the functions now async, since `pytest-asyncio`'s `auto` mode already awaits async test functions transparently, and the fixtures already `await` these functions where they're called — confirm this, don't assume it, since this is the highest-risk task in the plan)

### Task 4: Startup bootstrap (seed first admin)
- **ACTION**: Add a bootstrap function to `shared/auth.py`, called from `init_db()` in
  `shared/database.py` (not from each service's own `lifespan` — keeps it in one place,
  runs exactly once per `init_db()` call, which both services already call at startup).
- **IMPLEMENT**: In `shared/auth.py`:
  ```python
  async def bootstrap_first_admin():
      """If no users exist yet, seed one admin from VITALFORGE_USER/
      VITALFORGE_PASS -- a zero-touch upgrade path so an existing
      deployment's login keeps working exactly as before, just backed by
      a real (hashed) user record instead of the env-var pair. Does
      nothing if any user already exists, or if VITALFORGE_PASS is empty
      (matches today's "empty VITALFORGE_PASS = auth disabled" dev
      convenience -- an empty users table IS that state now)."""
      if not _PASS:
          return
      db = await get_db()
      try:
          row = await (await db.execute("SELECT 1 FROM users LIMIT 1")).fetchone()
          if row is not None:
              return
          await db.execute(
              "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
              (_USER, _hash_password(_PASS), datetime.now(timezone.utc).isoformat()),
          )
          await db.commit()
          logger.warning(
              "Seeded admin user %r from VITALFORGE_USER/VITALFORGE_PASS -- "
              "these env vars are no longer read for ongoing auth after this, "
              "only for this one-time bootstrap. Manage the account from "
              "/auth/account or /auth/admin/users from now on.",
              _USER,
          )
      finally:
          await db.close()
  ```
  In `shared/database.py`'s `init_db()`, after the existing `await db.commit()` and before
  `finally: await db.close()`: call `from shared.auth import bootstrap_first_admin` (local
  import, to avoid a module-level circular import — `shared/auth.py` now imports
  `shared/database.py` per Task 3, so `shared/database.py` importing `shared/auth.py` at
  module level would cycle) then `await bootstrap_first_admin()`.
- **MIRROR**: STARTUP_WARNING_PATTERN from the sibling security-fixes plan
  (`.claude/PRPs/plans/phase5-security-fixes.plan.md`) — a startup-time function that warns
  via `logger.warning`, never raises.
- **IMPORTS**: `from datetime import datetime, timezone` in `shared/auth.py` (new — this
  file currently has no datetime usage).
- **GOTCHA**: The circular-import direction matters — `shared/auth.py` → `shared/database.py`
  is the Task 3 dependency; `shared/database.py` → `shared/auth.py` (for this task) must be
  a **local** import inside `init_db()`, not a top-of-file import, or the module graph
  cycles at import time. Also: this must run exactly once at each *process* startup
  (both services independently call `init_db()` in their own `lifespan`), but is idempotent
  by construction (checks `SELECT 1 FROM users LIMIT 1` first) — running it twice (e.g. both
  services starting near-simultaneously against the shared DB file) is safe: whichever
  commits first wins, the second sees a non-empty table and no-ops. No `BEGIN IMMEDIATE`
  needed here (unlike the dedup-insert path in `vitalforge-weight/app.py`) because a
  double-insert race would at worst create the row twice with a `UNIQUE` constraint
  violation on `username`, which surfaces as a startup exception (visible, not silent) —
  acceptable for a genuinely rare race on a one-time bootstrap path.
- **VALIDATE**: `pytest -q tests/test_user_management.py -k bootstrap -v`

### Task 5: `/auth/account` and `/auth/admin/users` routes
- **ACTION**: Add `ACCOUNT_PAGE_HTML`, `ADMIN_USERS_PAGE_HTML`, and their routes inside
  `add_auth_routes(app)` in `shared/auth.py`.
- **IMPLEMENT**: Two new inline-HTML page constants (INLINE_HTML_PAGE_PATTERN), plus routes:
  ```python
  @app.get("/auth/account")
  async def account_page(request: Request):
      user = await require_auth(request)
      return HTMLResponse(ACCOUNT_PAGE_HTML)

  @app.post("/auth/account/password")
  async def change_own_password(request: Request):
      user = await require_auth(request)
      body = await request.json()
      current = body.get("current_password", "")
      new = body.get("new_password", "")
      if not await check_credentials(user, current):
          raise HTTPException(status_code=401, detail="Current password incorrect")
      if not new:
          raise HTTPException(status_code=422, detail="New password required")
      db = await get_db()
      try:
          await db.execute(
              "UPDATE users SET password_hash = ? WHERE username = ?",
              (_hash_password(new), user),
          )
          await db.commit()
      finally:
          await db.close()
      return {"success": True}

  @app.get("/auth/admin/users")
  async def admin_users_page(request: Request):
      user = await require_auth(request)
      if await get_current_user_role(user) != "admin":
          raise HTTPException(status_code=403, detail="Admin only")
      return HTMLResponse(ADMIN_USERS_PAGE_HTML)

  @app.get("/auth/admin/users/list")
  async def admin_list_users(request: Request):
      user = await require_auth(request)
      if await get_current_user_role(user) != "admin":
          raise HTTPException(status_code=403, detail="Admin only")
      db = await get_db()
      try:
          rows = await (
              await db.execute("SELECT id, username, role, created_at FROM users ORDER BY username")
          ).fetchall()
      finally:
          await db.close()
      return [dict(row) for row in rows]

  @app.post("/auth/admin/users")
  async def admin_create_user(request: Request):
      user = await require_auth(request)
      if await get_current_user_role(user) != "admin":
          raise HTTPException(status_code=403, detail="Admin only")
      body = await request.json()
      username = body.get("username", "").strip()
      password = body.get("password", "")
      role = body.get("role", "user")
      if not username or not password or role not in ("admin", "user"):
          raise HTTPException(status_code=422, detail="username, password, and a valid role are required")
      db = await get_db()
      try:
          try:
              await db.execute(
                  "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                  (username, _hash_password(password), role, datetime.now(timezone.utc).isoformat()),
              )
              await db.commit()
          except aiosqlite.IntegrityError:
              raise HTTPException(status_code=409, detail="Username already exists")
      finally:
          await db.close()
      return {"success": True}

  @app.delete("/auth/admin/users/{user_id}")
  async def admin_delete_user(request: Request, user_id: int):
      user = await require_auth(request)
      if await get_current_user_role(user) != "admin":
          raise HTTPException(status_code=403, detail="Admin only")
      db = await get_db()
      try:
          target = await (
              await db.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
          ).fetchone()
          if target is None:
              raise HTTPException(status_code=404, detail="User not found")
          if target["role"] == "admin":
              admin_count = (
                  await (await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")).fetchone()
              )[0]
              if admin_count <= 1:
                  raise HTTPException(status_code=409, detail="Cannot delete the last remaining admin")
          await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
          await db.commit()
      finally:
          await db.close()
      return {"success": True}
  ```
  A `PATCH /auth/admin/users/{user_id}` (role change / password reset) follows the exact
  same shape as `admin_create_user`/`admin_delete_user` — write it symmetrically; omitted
  here only for length, not because it's optional. It needs the same last-admin guard as
  delete (demoting the last admin to `user` must be blocked identically to deleting them).
- **MIRROR**: INLINE_HTML_PAGE_PATTERN for the two new page constants (match
  `LOGIN_PAGE_HTML`'s palette/structure exactly — this is a design consistency requirement,
  not just a suggestion); every existing route in `add_auth_routes` for the
  `get_db()`/`try/finally` DB-access shape.
- **IMPORTS**: `import aiosqlite` in `shared/auth.py` (new — needed for
  `aiosqlite.IntegrityError` in the create-user duplicate-username case).
- **GOTCHA**: The last-admin guard must count *other* admins, not just check "is there more
  than one row" — re-read the `admin_delete_user` logic above carefully: it counts admins
  with `role = 'admin'` and blocks only when the target IS an admin AND the count is `<= 1`
  (i.e., deleting/demoting them would leave zero). A non-admin can always be deleted freely.
  Write the `PATCH` (role-change) version of this guard to match exactly, including the
  case where an admin demotes *themselves* to `user` — this must be blocked the same way as
  deletion.
- **VALIDATE**: `pytest -q tests/test_user_management.py -v`

### Task 6: Role-based access tests
- **ACTION**: Extend `tests/test_auth_middleware.py` (route access) and create
  `tests/test_user_management.py` (CRUD/business logic).
- **IMPLEMENT**: In `tests/test_auth_middleware.py`, mirror TEST_STRUCTURE — a parametrized
  matrix over (route, role, expected status): a `user`-role session hitting
  `/auth/admin/users` or `/auth/admin/users/list` gets 403; an `admin`-role session gets
  200; an unauthenticated request gets 401/redirect (existing behavior, regression-only).
  In `tests/test_user_management.py`: password change requires the correct current
  password (wrong current password → 401, password unchanged); admin can create/delete
  users; the last-admin guard blocks both deletion and self-demotion; `bootstrap_first_admin`
  is idempotent (call it twice against the same DB, assert exactly one user row exists) and
  is a no-op when `VITALFORGE_PASS` is empty (matches today's open-dev-mode convenience).
- **MIRROR**: TEST_STRUCTURE above; `seed_row`-style helper functions from
  `tests/test_dedup.py` for the pattern of a small local fixture-adjacent helper (here:
  `seed_user(username, password, role) -> int` inserting directly via SQL, mirroring how
  `test_dedup.py`'s `seed_row` bypasses the route layer to set up state precisely).
- **IMPORTS**: `from shared.database import get_db` in the new test file, matching every
  other test file's DB-assertion pattern.
- **GOTCHA**: Use the real `_hash_password` (Task 2) when seeding a test user directly via
  SQL, not a plaintext string — `check_credentials`/`_verify_password` will reject a
  malformed (non-`salt$hash`) stored value with the `except ValueError: return False`
  branch, which would make a "wrong password" test pass for the wrong reason (a hash-format
  error, not an actual password mismatch). Assert the *right* failure mode where it matters.
- **VALIDATE**: `pytest -q tests/test_user_management.py tests/test_auth_middleware.py -v`

### Task 7: Confirm no other call sites need updating
- **ACTION**: Verify (don't assume) that converting `get_current_user`/`require_auth`/
  `check_credentials`/`_is_auth_configured` to `async def` has no ripple outside
  `shared/auth.py`.
- **IMPLEMENT**: `grep -rn "get_current_user\|require_auth\|check_credentials\|_is_auth_configured" vitalforge-weight/ vitalforge-dashboard/` — confirmed during planning to return
  zero hits outside `shared/auth.py` itself (neither service calls these directly; all
  enforcement goes through the middleware, which lives inside `shared/auth.py`). Re-run
  this grep as part of implementation to catch any drift since this plan was written.
- **MIRROR**: N/A — verification step.
- **IMPORTS**: N/A
- **GOTCHA**: If this grep ever DOES return a hit (e.g. future code adds a direct call), that
  call site needs `await` added and its enclosing function needs to be `async def` — don't
  silently `asyncio.run()` a sync wrapper around it, which would create a nested-event-loop
  problem inside FastAPI's already-running loop.
- **VALIDATE**: The grep command above returns nothing to change beyond `shared/auth.py`.

### Task 8: Docs
- **ACTION**: Update `README.md`.
- **IMPLEMENT**: Env var table (~line 94-95): note `VITALFORGE_USER`/`VITALFORGE_PASS` are
  now bootstrap-only (read once, to seed the first admin if `users` is empty — not checked
  on every request anymore). New subsection under Authentication describing `/auth/account`
  (self-service password change) and `/auth/admin/users` (admin user management), and the
  two-role model.
- **MIRROR**: `tests/test_docs_drift.py`'s existing pattern — after this edit, a drift guard
  asserting the README mentions `/auth/account`/`/auth/admin/users` would be consistent with
  that file's existing philosophy, but is not required by this plan (see NOT Building's
  spirit — keep this plan's own diff focused; a follow-up drift guard is a cheap, separate
  addition if wanted later).
- **IMPORTS**: N/A
- **GOTCHA**: Don't remove `VITALFORGE_API_TOKEN` documentation here — that's Phase B's
  concern (per NOT Building), still fully valid and unaffected by this phase.
- **VALIDATE**: `pytest -q tests/test_docs_drift.py -v` (confirm no existing drift guard
  broken by the README edit)

---

## Testing Strategy

### Unit Tests
| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `_hash_password`/`_verify_password` round-trip | a password string | verify returns `True` for the right password, `False` for wrong | — |
| `_verify_password` malformed stored hash | `stored_hash="not-the-right-format"` | returns `False`, does not raise | edge case — a corrupted/legacy row shouldn't crash a login attempt |
| `check_credentials` against seeded user | correct/incorrect username+password combos | `True` only for the exact right pair | mirrors today's `test_check_credentials_*` tests in `test_auth_token.py`, now DB-backed |
| `get_current_user` for a deleted user's still-valid signed cookie | a cookie signed for a username no longer in `users` | returns `None` (not the stale username) | the core "live re-check" property this plan exists to guarantee |
| `get_current_user_role` for admin vs. user | seeded rows of each role | correct role string, `None` for unknown username | — |
| Admin route access | `user`-role session vs. `admin`-role session vs. anonymous | 403 / 200 / 401-or-redirect respectively | — |
| Last-admin deletion/demotion guard | delete or demote the sole admin | 409, user/role unchanged in DB | the most important safety property in this plan — must not be skippable |
| `bootstrap_first_admin` idempotency | call twice against the same DB | exactly one user row after both calls | prevents a duplicate-seed bug on service restart |
| `bootstrap_first_admin` with `VITALFORGE_PASS` empty | — | no user created, table stays empty | preserves today's documented open-dev-mode behavior |

### Edge Cases Checklist
- [x] Empty input (empty username/password on create) — 422
- [x] Duplicate username on create — 409, not a 500
- [x] Concurrent access — the bootstrap race (Task 4's GOTCHA) and the general "both
      services hit the same SQLite file" property already established elsewhere in this
      codebase; no new concurrency primitive needed for this plan's own routes since none of
      them have a dedup-style read-then-write race the way `POST /api/weight` does (user
      creation collides on `UNIQUE(username)` at the DB level, which is itself the correct
      concurrency guard)
- [x] Permission denied (non-admin hitting admin routes) — 403, covered by Task 6
- [ ] Maximum size input — N/A, no documented bound on username/password length; not
      introducing one here (matches existing `WeightIn`'s lack of a length bound on `source`,
      etc. — out of scope for this plan)
- [ ] Network failure — N/A, no external network calls in this plan

---

## Validation Commands

### Static Analysis
```bash
source .venv/bin/activate && ruff check .
```
EXPECT: All checks passed

### Unit Tests (affected files)
```bash
source .venv/bin/activate && pytest -q tests/test_auth_token.py tests/test_auth_middleware.py tests/test_auth_matrix.py tests/test_user_management.py tests/test_migration.py tests/test_docs_drift.py -v
```
EXPECT: All new and existing tests pass, zero failures

### Full Test Suite
```bash
source .venv/bin/activate && pytest -q
```
EXPECT: No regressions in any other test file — in particular, confirm nothing outside the
auth-related files above changed behavior (this task's async conversion is the highest-risk
part of this plan; a full-suite pass is the real confidence check, not just the affected-file
run above)

### Playwright (separate invocation, per CLAUDE.md)
```bash
source .venv/bin/activate && pytest -q -m playwright
```
EXPECT: 3 passed — confirm the smoke tests (which exercise real login flows via
`weight_live_server`/`dashboard_live_server`) still work with the new async auth path. If
these fixtures monkeypatch `authenticate`/`push_weight` but don't seed a `users` row, check
whether they rely on the *empty-table-means-open-access* fallback (Task 3's
`_is_auth_configured`) — they likely do, since `tests/conftest.py`'s fixtures never set
`VITALFORGE_PASS`, so no user ever gets seeded and auth stays open exactly as today.

### Manual Validation
- [ ] `docker compose up --build` with `VITALFORGE_USER`/`VITALFORGE_PASS` already set in
      `.env` — confirm login still works exactly as before on first boot (bootstrap fires,
      no visible behavior change)
- [ ] Log in as the seeded admin, visit `/auth/admin/users`, create a second user with role
      `user`
- [ ] Log in as that second user, confirm `/auth/admin/users` returns 403, confirm
      `/auth/account` lets them change their own password with the right current password
      and rejects the wrong one
- [ ] Attempt to delete the admin account while it's the only admin — confirm it's blocked

---

## Acceptance Criteria
- [ ] All 8 tasks completed
- [ ] All validation commands pass
- [ ] New tests written and passing (count TBD at implementation — estimate 15-20 across
      the three affected/new test files)
- [ ] No lint errors
- [ ] Matches UX design (login form unchanged in shape; two new pages match the existing
      dark-theme inline-HTML style exactly)

## Completion Checklist
- [ ] Code follows discovered patterns (TABLE_CREATION_PATTERN, INLINE_HTML_PAGE_PATTERN,
      AUTH_CHECK_PATTERN, CONSTANT_TIME_COMPARISON_PATTERN)
- [ ] Error handling matches codebase style (422 for bad input, 403 for wrong role, 409 for
      conflict/last-admin-guard, 404 for missing user — never a bare 500 for an expected
      condition)
- [ ] Logging follows codebase conventions (module `logger`, `_hash_password`'s output and
      raw passwords never logged anywhere — audit every new `logger.warning`/`logger.info`
      call for this before merging)
- [ ] Tests follow test patterns (parametrized matrix style, `seed_*` helper functions,
      direct SQL for state setup bypassing the route layer where that's the established
      idiom)
- [ ] No hardcoded values beyond the scrypt cost parameters (`_SCRYPT_N`/`_SCRYPT_R`/`_SCRYPT_P`,
      which are deliberately fixed constants, not configuration — a mid-flight cost-parameter
      change would need a migration story, out of scope here)
- [ ] Documentation updated (README)
- [ ] No unnecessary scope additions (no self-registration, no email, no >2 roles, no
      `api_tokens` table — see NOT Building)
- [ ] Self-contained — no questions needed during implementation

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Task 3 (sync→async conversion) breaks something Task 7's grep didn't catch, or a fixture in `tests/conftest.py` calls one of these functions synchronously | Low (grep confirmed zero external call sites during planning) | High if it happens — auth is the highest-blast-radius module in this repo | Task 7 is a mandatory re-verification step, not a one-time planning check; full test suite run (not just affected files) is a required validation command specifically to catch this |
| This plan and `phase5-security-fixes.plan.md` (VITALFORGE_SECRET/TLS cookie fix) both edit `shared/auth.py`'s top-of-file region and could conflict if implemented out of order or in parallel | Medium — both are live, unmerged plans as of this writing | Low — a normal merge conflict, not a semantic one (they touch different functions: secret resolution + cookie flag vs. auth-check functions) | Implement the security-fixes plan first (smaller, already fully scoped) if both are queued; otherwise resolve the textual conflict on merge — no design-level incompatibility between them |
| Scrypt cost parameters (`n=2**14`) chosen for "modest home-server hardware" without knowing JD's actual server specs | Low | Low — worst case is a login taking noticeably longer than instant, not a security or correctness issue | If login latency is a problem in practice, lowering `n` is a one-constant change with no migration needed (existing hashes remain verifiable regardless of what `_SCRYPT_N` is set to *now*, since the salt+params aren't stored per-row — **actually this IS a real gotcha**: if `_SCRYPT_N` changes after users exist, existing stored hashes were derived with the OLD `n` and will fail to verify against the new one, since `_verify_password` re-derives using the *current* module constant, not a value read from the stored hash. Note this explicitly: changing `_SCRYPT_N`/`_R`/`_P` after this ships invalidates every existing password and requires a forced reset for all users — do not change these constants casually post-launch |

## Notes
- This plan supersedes the earlier, narrower "global tokens, single shared credential"
  token-management brainstorm from earlier in this session — that design was revised mid-
  brainstorm once real multi-user support was requested, and its final, approved shape
  (per-user tokens) is captured in the separate Phase B plan, not here.
- Phase B (per-user API tokens) should not be started until this plan is merged — it
  depends on `users.id` existing as a real, populated foreign-key target.
- Source: `superpowers:brainstorming` design approved in this session (two presented
  sections, both explicitly approved by the user: "Data model & auth flow" and "UI/UX,
  migration, and safety guards").
