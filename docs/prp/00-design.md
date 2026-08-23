# 00 — Design: Track A (bearer token auth) + Track B (body-composition intake)

**Phase:** 0 (Design). **Status:** revised post-DA-review. All exit-gate criteria
pass; the five decisions in §6 are resolved (D1–D3 confirmed by JD 2026-08-22;
D4–D5 decided by JD on the review's two blocking findings).
**Scope:** design only. No implementation, no test code, no branches.

> **Revision note — 2026-08-22, post-DA review.** Revised to incorporate
> `docs/prp/02-validation.md` (Phase 2 devil's advocate; 13 objections, 2
> blocking). The DA re-ran several claims this document asserted as verified and
> **falsified two of them**; they are corrected here rather than defended.
> Sections materially changed: **§2.4** (logging policy narrowed + startup
> warning — F6), **§2.5** (exemption claim corrected — F7), **§3.1/§3.2** (the
> new weight bound returns 422, not 400 — F8), **§3.4** (FIT truncation — F10),
> **§3.5** (units unverified; column naming — F4, F11), **§3.7** (atomic dedup —
> F1; boundary and rationale — F12), **§4.1** (revocation asymmetry — F7),
> **§4.4/§4.5** (durability caveats — F2, F3), **§5.3** (premise restated — F3),
> **§5.5**, and **§6** (D4/D5). §7's gate assessment is unchanged: the gates were
> about the behavior matrix, the migration, and the contract, none of which the
> review reopened.
>
> **Second revision — 2026-08-22, post-fix-verification.** `02b-fix-verification.md`
> checked whether the above actually landed: 10 clean, the rest concentrated in
> **§3.7**. Applied here: the dedup transaction now spans the duplicate `SELECT`
> and **whichever write follows it** — scoping it to the `INSERT` left the
> enrichment `UPDATE` racing identically (F1); the Garmin push moved **outside**
> the transaction, because the in-transaction timeout the previous revision
> mandated names a mechanism `garminconnect==0.3.11` does not provide (N1) — this
> also reverses the route's push-before-write ordering for **every** request, not
> just duplicates (see §1.4); the misquoted busy-timeout stall corrected from
> 1.00 s to 5.01 s (N2); §3.5's superseded key-names block removed and §1.7's
> stale fixture description corrected; §4.5 rule 4's reconciliation advice
> qualified, since `/api/weight/recent` is hardcoded to `LIMIT 10`.

Everything in §1 was read out of the working tree or verified by running code,
not taken from the spec documents. Where `docs/prp/vitalforge-token-auth-pr.md`
assumed something the code does not do, §1.2 says so explicitly and §2 designs
against the code.

Verification commands used to establish the non-obvious facts in §1 are recorded
inline so a reviewer can re-run them.

---

## 1. As-is state (ground truth)

### 1.1 Auth is middleware, not a dependency

`shared/auth.py` holds module-level config read **once at import time**:

| Global | Source | Default |
|---|---|---|
| `_SECRET` | `VITALFORGE_SECRET` | `"default-dev-secret"` |
| `_USER` | `VITALFORGE_USER` | `"admin"` |
| `_PASS` | `VITALFORGE_PASS` | `""` |
| `_COOKIE_NAME` | constant | `"vf_session"` |
| `_MAX_AGE` | constant | 30 days |

Enforcement lives entirely in the `auth_middleware` closure registered inside
`add_auth_routes()` (`shared/auth.py:167-183`). Its logic:

1. Exempt `path.startswith("/auth/")`, `path == "/health"`,
   `path.startswith("/static/")`.
2. If `not _is_auth_configured()` (i.e. `_PASS` is empty) → pass through.
3. `user = get_current_user(request)`; if `None`:
   - `/api/*` → `raise HTTPException(401)`
   - anything else → `RedirectResponse("/auth/login", 302)`

**`require_auth` (`shared/auth.py:48-52`) is dead code.** Verified:

```
$ grep -rn "require_auth\|get_current_user" --include=*.py . | grep -v shared/auth.py
(no results)
```

No route in either service declares it as a dependency. The token-auth spec's
"in the existing auth dependency, short-circuit before the session cookie check"
therefore has no dependency to modify. The real single chokepoint is
`get_current_user()` (`shared/auth.py:39-45`), which is called by the middleware
(line 177), by the login page redirect (line 145), and by the dead
`require_auth`. **Track A's bearer check belongs inside `get_current_user()`.**

`get_current_user()` returns `"anonymous"` when auth is unconfigured, the
username on a valid cookie, and `None` otherwise.

### 1.2 Three verified defects in the as-is auth path

These are not hypotheticals; each was reproduced.

**D1 — `/api/*` returns 500, not 401, when unauthenticated.**
`raise HTTPException` inside a `@app.middleware("http")` dispatch is not caught
by Starlette's `ExceptionMiddleware` (which sits *inside* the router, below user
middleware). It propagates to `ServerErrorMiddleware` and surfaces as a
plain-text 500.

```
$ python3 probe_401.py        # VITALFORGE_PASS set, no credential presented
/api/thing -> 500 'text/plain; charset=utf-8' 'Internal Server Error'
/page      -> 302 None ''
```

Consequences: today's Tasker cookie client cannot distinguish an expired cookie
from a server fault, and **there is currently no 401 response shape for the
Bascule contract to pin.** Fixing this is in Track A's scope (§2.3) — it is the
behavior the code already intends, and it is strictly an improvement for the
existing cookie clients.

**D2 — `compare_digest` raises `TypeError` on non-ASCII `str`.**

```
$ python3 -c "import hmac; hmac.compare_digest('tökén','tökén')"
TypeError: comparing strings with non-ASCII characters is not supported
```

`check_credentials` (`shared/auth.py:55-56`) compares `str` directly, so a
non-ASCII password submitted to `POST /auth/login` raises inside the route →
500. Track A must not replicate this on the token path: **compare `bytes`, not
`str`** (§2.2). Phase 4's review brief names "unicode in token" explicitly.

**D3 — `hmac.compare_digest("", "")` returns `True`** (verified). With `_PASS`
empty, `POST /auth/login` with `{"username":"admin","password":""}` mints a valid
session cookie. Harmless today because auth is off in that configuration, but it
is the exact shape of the "token set but empty string" failure mode in §5.1, and
the token path must guard against it independently of the empty-value check.

### 1.3 Database and "migrations"

`shared/database.py` is 140 lines of `CREATE TABLE IF NOT EXISTS` inside
`init_db()`, awaited from each service's lifespan before it serves traffic.
There is no migration tool, no version table, no `ALTER` anywhere.

`weight_log` (the only table Track B writes) is:

```sql
CREATE TABLE IF NOT EXISTS weight_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weight_lbs REAL NOT NULL,
    weight_kg REAL NOT NULL,
    weight_grams INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    synced_to_garmin INTEGER DEFAULT 0
)
```

No unique constraint, no index on `timestamp`, no `source`. `timestamp` is
`datetime.now(timezone.utc).isoformat()` — server receipt time, ISO-8601 with a
`+00:00` offset.

Every other table is date-keyed (`date TEXT PRIMARY KEY`) and written by
`sync.py`'s `upsert()` helper via `INSERT OR REPLACE`. `weight_history` already
carries `body_fat`.

Relevant verified SQLite behavior (Python 3.12/3.14, SQLite 3.50.2):

- Duplicate `ADD COLUMN` raises exactly
  `sqlite3.OperationalError: duplicate column name: <name>`.
- `ALTER TABLE ... ADD COLUMN` **auto-commits without an explicit `commit()`** —
  a connection closed immediately after the `execute()` still leaves the column
  visible to other connections. Confirmed by probe.
- `ADD COLUMN` with no non-constant `DEFAULT` is an O(1) schema-header change; it
  does not rewrite existing rows.

Both services open the same file and both run `init_db()` at boot, and
`docker-compose.yml` starts them together. Concurrent DDL is a real condition,
not a thought experiment (§3.3).

### 1.4 The weight route

`vitalforge-weight/app.py:46-114`:

```python
class WeightIn(BaseModel):
    weight: float
    unit: str = "lbs"
```

No bounds of any kind. `unit` is validated *in the route body* and returns
**400** (line 70) — not Pydantic's 422. `tests/test_weight_api.py:48-50` asserts
that 400. Order of operations is: convert → **push to Garmin** → write to SQLite
→ respond. Garmin failure is caught, logged, and reported as
`synced_to_garmin: false` + `garmin_error`, and the row is still stored.

> **Behavior change in Track B, for the record.** §3.7 reverses this ordering for
> **every** request, not only duplicates: after B4 the route writes to SQLite and
> commits *first*, then pushes to Garmin, then updates the sync flag. This is a
> consequence of making dedup atomic — the write lock must not span a network
> call — but it changes the plain non-duplicate path too, and it is a second,
> smaller behavior change riding along with the concurrency fix. Externally
> visible difference: a row now exists (briefly, at `synced_to_garmin = 0`)
> before the Garmin push is attempted, where previously the push happened first.
> The response shape is unchanged.

The PWA sends exactly `{weight, unit}` and nothing else
(`vitalforge-weight/templates/index.html:359`), so tightening the model with
`extra="forbid"` cannot break it.

Because `weight` has no upper bound and the FIT encoder packs weight as
`uint16` at scale 100 (§1.5), a fat-fingered `99999` today reaches
`struct.error` inside `garminconnect`, is swallowed by the route's bare
`except Exception`, and the bogus row is stored with `synced_to_garmin: false`.

### 1.5 Garmin client — verified signature and units

`garminconnect==0.3.11`, installed at
`/home/user/.local/lib/python3.14/site-packages/garminconnect`.

`shared/garmin_client.py:61-75` (`push_weight`) calls
`add_body_composition(timestamp=..., weight=kg)` and nothing else.

`Garmin.add_body_composition` (`__init__.py:1183-1226`) accepts:

| Parameter | Unit | FIT field | Base type | Scale |
|---|---|---|---|---|
| `timestamp` | ISO-8601 str | 253 | uint32 | 1 |
| `weight` | **kg** | 0 | uint16 | 100 |
| `percent_fat` | **%** | 1 | uint16 | 100 |
| `percent_hydration` | **%** | 2 | uint16 | 100 |
| `visceral_fat_mass` | kg | 3 | uint16 | 100 |
| `bone_mass` | **kg** | 4 | uint16 | 100 |
| `muscle_mass` | **kg** | 5 | uint16 | 100 |
| `basal_met` / `active_met` | kcal | 7 / 9 | uint16 | 4 |
| `physique_rating` / `metabolic_age` / `visceral_fat_rating` | — | 8 / 10 / 11 | uint8 | 1 |
| `bmi` | — | 13 | uint16 | 10 |

Three findings that shape Track B:

**G1 — there is no muscle-*percentage* field.** `muscle_mass` is a mass in kg.
The task brief asks for "muscle %". This is the escalation trigger "discovering
that `garminconnect` cannot push a field the design assumed" → §6, D1.

**G2 — `bone_mass` and `muscle_mass` are masses, `percent_fat` and
`percent_hydration` are percentages.** Mixing these up is silent data
corruption, not an error.

**G3 — one call, one FIT upload.** Weight and composition are written into a
single `weight_scale` record and POSTed as one multipart upload
(`__init__.py:1220-1226`). **Weight and composition cannot succeed or fail
independently** unless we deliberately split them into two calls. This
invalidates the premise of one of the required failure modes (§5.3).

**G4 — no clamping.** `FitBaseType.pack` does `pack(fmt, int(value))` with no
range check (`fit.py:178-183`), and `_build_content_block` multiplies by the
scale first (`fit.py:250-251`). Any value ≥ 655.36 in a uint16/scale-100 field
raises `struct.error`. Only `weight` is validated by `garminconnect` at all
(`_validate_positive_number`, `__init__.py:95-107`) — the composition fields are
entirely unvalidated. **Range validation in our DTO is the only thing standing
between a bad client and a 500-shaped failure.**

### 1.6 Dashboard metric exposure

`METRIC_TABLES` (`vitalforge-dashboard/app.py:31-45`) maps a metric key to a
`(table, column)` pair. `/api/metrics/{name}` (line 123) interpolates them into
`SELECT date, [col] as value FROM [table] WHERE date >= date('now', ?)`.

The pattern **requires a `date`-keyed, one-row-per-day table**. `weight_log` has
`timestamp`, not `date`, and can hold several rows per day, so it cannot be
exposed through this endpoint without a special case. `body_fat` is already
mapped to `weight_history.body_fat`, populated by
`sync.py:sync_weight_history()` from `latestWeight.bodyFat`.

Table/column names come from a hardcoded dict, never from user input, so the
f-string interpolation is not an injection vector; the `days` parameter is
bound. No change needed there.

### 1.7 Test harness

`tests/conftest.py` already provides everything Phases 1–4 need:

- `tmp_db_path` — monkeypatches `shared.database.DB_PATH` **and** sets the env
  var, because `get_db()` re-reads the module global each call.
- `fake_garmin_client` — a `FakeGarminClient` whose `add_body_composition`
  records `{"timestamp", "weight"}` and **discards `**kwargs`**. It must be
  extended to capture the composition kwargs, or Track B's mapping tests assert
  nothing.
- `weight_app_module` — patches `authenticate` and `push_weight` **on the app
  module**, because `app.py` did `from shared.garmin_client import ...` and
  patching the shared module alone would not reach the bound names.
- Services are loaded with `importlib.import_module("vitalforge-weight.app")`
  (hyphenated directory names).

`tests/fixtures/garmin/weigh_ins.json` originally held only
`{weight, bmi, bodyFat}`. It has since been extended with the real
`bodyWater`/`boneMass`/`muscleMass` key names — but with **synthetic values**,
because the live account's are all `null`. That makes the fixture green under any
unit, which is exactly why §3.5 treats the units as unverified.

`pyproject.toml` runs `pytest` with `asyncio_mode = "auto"`, `pythonpath = ["."]`
and excludes `-m playwright` by default. CI (`.github/workflows/docker.yml`)
already runs `ruff check .`, `pytest -q`, then `pytest -q -m playwright` as a
`test` job gating `build-and-push`. Phase 2's "CI pinned" item is largely already
satisfied; `mypy` is **not** configured and adding it is out of scope for these
two tracks (extend, don't replace).

**The import-time-globals trap:** `_PASS`, `_SECRET`, and the new
`_API_TOKEN` are read at module import. Behavior-matrix tests **must
`monkeypatch.setattr(shared.auth, "_API_TOKEN", ...)`**, not just
`monkeypatch.setenv(...)` — the same trap conftest.py already documents for
`DB_PATH` and `push_weight`. Without this, Phase 2's red contract tests are
unwritable. `_serializer` is likewise built from `_SECRET` at import, so tests
that change the secret must rebuild it.

---

## 2. Track A — bearer token auth

### 2.1 Placement

The bearer check goes **inside `get_current_user()`, before the cookie
lookup**, and after the auth-disabled short-circuit:

```
get_current_user(request):
    1. if not _is_auth_configured():   return "anonymous"     # unchanged
    2. if _bearer_token_valid(request): return "api-token"    # NEW
    3. cookie = request.cookies.get("vf_session")             # unchanged
    4. if not cookie: return None
    5. return validate_session(cookie)
```

Why here and not in the middleware: `get_current_user()` is the one function
every caller funnels through (middleware, login-page redirect, `require_auth`).
Putting the check in the middleware would leave `require_auth` — dead today, but
the natural thing for a future route to adopt — silently token-blind.

Why before the cookie check: a machine client never has a cookie, so a valid
token must not be shadowed by a missing or malformed one. Note the ordering is
*not* load-bearing for security, only for cost and clarity — step 2 returning
`False` always falls through to step 3, so a wrong token never blocks a valid
cookie. This is a deliberate "alternative credential" model.

**Reviewed and upheld.** *Can the bearer path ever weaken the cookie path?* was a
mandatory Phase 2 DA target, and the reviewer attacked it directly — including
trying to construct a raise inside `_bearer_token_valid` that would escape
`get_current_user()` and turn a valid-cookie request into a 500. It does not
work: Starlette decodes headers as latin-1, so every header value is in
U+0000–U+00FF and `.encode("utf-8")` on such a string cannot raise; `partition`,
`.lower()`, and `.strip()` cannot raise either. **On authentication, the ordering
holds.** The review did find a weakening on a different axis the design had not
considered — **revocation**, not authentication — which is documented in §4.1
and the README rather than changed here (F7).

The returned principal for a token request is the literal string `"api-token"`,
distinct from `"anonymous"` (auth disabled) and from any real username. Nothing
consumes the principal today; keeping it distinguishable costs nothing and makes
future request attribution possible.

### 2.2 The comparison

```
_API_TOKEN = os.environ.get("VITALFORGE_API_TOKEN", "").strip()
_API_TOKEN_BYTES = _API_TOKEN.encode("utf-8")

_bearer_token_valid(request) -> bool:
    if not _API_TOKEN:                      return False   # guard 1 (§5.1)
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":          return False
    value = value.strip()
    if not value:                           return False   # guard 2 (§5.1)
    return hmac.compare_digest(value.encode("utf-8"), _API_TOKEN_BYTES)
```

Requirements this encodes:

- **Constant time.** `hmac.compare_digest` (identical to `secrets.compare_digest`
  — both are `_hashlib.compare_digest`) on **bytes**. Comparing `bytes` rather
  than `str` is mandatory, not stylistic: it is the only thing preventing D2's
  `TypeError` → 500 on `Authorization: Bearer tökén`. Note `compare_digest` is
  constant-time only with respect to *content*, not *length*; the length of a
  `token_urlsafe(32)` is not a secret.
- **Two independent empty guards.** `not _API_TOKEN` and `not value`. Either
  alone would suffice, but D3 proved `compare_digest("", "")` is `True`, so the
  empty case is guarded on both sides.
- **`.strip()` on both sides.** The env var is stripped at import (a trailing
  newline from a `.env` file or a secrets mount is the single most likely
  operator error). The presented value is stripped too. This cannot weaken the
  comparison — whitespace carries no entropy and stripping cannot produce a
  prefix match — and it removes a class of unfalsifiable client bugs.
- **Case-insensitive scheme.** `scheme.lower()`. Starlette's header lookup is
  already case-insensitive for the header *name*.
- **No logging, ever.** `_bearer_token_valid` logs nothing on any path. Rationale
  in §2.4.

### 2.3 The 401 response (fixes D1)

The middleware stops raising and starts returning:

```
if path.startswith("/api/"):
    return JSONResponse(
        status_code=401,
        content={"detail": "Not authenticated"},
        headers={"WWW-Authenticate": "Bearer"},
    )
return RedirectResponse("/auth/login", status_code=302)
```

- Body `{"detail": "Not authenticated"}` reproduces exactly what the existing
  `HTTPException(401, "Not authenticated")` was *intended* to emit, so this
  matches FastAPI's convention and no client sees a novel shape.
- **`WWW-Authenticate: Bearer` is included.** RFC 7235 requires a challenge on
  401. It is safe for the PWA: only `/api/*` returns 401 (HTML paths still 302),
  `fetch()` ignores the header, and browsers raise a native credential dialog for
  `Basic`, not `Bearer`. Bascule pins this header either way, so the decision is
  recorded here rather than left to implementation.
- The 401 body is a fixed constant. It never echoes the `Authorization` header,
  the presented token, the cookie, or the reason for failure — a client learns
  "not authenticated", not "your token was 3 characters short".
- HTML paths keep the 302 redirect. Unchanged.

This is a **behavior change to an existing endpoint** (500 → 401) and lands with
its own regression test in the same PR.

### 2.4 Logging audit

Ground rule: never log credentials, tokens, or `Authorization` headers.

**The policy is "no logging on the per-request auth path"** — not "no logging in
`shared/auth.py`." That distinction was implicit in the original draft and the
DA correctly read the broader version as ruling out a startup warning it
shouldn't (F6). Stated precisely:

- **Per-request auth path: no logging, ever.** A failed token attempt is
  deliberately not logged. This is a single-operator LAN deployment where the log
  volume from a scanner would exceed its diagnostic value, and any log line about
  a token invites a later "log the prefix for debugging" patch.
- **Startup configuration warnings: yes, exactly one.** It fires once per boot,
  contains no credential, and matches what the services already log at startup
  (`app.py:27`, `:29`, `:33`):

  ```python
  if _API_TOKEN and not _PASS:
      logger.warning(
          "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty — "
          "auth is DISABLED and the token is inert. Set VITALFORGE_PASS to enable auth."
      )
  ```

  **Why this is load-bearing.** Configuration A3 (§2.5) is reachable by ordinary
  operator error on a live system holding real health data: a `.env` edit, a
  secrets mount that fails to populate, a `docker-compose.prod.yml` that omits
  the variable. Both services then serve completely unauthenticated on every
  path — and the operator has *positive evidence auth is working*, because the
  token client keeps getting 200s. A token client cannot distinguish "my token is
  validated" from "auth is off"; neither can a `curl` with a deliberately wrong
  token, which is 200 in A3 and 401 in A1. The only way to notice is to already
  suspect it. One line converts a silent open server into a loud one. The
  behavior matrix is unchanged — A3 stays open-access.
- Uvicorn's default access logger emits method, path, and status only — no
  headers. No custom access-log config exists in either service. Verify unchanged
  at implementation time.
- `shared/garmin_client.py:69` and `:74` log the weight value and Garmin's
  response dict. Neither contains credentials. They *do* log personal health
  data at INFO, which is pre-existing behavior and out of scope, but Track B must
  not extend those lines with anything new (§3.4).

### 2.5 Behavior matrix — complete

Configurations:

- **A1** `VITALFORGE_PASS` set · `VITALFORGE_API_TOKEN` set
- **A2** `VITALFORGE_PASS` set · `VITALFORGE_API_TOKEN` unset, empty, or
  whitespace-only (all three collapse: `.strip()` at import makes them identical)
- **A3** `VITALFORGE_PASS` unset/empty · `VITALFORGE_API_TOKEN` set
- **A4** `VITALFORGE_PASS` unset/empty · `VITALFORGE_API_TOKEN` unset

Credentials presented:

| id | Presented |
|---|---|
| C0 | nothing |
| C1 | valid `vf_session` cookie |
| C2 | invalid / expired / garbage cookie |
| C3 | `Authorization: Bearer <correct>` |
| C4 | `Authorization: Bearer <wrong>` |
| C5 | `Authorization: Bearer` (empty or whitespace-only value) |
| C6 | wrong or missing scheme (`Basic <correct>`, or bare `<correct>`) |
| C7 | valid bearer **+** invalid cookie |
| C8 | valid cookie **+** wrong bearer |
| C9 | `Authorization: Bearer <non-ASCII>` |

Outcome column is for an `/api/*` path. For an HTML path, every "401" below is
instead a **302 → `/auth/login`**; every "allow" is unchanged.

`/health`, `/auth/*`, and `/static/*` are exempt from **middleware enforcement**
in all 40 cells — the middleware returns before any credential is inspected
(`shared/auth.py:171`), so no configuration can make them 401 or 302.

An earlier draft said they were "never affected by any configuration," which the
DA correctly falsified (F7): `GET /auth/login` calls `get_current_user()`
*directly* (`shared/auth.py:145`) and branches on the result, so once the bearer
check lives inside that function, a request presenting a valid token to
`/auth/login` gets a 302 to `/` instead of the login page. Harmless in practice —
browsers do not send `Authorization` headers, and machine clients do not fetch
the login page — but the exemption is from *enforcement*, not from the credential
logic, and the table's completeness claim rests on stating that accurately.

#### A1 — PASS set, TOKEN set

| Cell | Presented | Principal | Result | Notes |
|---|---|---|---|---|
| A1-C0 | nothing | `None` | **401** | |
| A1-C1 | valid cookie | username | **allow** | no regression |
| A1-C2 | bad cookie | `None` | **401** | |
| A1-C3 | valid bearer | `api-token` | **allow** | the Bascule path |
| A1-C4 | wrong bearer | `None` | **401** | falls through to cookie (absent) |
| A1-C5 | `Bearer` empty | `None` | **401** | guard 2 |
| A1-C6 | wrong scheme | `None` | **401** | scheme check; token never compared |
| A1-C7 | valid bearer + bad cookie | `api-token` | **allow** | bearer-first ordering |
| A1-C8 | valid cookie + wrong bearer | username | **allow** | fall-through, no side effects |
| A1-C9 | non-ASCII bearer | `None` | **401** | bytes compare; **must not 500** (D2) |

#### A2 — PASS set, TOKEN unset/empty/whitespace

Bearer auth is fully disabled. Behavior is byte-identical to today's code except
that unauthenticated `/api/*` now returns 401 instead of 500 (D1).

| Cell | Presented | Principal | Result | Notes |
|---|---|---|---|---|
| A2-C0 | nothing | `None` | **401** | was 500 |
| A2-C1 | valid cookie | username | **allow** | |
| A2-C2 | bad cookie | `None` | **401** | was 500 |
| A2-C3 | "valid" bearer | `None` | **401** | guard 1: no token configured |
| A2-C4 | wrong bearer | `None` | **401** | guard 1 |
| A2-C5 | `Bearer` empty | `None` | **401** | guard 1 |
| A2-C6 | wrong scheme | `None` | **401** | guard 1 |
| A2-C7 | bearer + bad cookie | `None` | **401** | guard 1, then cookie fails |
| A2-C8 | valid cookie + bearer | username | **allow** | header ignored entirely |
| A2-C9 | non-ASCII bearer | `None` | **401** | guard 1 returns before any compare |

#### A3 — PASS unset, TOKEN set

**Auth is off. The token is inert.** `_is_auth_configured()` is `False`, so
`get_current_user()` returns `"anonymous"` at step 1 and never reaches the bearer
check.

| Cell | Presented | Principal | Result |
|---|---|---|---|
| A3-C0 … A3-C9 | *any of the ten* | `anonymous` | **allow** (all ten cells) |

This is the one cell-group that looks ambiguous — "a token is configured, surely
it should be enforced?" — but it is **resolved by constraint, not escalated**.
The ground rule is "existing clients unaffected at every merge point": today,
`VITALFORGE_PASS` empty means open access, and CLAUDE.md documents this as
expected dev behavior. Making a token become load-bearing when `PASS` is empty
would silently lock out every existing client of any deployment that sets a token
without a password. `VITALFORGE_PASS` remains the single master switch for
whether auth exists at all.

**Reviewed and upheld, with one addition.** The Phase 2 DA accepted the reasoning
— the ground rule genuinely resolves the *design* question — but found that the
decision leaves a silently-open server that no client can detect (F6). The matrix
is unchanged; the mitigation is the one-line startup warning in §2.4. The
reviewer also noted, fairly, that this design resolved an ambiguous matrix cell
in JD's favour without asking JD, on a system holding his real data — the call
was right on the merits, thin on process.

#### A4 — PASS unset, TOKEN unset

| Cell | Presented | Principal | Result |
|---|---|---|---|
| A4-C0 … A4-C9 | *any of the ten* | `anonymous` | **allow** (all ten cells) |

Today's default `.env.example`-less behavior, entirely unchanged.

**40 of 40 cells specified.**

### 2.6 Track A surface area

`shared/auth.py` (bearer helper, `get_current_user` step 2, middleware 401),
`.env.example`, `README.md` (env table, Authentication section, Tasker section
rewritten from cookie-copying to `Authorization: Bearer`), `tests/`.
No change to either service's `app.py`. No change to `shared/garmin_client.py`.

---

## 3. Track B — body-composition intake

### 3.1 Request DTO

```python
class WeightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: float
    unit: str = "lbs"                     # kept as str — see below
    body_fat_pct:   float | None = Field(default=None, ge=3.0,  le=75.0)
    body_water_pct: float | None = Field(default=None, ge=30.0, le=80.0)
    muscle_pct:     float | None = Field(default=None, ge=10.0, le=90.0)
    bone_mass_kg:   float | None = Field(default=None, ge=0.5,  le=10.0)
    source: Literal["pwa", "bascule", "bridge", "tasker"] | None = None
```

Two deliberate non-changes:

- **`unit` stays a plain `str` with the route's 400 check.** Converting it to a
  `Literal` would turn the existing 400 into a 422 and break
  `tests/test_weight_api.py:48-50`. It is retained as a **legacy quirk**, not as
  backward compatibility: the DA checked, and the only thing depending on that
  400 is this repo's own test, which asserts the status and nothing about the
  body (F8c). No external client parses it. Changing it has no benefit, so it
  stays; but the honest label is "legacy", and §4.4 says so.
- **No `measured_at` field.** A bridge delivering a weigh-in recorded minutes
  earlier is a real scenario, but adding client-supplied timestamps changes the
  dedup key, the Garmin `timestamp` argument, and the contract all at once. Out
  of scope here; recorded as a known gap so Bascule's milestone planning does not
  assume it exists.

**`extra="forbid"` is safe**: the PWA sends exactly `{weight, unit}`
(`index.html:359`) and the documented Tasker payload is the same. Unknown fields
now produce a 422 rather than being silently dropped — the "catch client drift
loudly" rule. One caveat: the token-auth spec names `ble-scale-sync` on Atlas as
a live client, and its payload is not in this tree, so it cannot be checked here.
The ground rule's "existing clients" are the PWA, the Tasker cookie flow, and
dashboard sync — all verified — but if `ble-scale-sync` sends any field beyond
`{weight, unit}` it will start getting 422s. Worth one question to JD before the
Track B merge; it does not change the design.

`weight` also gets a post-conversion bound of **`2.0 ≤ weight_kg ≤ 500.0`**,
implemented as a **`model_validator(mode="after")` returning 422** — the same
status as every other validation Track B adds.

An earlier draft put this in the route as a 400, reasoning that the limit is in
kg while the input may be in lbs and so could not live on the field. The DA
falsified the premise (F8b): `model_validator(mode="after")` handles exactly this
kind of derived, cross-field constraint, and produces a normal 422 body:

```
{"detail": [{"type": "value_error", "loc": ["body"],
             "msg": "Value error, weight must be between 2 and 500 kg after unit conversion"}]}
```

Nothing was being traded off — the legacy `unit` 400 stays untouched either way.
Extending a legacy quirk into brand-new surface area on a
backward-compatibility rationale that cannot apply to a validation which has
never existed was simply wrong. Rationale for the bound itself:
below 2 kg is a decimal or unit error; above 500 kg exceeds any human and, more
importantly, the FIT `uint16`/scale-100 ceiling is **655.35 kg** (§1.5 G4) —
without this bound a large input reaches `struct.error` inside `garminconnect`
(§1.4). This closes a pre-existing hole.

### 3.2 Validation bounds and why

The bounds exist to **catch unit errors, decimal-place errors, and BIA garbage**
— not to gatekeep physiology. That framing justifies generous edges: a bound that
rejects a real person is a worse failure than one that lets an unusual-but-real
value through.

| Field | Range | What it catches |
|---|---|---|
| `body_fat_pct` | 3–75 | `0.20` sent as a fraction (→ rejected, the point); raw BIA impedance in the hundreds. Floor is near the essential-fat limit; ceiling is above any recorded human so it never rejects a real reading. |
| `body_water_pct` | 30–80 | Same fraction/raw-unit errors. Real adults sit ~45–65%; the window is deliberately wider than physiology so a genuine outlier is stored, not dropped. |
| `muscle_pct` | 10–90 | Fractions and raw units. Consumer scales vary wildly in what they call "muscle" (skeletal vs. lean mass), so a tight bound would reject legitimate device output. |
| `bone_mass_kg` | 0.5–10.0 | Grams-instead-of-kg (`3200` → rejected) and fractions. **It cannot catch a lbs/kg mixup**: a real 3.5 kg skeleton sent as 7.7 lbs lands inside the range and is stored wrong. Only the field *name* defends against that, which is why it is `bone_mass_kg` and not `bone_mass` (§4). |
| `weight` (derived kg) | 2–500 | Decimal errors, unit errors, and the FIT 655.35 kg encoder ceiling. |

All five fields are independently optional. There is no cross-field requirement —
a client may send weight plus body fat only. The one derived cross-field value,
`muscle_mass_kg = weight_kg × muscle_pct / 100`, is bounded above by
`500 × 0.90 = 450 kg`, comfortably under the 655.35 encoder ceiling, so no
additional guard is needed.

### 3.3 Migration

Five nullable columns on `weight_log`, all `REAL` except `source TEXT`:

```
body_fat_pct REAL · body_water_pct REAL · muscle_pct REAL
bone_mass_kg REAL · source TEXT
```

The migration is applied in **two places**, both inside `init_db()`:

1. Added to the `CREATE TABLE IF NOT EXISTS weight_log (...)` body, so a fresh
   database is created correct in one statement.
2. A guarded `ALTER TABLE weight_log ADD COLUMN <name> <type>` per column, for
   databases that already exist.

The guard is **attempt-and-swallow, not check-then-act**:

```
for column_ddl in ADDITIVE_COLUMNS:
    try:
        await db.execute(f"ALTER TABLE weight_log ADD COLUMN {column_ddl}")
        await db.commit()
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
```

Why not `PRAGMA table_info`: both services run `init_db()` against the same file
and `docker-compose` starts them together, so a pre-check is TOCTOU-racy — both
can observe "absent" and both then attempt the `ADD COLUMN`. Attempt-and-swallow
is correct under that race by construction. Verified error text is exactly
`duplicate column name: <name>` (§1.3).

Only the duplicate-column error is swallowed. **`database is locked` must
propagate** — a container that cannot acquire the write lock should fail its
lifespan and be restarted by Docker, not proceed to serve traffic against a
half-migrated schema.

`await db.commit()` is belt-and-braces: §1.3 verified that `ADD COLUMN`
auto-commits regardless.

Properties this satisfies:

- **Additive only.** No `DROP`, no `RENAME`, no type change, no `NOT NULL`, no
  `DEFAULT`. Historical rows get `NULL` in all five columns, which is the
  intended "unknown provenance / no composition recorded" value.
- **Idempotent.** Re-running is a no-op by construction.
- **Rollback-safe.** Every existing `SELECT` and `INSERT` in both services names
  its columns explicitly (`app.py:98`, `:122`, `:146`; `sync.py:27-30`), so the
  previous image reads and writes a migrated database without noticing the extra
  columns. Rollback is `docker compose pull` of the previous tag — no data step.
- **Cheap.** `ADD COLUMN` without a non-constant default is an O(1) schema-header
  write; it does not rewrite the weigh-in history.

Interrupted-mid-run analysis is in §5.4.

### 3.4 Garmin push mapping

`shared/garmin_client.push_weight` gains **keyword-only** optional parameters, so
the existing positional call sites are untouched:

```python
def push_weight(
    weight_grams: int,
    timestamp: datetime | None = None,
    *,
    percent_fat: float | None = None,
    percent_hydration: float | None = None,
    muscle_mass_kg: float | None = None,
    bone_mass_kg: float | None = None,
) -> None:
```

which forwards to the single `add_body_composition` call:

| DTO field | → `add_body_composition` kwarg | Unit at the wire | Transform |
|---|---|---|---|
| `weight` (converted) | `weight` | kg | `weight_grams / 1000` (unchanged) |
| `body_fat_pct` | `percent_fat` | % | pass-through* |
| `body_water_pct` | `percent_hydration` | % | pass-through* |
| `muscle_pct` | `muscle_mass` | **kg** | `weight_kg × muscle_pct / 100` ← **§6 D1** |
| `bone_mass_kg` | `bone_mass` | kg | pass-through* |

\* **"Pass-through" means no unit conversion, not no loss.** The FIT encoder
multiplies by the scale (100) and then **truncates** — `int(value)`, not
`round()` (`fit.py:179-183`, `:249-251`) — so every value is silently floored to
0.01 resolution. With binary floats that costs a hundredth on ordinary inputs,
including the one used in this document's own contract example:

```
$ python3 -c "print(int(18.4*100), int(55.2*100), int(40.1*100), int(3.2*100))"
1839 5520 4010 320
```

`body_fat_pct: 18.4` reaches Garmin as **18.39**, while `weight_log` stores
18.4. They will never agree. This is **accepted, not fixed** (F10): a hundredth
of a percent is clinically irrelevant, and pre-rounding before the call would
mean touching a fragile reverse-engineered path for no user-visible gain. It is
recorded because two downstream claims depended on precision the encoder does not
provide — see §3.5, and note that any future assertion comparing a pushed value
to a read-back value **must use a tolerance**. The same truncation is
pre-existing for `weight` (84.096 kg already encodes as 84.09).

Not sent: `visceral_fat_mass`, `basal_met`, `active_met`, `physique_rating`,
`metabolic_age`, `visceral_fat_rating`, `bmi`. The BF720 does not report them and
`bmi` is already derived by Garmin from weight and the user's profile height —
sending our own would create a second source of truth for a value Garmin already
computes.

`None` fields are passed through as `None` and land in the FIT record as the
base type's *invalid* sentinel (`0xFFFF`), which is the correct FIT encoding for
"not measured" — omitting a field is not an option, the record layout is fixed.

**No change to `authenticate()`, `get_client()`, or the token store.** The
`_client` singleton and the garth token flow are untouched, per the ground rule.

The existing `logger.info` lines at `garmin_client.py:69` and `:74` are **not**
extended with composition values (§2.4).

### 3.5 Dashboard exposure

Composition is exposed through the existing `/api/metrics/{name}` pattern by
adding nullable columns to the **date-keyed `weight_history` table** — not by
special-casing `weight_log`, which is timestamp-keyed and multi-row-per-day and
does not fit the endpoint's contract (§1.6).

Per CLAUDE.md's rule, all three files change together:

1. `shared/database.py` — `weight_history` gains `body_water REAL` plus
   **unit-suffixed** mass columns (same additive/guarded treatment as
   `weight_log`; `body_fat` already exists). The suffix is not decided yet — see
   "Column naming" below.
2. `vitalforge-dashboard/sync.py` — `sync_weight_history()` reads the new keys
   off `latestWeight` alongside the existing `bodyFat`.
3. `vitalforge-dashboard/app.py` — `METRIC_TABLES` gains `"body_water"`,
   `"muscle_mass"`, and `"bone_mass"` as **API metric keys**, each mapped to its
   unit-suffixed `weight_history` column. The dict key and the column name are
   separate strings, so the URL surface stays unsuffixed while the schema keeps
   its unit marker.

**Column naming — the `_kg`/`_g` suffix is mandatory here too (F11).** §3.2 and
§4.3 make the unit suffix load-bearing — *"the `_kg` suffix is the only
defense"* against a lbs/kg mixup that range validation cannot catch. An earlier
draft of this section then added bare `muscle_mass` / `bone_mass` columns,
stripping the convention it had just declared essential, for the same physical
quantity, in the same document. Without the suffix, one system would store bone
mass in two tables under two names with only one carrying a unit marker, and
anyone joining or charting them would have nothing in the schema to warn them.

The suffix cannot be chosen yet, because the units are unverified (below). Both
variants are named so the implementer picks rather than invents:
**`bone_mass_g` / `muscle_mass_g`** if grams, **`bone_mass_kg` /
`muscle_mass_kg`** if kg. This must be settled *before* B5 writes the columns —
correcting it afterwards means `ALTER ... RENAME`, which the ground rules forbid
as non-additive. `body_water` needs no suffix; it is a percentage, and the DTO
field is `body_water_pct`.

Consequence, stated plainly: **composition posted to the PWA is not visible on
the dashboard until the next sync pulls it back from Garmin.** That is a
deliberate choice — it makes the dashboard show what Garmin actually *accepted*
rather than optimistically displaying what we hoped we sent.

An earlier draft called that "a round-trip verification of the push." **It is
not, and must not be relied on as an equality check** (F10): §3.4's FIT encoder
truncates every value to 0.01, so a pushed `18.4` reads back as `18.39` and a
strict comparison would report a permanent discrepancy on roughly half of all
values. What the round trip actually verifies is *presence* — that Garmin
ingested the field at all — not *fidelity*.

**Key names confirmed; units UNVERIFIED (F4).** The distinction matters and an
earlier draft blurred it by labelling this "RESOLVED". What was confirmed is
**names**. The **units of `boneMass` and `muscleMass` on the read path have never
been observed and cannot be**, because every composition field in the live
account is `null` — nothing has ever pushed composition to Garmin, which is what
Track B implements.

The one adjacent field whose unit *is* observable is in **grams**:
`sync.py:221` reads `latestWeight.weight` and writes it straight into
`weight_history.weight_grams`. So the working **hypothesis is grams**, by
precedent — but it is a hypothesis, not a finding.

This is structurally untestable before the fact: the fixture's values are
synthetic (`boneMass: 3.2` alongside `weight: 81200` — kg and grams in one
object), and `sync.py` stores whatever number the fixture holds, so
`test_sync_populates_composition_from_weigh_ins_fixture` passes under grams,
under kg, or under any unit at all. The one thing the suite exists to catch here
is the one thing it cannot. If the units are grams and we assume kg, the
dashboard plots bone mass as ~3200 against a `weight_log` storing 3.2, silently.

**Resolution path:** the Phase 3 live checkpoint after B3 (one real weigh-in with
composition) is the first and only event that can make these fields non-null. Its
brief is extended to **read back the raw `get_weigh_ins()` response and record
the observed units**. **B5 cannot start until that checkpoint reports**, because
the column names depend on the answer (above). See `01-plan.md` B3 and B5.

Because the names are confirmed, **the `weight_log` date-aggregating fallback
this section previously described is not needed and is not being built** (see
`01-plan.md` §4.2 and B5). §6 D2's "keep the `weight_log` columns" reverts to a
preference — the failure-forensics argument — rather than a necessity.

### 3.6 Provenance (`source`)

`source TEXT` on `weight_log`, nullable, values `pwa` | `bascule` | `bridge` |
`tasker`. Historical rows stay `NULL` = unknown provenance.

Validated as a closed `Literal`, so an unknown value is a 422. This is the same
"fail loudly, don't silently accept" rule applied to `extra="forbid"`. The
tradeoff is real and worth stating: adding a fifth client type requires a server
change before that client can post. The mitigation is that it is a one-line,
backward-compatible addition, and the alternative — a free-form string — means a
typo'd `"basucle"` silently becomes a permanent, unqueryable provenance value.

The PWA's `fetch` body gains `source: "pwa"` (one line in
`templates/index.html:359`). `source` is optional, so a client that omits it
still succeeds with `NULL`.

`source` is deliberately **not** part of the dedup key (§3.7).

### 3.7 Dedup

**Key:** `abs(weight_grams − incoming) <= 50` (inclusive — exactly 50 g
collapses) **and** an existing row whose `timestamp` is within **60 seconds** of
now.
**Not keyed on `source`** — the whole point is to collapse the same physical
weigh-in arriving from two different bridges.

The boundary is specified as `<=` rather than left to the implementer (F12): a
0.05 kg-resolution scale straddles it exactly (84.10 vs 84.15 kg is 50 g), and
§4.5's retry-safety guarantee to Bascule is *defined* by this predicate, so a
coin-flip between `<` and `<=` is a coin-flip in a contract.

**The time comparison must use `julianday(timestamp)`, not string comparison.**
`/api/weight/trend` (`app.py:146`) compares an ISO-8601 `+00:00` string against
`datetime('now')` and works only because `'T' > ' '` (§5.6). `julianday()`
normalises the offset correctly and is the right tool here.

### Atomicity — the check and the insert are one transaction

**This is the fix for a blocking defect (F1), not an optimisation.** As
originally specified — read for duplicate, then push, then write, with no
transaction — dedup is check-then-act across multiple `await` points. Two
concurrent POSTs both run the `SELECT` before either runs the `INSERT`, both see
no duplicate, and both push to Garmin. The DA demonstrated this by modelling the
route faithfully and running the two-bridge case:

```
--- CONCURRENT (asyncio.gather), same weigh-in ---
rows in weight_log : 2     Garmin uploads : 2
--- SEQUENTIAL (control) ---
rows in weight_log : 1     Garmin uploads : 1
```

The feature failed *the exact scenario it exists for*, and a single uvicorn
worker is sufficient to trigger it — asyncio interleaving alone breaks it. The
control proves the predicate was right and the race was the whole defect. This
was doubly bad because §3.3 had already rejected `PRAGMA table_info` on TOCTOU
grounds for a migration that runs twice in the system's life, then used the
unprotected pattern on the path that runs on every weigh-in.

**Resolution (§6 D5): `BEGIN IMMEDIATE` on one connection, spanning the duplicate
`SELECT` and *whichever write follows it*, with the Garmin push moved outside the
transaction.** `BEGIN IMMEDIATE` takes the write lock up front, so a second
concurrent request blocks at `BEGIN` rather than proceeding to a stale read. The
alternative considered and rejected was a partial unique index on a quantised
bucket.

**The transaction must span the write, not the insert.** An earlier revision
scoped it to "the duplicate `SELECT` and the row `INSERT`" — which covers only
one of §3.7's three sub-cases. **Sub-case 2 (enrichment) is a read-modify-write
and is the identical check-then-act shape**: two concurrent composition-bearing
POSTs matching the same stored row both `SELECT` it, both see NULL composition
columns, both `UPDATE`, and both re-push — a lost update plus the double Garmin
upload this section spends four paragraphs worrying about, reached through the
path the first fix did not cover. Under a correctly scoped transaction the second
POST serialises, observes the now-non-NULL columns, and falls through to
sub-case 1 or 3.

### Ordering: read → write → commit → push → update-flag

```
1. BEGIN IMMEDIATE
2. SELECT for a duplicate match
3. Branch, and write inside the same transaction:
     no match          -> INSERT the new row
     enrichment match  -> UPDATE the NULL composition columns
     collapse/conflict -> no write
4. COMMIT                         <- lock released here; pure DB work, no network
5. Push to Garmin                 <- outside any lock
6. UPDATE synced_to_garmin / garmin_error from the push outcome
```

**Why the push moved out of the transaction.** Keeping the original
read → push → write ordering would have put the Garmin HTTP upload *inside* the
write transaction, and the only thing making that acceptable was a requirement
that the call carry an explicit timeout shorter than SQLite's busy timeout.
**That mitigation names a mechanism that does not exist**: `add_body_composition`
has no `timeout` parameter, nor does `Garmin.__init__`; the call bottoms out in
garth's session, which is adjacent to the auth flow the ground rules protect. It
is also **synchronous** — `push_weight` is not a coroutine and
`vitalforge-weight/app.py:87` calls it with no `await` inside an `async def`
route — so `asyncio.wait_for` could not bound it either, and for the duration of
the upload the weight service's whole event loop is blocked, not merely its write
lock.

With this ordering the lock covers only local DB work, so **no timeout mechanism
is needed at all.** The cost it replaces was real and 5× larger than an earlier
draft's pasted figure suggested — reproduced against `get_db()` exactly as
written (`database.py:9-15`, WAL, no `busy_timeout` override):

```
default busy_timeout (ms): 5000
concurrent writer: FAILED after 5.01s -> OperationalError: database is locked
concurrent READ ok -> 0
```

WAL leaves readers unaffected, but the dashboard's `sync.py` `upsert()` uses
`try/finally` with no `except`, so that error would have propagated. Note the
interaction with §3.3's deliberate "let `database is locked` propagate" decision:
that was reasoned about a once-per-deploy migration, and holding the lock across
an unbounded network call would have let a routine weigh-in produce the same
error in the *other* service.

**The residual risk of this ordering, stated plainly.** If the process dies
between step 5 and step 6, the row is stored but permanently reads
`synced_to_garmin = 0` even though Garmin holds the data — a false negative of
the kind §5.3 Path B already describes, and nothing reconciles it. That window is
sub-second and requires a crash inside it, which is far narrower than the
alternative of holding a write lock across an unbounded network call. Accepted.

**This changes the ordering for the plain non-duplicate case too** — see §1.4 and
the behavior-change note there. It is not solely an enrichment fix.

**Why a tolerance rather than exact equality.** `weight_grams` is
`round(weight_kg × 1000)`, and `weight_kg` is derived from a *client-supplied*
unit. The same weigh-in sent as `185.4 lbs` yields 84096 g; sent as `84.1 kg` it
yields 84100 g. Exact matching would miss the duplicate whenever the two bridges
disagree about units or rounding — which is exactly the scenario dedup exists
for, since there is no guarantee both read the scale through the same conversion
path. ±50 g (0.11 lb) comfortably absorbs that conversion delta. The cost is that
two genuine consecutive weigh-ins within 60 s **and** within 50 g of each other
collapse into one; those readings are indistinguishable in practice and
collapsing them loses no information.

**Ordering change:** the route currently pushes to Garmin *before* writing to
SQLite (§1.4). Dedup requires a DB read first, otherwise a duplicate is pushed to
Garmin before we discover it. The final ordering is the one established above —
**read → write → commit → push → update-flag** — not the push-before-write
ordering this section originally specified; see "Atomicity" for why the push
moved out of the transaction. Unlike the original dedup design, this *does*
change observable behavior for a non-duplicate request too: today's route pushes
to Garmin before the row exists in SQLite, and after this fix the row is
committed first. See §1.4's note on this same change.

On a duplicate hit, the response is **200 with `"deduplicated": true`** and the
existing row's `id` and `timestamp` — **not a 409**. A 4xx makes a well-behaved
bridge retry, which is precisely the wrong reaction to "your data is already
safely stored".

Three sub-cases, because a duplicate is not always identical:

1. **Incoming adds no new information** (its composition fields are absent, or
   equal to what is stored) → collapse. No Garmin push, no write. Return the
   existing row, `deduplicated: true`.
2. **Incoming adds composition the stored row lacks** (e.g. Tasker posted
   weight-only, Bascule follows seconds later with full BIA) → `UPDATE` the
   `NULL` columns **inside the transaction**, then, **after the commit**,
   re-push to Garmin using the stored row's original `timestamp`, not the
   current time. Return the existing `id` with
   `deduplicated: true, enriched: true`. Collapsing this case blindly would
   silently discard the composition data the whole track exists to capture. See
   the enrichment-push caveat below — this is the one place the design knowingly
   sends two uploads for one weigh-in.

   The `UPDATE` is inside the transaction specifically so two concurrent
   enrichment POSTs cannot both observe NULL columns and both re-push; the
   second serialises and falls through to sub-case 1 or 3. See "Atomicity" below.
3. **Incoming conflicts** — a different non-`NULL` value for a field already set
   → the stored value wins, nothing is overwritten, response carries
   `deduplicated: true, conflict: true`, and a WARNING is logged with the field
   names (values only, never credentials).

**Enrichment-push caveat (sub-case 2), stated because it cuts against §5.3.**
POST 1 has already uploaded the weight to Garmin, so any enrichment upload is a
*second* `add_body_composition` call for one weigh-in, and Garmin may end up
holding two records. That is knowingly in tension with §5.3's refusal to
manufacture Garmin/local divergence, and the reconciliation is this: §5.3 rejects
a fallback push whose contents would **disagree** with the stored row; an
enrichment push sends a payload that **matches** the row's final state exactly,
and reuses the original timestamp precisely so Garmin has the best chance of
treating it as the same weigh-in rather than a new one.

Whether Garmin actually collapses two same-timestamp uploads is **unverified and
unverifiable without a live account** — it is a named item for the Phase 3
"one real weigh-in verified in Garmin Connect" checkpoint. If the live check
shows Garmin keeps both records, fall back to **enrich locally and do not
re-push**, accepting that composition never reaches Garmin for a weigh-in that
arrived split across two POSTs, and that §3.5's dashboard exposure therefore
never shows it. The third option — treating a composition-bearing payload as a
non-duplicate and storing a second row — is rejected: it produces the same double
Garmin record *plus* a duplicated local row.

If the enrichment push fails, `synced_to_garmin` is set back to **0** and the
response carries `garmin_error` alongside `enriched: true`. The column means
"this row's current contents are in Garmin", and after a successful enrichment
`UPDATE` with a failed push, they are not.

**Why 60 seconds.** Two bridges racing on one weigh-in deliver within seconds;
60 s is an order of magnitude of headroom over that. The counter-case — two
*real* weigh-ins ten minutes apart — is 600 s, a full order of magnitude
*outside* the window, so both are stored.

**The residual false positive is larger than an earlier draft claimed** (F12).
That draft argued the only collapsed pair would report "the exact same gram
value", so "the two records would be byte-identical" and nothing is lost. **That
argument is for an exact-match key, not the tolerance actually specified.** With
±50 g, two genuinely different readings collapse whenever they land within
0.11 lb of each other — and 50 g is *larger than one display increment on a
0.1 lb scale* (45.36 g), so two consecutive readings one display step apart
collapse. The conclusion still stands (a 45 g difference between two weigh-ins
inside 60 s is noise, and sub-case 2 keeps the collapse non-lossy for
composition), but it stands on "the difference is negligible", not on "the
records are identical".

**This reasoning holds only for readings taken close together in time.** It does
*not* extend to a burst delivery of readings taken days apart — see the
receipt-time caveat below, which is the one place the tolerance is doing work it
was never designed for.

Requires an index on `weight_log(timestamp)` for the lookup to stay cheap as
history grows — additive, no data change.

### Accepted residual risk: the window is anchored to receipt time (F2, §6 D4)

`timestamp` is `datetime.now(timezone.utc)` at **server receipt**
(`app.py:80-81`); §3.1 declines to add `measured_at`, so no client value is
consulted. **The window therefore measures delivery time, not measurement time**,
and this design reasoned entirely about the same physical weigh-in arriving
twice. It never considered *different* weigh-ins arriving together.

The failure mode: a store-and-forward client (a phone out of BLE or network
range) buffers several days of weigh-ins and flushes them on reconnect. That
burst puts N **distinct** weigh-ins inside one 60-second window, and the only
thing separating them is the ±50 g tolerance — a quantity chosen to absorb unit
conversion rounding between two bridges, now acting as the sole discriminator
between two different days' measurements. Any collision **discards the later
reading permanently: no row, no Garmin push, no error, and a 200 with
`deduplicated: true` reported to the client as success.**

**JD's decision (§6 D4) is to accept this rather than add `measured_at`.** The
residual risk is therefore live and must be stated plainly rather than softened:

- **Silent.** Nothing logs it; the response is a success.
- **Permanent.** The reading is not stored anywhere and cannot be recovered.
- **Rare but non-zero.** It needs two buffered readings within ~0.11 lb of each
  other. That is not every flush — but because it is undetectable, it is
  undetectable *at any rate above zero*, and the rate rises with burst size.
- **Client-visible only as success**, which is why §4.5 rule 4 is rewritten to
  tell Bascule not to treat `deduplicated: true` as proof of durable storage and
  not to blind-flush a buffer. The correctness burden moves to a client in
  another repo — that is the cost of this option, chosen over adding
  `measured_at` and its knock-on work (the dedup key, the Garmin push timestamp,
  and untrusted client-clock input all change together).

This is a **known, accepted defect**, not an oversight. If Bascule's replay path
(the prompt's Phase 5 deliverable tracks it as milestone 7) becomes real, revisit
`measured_at` — it removes the anchor bug at the root.

### 3.8 Response shape

The existing response keys (`success`, `weight_lbs`, `weight_kg`, `timestamp`,
`synced_to_garmin`, optional `garmin_error`) are **unchanged**; existing clients
that read them keep working. Composition fields echo back only when supplied.
Full shapes in §4.

---

## 4. Bascule contract (hand-off ready)

> **This section is the deliverable for the parallel Bascule Android client
> effort.** It is pinned the moment Track A merges to `main`. Track B's fields
> (§4.2, §4.5) are pinned when Track B merges, and depend on §6 D1.

**Base URL:** the `vitalforge-weight` service (port 8085 / `weight.<domain>`).
**Endpoint:** `POST /api/weight`, `Content-Type: application/json`.

### 4.1 Authentication

```
Authorization: Bearer <VITALFORGE_API_TOKEN>
```

- The scheme is matched case-insensitively; the token value is compared exactly
  (after surrounding whitespace is stripped from both sides).
- The token is long-lived and has no expiry. Revocation = rotate the env var and
  restart the container. There is no refresh flow to implement.
- **The two credential types revoke independently, and neither implies the
  other** (F7). Before Track A there was exactly one lever that invalidated every
  credential in the system: rotating `VITALFORGE_SECRET` invalidates every
  outstanding `vf_session` cookie at once (`shared/auth.py:20`). After Track A
  that lever is **no longer complete** — a leaked bearer token survives a
  `VITALFORGE_SECRET` rotation untouched, and rotating `VITALFORGE_API_TOKEN`
  does not invalidate any cookie. An operator responding to a suspected
  compromise by rotating the secret (the obvious move) would leave the
  longer-lived, more powerful credential live. **Responding to a compromise means
  rotating both.** The README's Authentication section states both procedures.
- The token grants full API access to both services, including
  `DELETE /api/weight/{id}`. Combined with the no-expiry property, Track A
  introduces a credential that is strictly more powerful and strictly
  longer-lived than a session cookie (which expires in 30 days). Accepted for a
  single-operator deployment. **Follow-up worth JD's attention, not blocking:**
  Bascule only ever POSTs, so a write-scoped or POST-only token would be a
  tighter fit than full access including `DELETE`; the natural next step if scope
  ever matters is a read/write distinction rather than per-endpoint tokens.
- **Store it in `EncryptedSharedPreferences`.** Never plain
  `SharedPreferences`, never in source, never in logs or crash reports.
- `/health` requires no credential and is the correct connectivity probe.

### 4.2 Request — weight only

```json
{ "weight": 185.4, "unit": "lbs", "source": "bascule" }
```

`unit` is `"lbs"` or `"kg"` (default `"lbs"` if omitted). `source` is optional.

### 4.3 Request — full payload

```json
{
  "weight": 185.4,
  "unit": "lbs",
  "body_fat_pct": 18.4,
  "body_water_pct": 55.2,
  "muscle_pct": 40.1,
  "bone_mass_kg": 3.2,
  "source": "bascule"
}
```

Every composition field is independently optional; send only what the scale
actually measured. Omit a field entirely rather than sending `null` or `0`.

**Units are in the field names, and this is load-bearing.** `body_fat_pct`,
`body_water_pct`, and `muscle_pct` are **percentages** (`18.4`, never `0.184`).
`bone_mass_kg` is **kilograms**. Range validation will reject a fraction sent
where a percentage is expected, but **it cannot detect pounds sent where
kilograms are expected** — a 3.5 kg skeleton sent as `7.7` lands inside the
accepted range and is stored and pushed to Garmin wrong, permanently. The `_kg`
suffix is the only defense; do not strip it when mapping from the device SDK.

| Field | Type | Range | Unit |
|---|---|---|---|
| `weight` | number | > 0, and 2–500 after conversion to kg | per `unit` |
| `unit` | string | `"lbs"` \| `"kg"` | — |
| `body_fat_pct` | number | 3–75 | percent |
| `body_water_pct` | number | 30–80 | percent |
| `muscle_pct` | number | 10–90 | percent |
| `bone_mass_kg` | number | 0.5–10.0 | kilograms |
| `source` | string | `"pwa"` \| `"bascule"` \| `"bridge"` \| `"tasker"` | — |

**Unknown fields are rejected, not ignored.** Any key not in this table produces
a 422. This is deliberate: it makes client/server drift fail loudly at the first
request instead of silently discarding data for months.

### 4.4 Responses

**200 — success**

```json
{
  "success": true,
  "weight_lbs": 185.4,
  "weight_kg": 84.1,
  "timestamp": "2026-08-22T14:03:11.482913+00:00",
  "synced_to_garmin": true,
  "body_fat_pct": 18.4,
  "body_water_pct": 55.2,
  "muscle_pct": 40.1,
  "bone_mass_kg": 3.2,
  "source": "bascule"
}
```

Composition keys appear only when supplied. `timestamp` is server-assigned UTC
ISO-8601 — the client's clock is not consulted.

**200 — stored locally, Garmin push failed** (see §5.3)

```json
{
  "success": true,
  "weight_lbs": 185.4,
  "weight_kg": 84.1,
  "timestamp": "2026-08-22T14:03:11.482913+00:00",
  "synced_to_garmin": false,
  "garmin_error": "<message>"
}
```

**The data is stored. Do not retry.** A retry produces a duplicate row (or, if
inside the dedup window, a no-op).

`synced_to_garmin: false` means "this row's contents are not known to be in
Garmin." Two caveats, both of which an earlier draft glossed by calling it "a
reconciliation signal" (F3):

- **No reconciliation process exists.** Nothing in the system re-pushes a row
  with `synced_to_garmin = 0`, and nothing ever sets it to 1 later. The flag is
  a record of what happened at write time, not a work queue. Do not build client
  behavior on the assumption that the server will catch up.
- **It can be a false negative.** `add_body_composition` is one HTTP POST; if
  Garmin commits the upload but the response is lost (connection reset, read
  timeout), the client raises, the route catches it, and we store
  `synced_to_garmin: 0` for data Garmin actually holds. A retry in that state
  double-pushes. This is why the correct client behavior is *do not retry*.

**200 — duplicate collapsed** (see §3.7)

```json
{
  "success": true,
  "deduplicated": true,
  "id": 4171,
  "weight_lbs": 185.4,
  "weight_kg": 84.1,
  "timestamp": "2026-08-22T14:03:09.106522+00:00",
  "synced_to_garmin": true
}
```

May additionally carry `"enriched": true` or `"conflict": true`. `timestamp` is
the **original** row's, not the current request's.

> **`deduplicated: true` is not proof your reading was stored.** It means the
> server matched your payload to an existing row within ±50 g and 60 seconds of
> **receipt time** — not measurement time. If you deliver several readings taken
> at different times inside one 60-second burst, two of them landing within
> 0.11 lb of each other collapse into one, and **the second is discarded
> permanently with no error**. See §4.5 rule 4 before implementing any buffered
> or replay delivery.

**400 — invalid `unit`** *(the only 400 in the API)*

```json
{ "detail": "unit must be 'lbs' or 'kg'" }
```

`unit` is validated in the route rather than by the schema, so it alone returns
400 where every other validation error returns 422. This is a **legacy quirk
retained because changing it has no benefit** — not a backward-compatibility
guarantee. The only thing depending on it is this repo's own test, which asserts
the status and nothing about the body; no external client parses it (F8c).

The post-conversion weight bound (2–500 kg) returns **422**, like every other
Track B validation — see below.

> **Behavior change for the record:** the weight-range rejection is new. Today an
> absurd weight (`99999`) returns **200** with `synced_to_garmin: false`, because
> the value reaches `struct.error` inside the FIT encoder and is swallowed by the
> route (§1.4). This is the second deliberate change to an existing endpoint in
> these two tracks, alongside the 500→401 fix (§2.3), and like it ships with its
> own test in the same PR.

**401 — missing or invalid credential**

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{ "detail": "Not authenticated" }
```

Identical for a missing header, a wrong token, a malformed scheme, and an
expired cookie — the server deliberately does not say which. Not retryable
without new credentials.

> **Compatibility note for Bascule:** before Track A merges, this path returns
> `500` with a plain-text `Internal Server Error` body (§1.2 D1). Bascule should
> not ship logic that depends on the 500, and should treat a 401 as
> authoritative from the first Track A deploy onward.

**422 — schema validation failure**

FastAPI's standard shape. Unknown field:

```json
{ "detail": [ { "type": "extra_forbidden",
                "loc": ["body", "bodyFat"],
                "msg": "Extra inputs are not permitted",
                "input": 20 } ] }
```

Out-of-range value:

```json
{ "detail": [ { "type": "less_than_equal",
                "loc": ["body", "body_fat_pct"],
                "msg": "Input should be less than or equal to 75",
                "input": 99,
                "ctx": { "le": 75.0 } } ] }
```

Weight outside the 2–500 kg post-conversion bound. Note `loc` is `["body"]` — a
model-level validator, not a field-level one:

```json
{ "detail": [ { "type": "value_error",
                "loc": ["body"],
                "msg": "Value error, weight must be between 2 and 500 kg after unit conversion" } ] }
```

Also `"type": "missing"` (required field absent) and `"type": "float_parsing"`
(wrong type). `detail` is always an **array** on a 422 and always a **string** on
400/401/500 — parse accordingly, and note the array entries are objects: a client
that renders `detail` directly will print `[object Object]` unless it flattens
first. All shapes above were produced by running the model, not transcribed from
memory.

**500 — unexpected server error**

```json
{ "detail": "Internal Server Error" }
```

Retryable with backoff. Note that a Garmin outage does **not** produce a 500 —
it produces the 200 with `synced_to_garmin: false`.

### 4.5 Client rules

1. Treat 401 as terminal until the operator supplies a new token. Do not retry
   on a timer.
2. Treat 400 and 422 as terminal for that payload. Log the `detail` and drop or
   quarantine the reading — retrying an identical malformed body loops forever.
3. Retry only on 500, connection failure, and timeout, with backoff.
4. **Retry after a timeout is safe for a single in-flight reading — and only
   that.** The 60-second dedup window (§3.7) collapses a redelivery of the same
   weigh-in, so retrying one timed-out POST will not create a duplicate.

   **It is not safe to blind-flush a buffer.** The dedup window is anchored to
   **server receipt time**, not to when the weigh-in was taken — there is no
   `measured_at` field, and the server never consults your clock. So if you
   deliver a backlog in a burst, several *distinct* readings land inside one
   60-second window, and the ±50 g tolerance — which exists to absorb unit
   rounding between two bridges — becomes the only thing separating two
   different days' measurements. **Any pair within 0.11 lb of each other
   collapses, and the later one is discarded permanently: no row, no Garmin
   push, no error, and a `200` with `deduplicated: true` that looks exactly like
   success.**

   Concretely, for a store-and-forward or replay path:

   - **Do not treat `deduplicated: true` as proof of durable storage.** It is
     proof the server matched your payload against an existing row, nothing more.
   - **Do not drop your local copy on a `deduplicated: true` response** the way
     you safely can on a plain `200`.
   - **Space a backlog flush out** so each reading lands in its own window
     (>60 s apart). This is the reliable option.
   - Reconciling afterwards via `GET /api/weight/recent` works only for small
     backlogs: that endpoint is hardcoded to **`LIMIT 10`**
     (`vitalforge-weight/app.py:122`), so a client cannot see past the ten most
     recent rows and cannot detect a loss older than that. Do not build a
     reconciliation loop on it without first adding a `?limit=` parameter.

   This is a **known, accepted server-side limitation** (§6 D4), not a bug to
   report. It is documented rather than fixed because fixing it means adding a
   client-supplied `measured_at`, which changes the dedup key, the Garmin push
   timestamp, and this contract together. If Bascule's replay milestone makes
   burst delivery routine, raise it — that is the trigger to revisit.
5. Send `source: "bascule"`.
6. Omit unmeasured fields entirely. Never send `null`, `0`, or a sentinel.
7. Never log the `Authorization` header.
8. **Treat `conflict: true` as silent data rejection on the named fields, not a
   soft warning.** When a dedup match (§3.7) finds a field present on both the
   stored row and your POST with a *different* value, the server keeps the
   stored value and returns `conflict: true` plus `conflict_fields` (an array
   naming every field that conflicted — composition fields and `source` alike;
   response is otherwise still `200`, still `deduplicated: true`). Your
   submitted value for those fields was **not** stored and was **not**
   re-pushed to Garmin. This can happen on `source` too: if your POST's
   `source` differs from the stored row's, the original is kept and `source`
   appears in `conflict_fields` — do not assume a `conflict: true` response
   means your provenance tag was recorded. (Added 2026-08-22, Phase 4
   adversarial review finding — `conflict_fields` did not exist before this;
   earlier versions of this contract returned only the bare `conflict: true`
   boolean, with no way to tell which field(s) without server-log access.)

---

## 5. Failure-mode review

### 5.1 Token set but empty string

`VITALFORGE_API_TOKEN=""`, `VITALFORGE_API_TOKEN="   "`, and an unset variable
are made identical by `.strip()` at import: all three yield `_API_TOKEN == ""`
and `_bearer_token_valid` returns `False` at guard 1 before touching the header.
Bearer auth is simply disabled — the A2 configuration in §2.5.

This matters because of D3: `hmac.compare_digest("", "")` is verifiably `True`.
Without guard 1, `VITALFORGE_API_TOKEN=` in a `.env` file plus a request with a
header the parser reduces to an empty value would authenticate **anyone**. Two
independent guards (`not _API_TOKEN`, `not value`) cover it, and the matrix has a
cell for each (A2-C3, A1-C5).

The trailing-newline case (`VITALFORGE_API_TOKEN=abc\n` from a file-based secret
mount) is why the env var is stripped rather than used raw — otherwise every
client would fail with an unfalsifiable 401.

### 5.2 Header injection

- **CR/LF injection** is impossible at our layer: `h11`/`uvicorn` reject headers
  containing bare CR or LF during parsing, so a crafted header never reaches
  Starlette. We add no header parsing of our own beyond `partition(" ")`.
- **Response splitting** cannot originate here: the 401 response body and headers
  are compile-time constants and never interpolate any request value (§2.3).
- **Log injection** cannot originate here: `shared/auth.py` logs nothing on any
  auth path (§2.4).
- **Non-ASCII / unicode token** (`Authorization: Bearer tökén`) — this is the one
  input that *would* have produced a 500 via `TypeError` (D2). Comparing
  `.encode("utf-8")` bytes makes it a clean `False` → 401. Cell A1-C9. This is an
  explicitly named Phase 4 adversarial-review target, so it is covered by design
  rather than discovered there.
- **Multiple `Authorization` headers** — Starlette's `headers.get()` returns the
  **first** occurrence (verified). Only that one is compared: a valid token in a
  second header is ignored, and a junk first header masks a valid second one.
  This is deterministic rather than fail-closed, so it is worth an explicit test
  — smuggling a second header past a proxy cannot grant access, but neither does
  the design silently prefer whichever header happens to be valid.
- **Scheme confusion** (`Basic <token>`, bare `<token>`, `Bearer,<token>`) — the
  scheme is matched exactly (case-insensitively) against `bearer`; anything else
  returns `False` before the token is compared. Cell A1-C6.
- **Method confusion** — the middleware is method-agnostic and matches on path
  prefix only, so there is no verb that skips the check. `HEAD /api/...` and
  `OPTIONS /api/...` are authenticated exactly like `GET`.
- **Path-prefix confusion** — the exempt list is `/auth/`, `/health`, `/static/`.
  `/api/` is not special-cased *into* auth; it only selects 401-vs-302 for
  already-failed auth. A path like `/API/weight` (uppercase) would not match
  `/api/` and would get a 302 instead of a 401 — cosmetic, not a bypass, since
  it also would not match any route.

### 5.3 Garmin: weight succeeds, composition fails

**The premise as posed does not hold — but an earlier draft over-claimed the
correction, and the DA falsified that too (F3).** Stating it precisely:

**What is true.** §1.5 G3 verified that `add_body_composition` builds one FIT
`weight_scale` record and POSTs it as a single multipart upload. **Weight and
composition cannot fail independently *within a single upload*.** That is a
statement about transport atomicity, and it holds.

**What an earlier draft wrongly inferred from it** was that partial success is
impossible full stop, and used that to close off analysis. Three paths reopen it:

- **Path A — this design's own enrichment re-push.** §3.7 sub-case 2: POST 1
  uploads weight successfully; POST 2 enriches and re-pushes. If that second
  upload fails, Garmin holds the weight and not the composition. §3.7 even
  specifies the handling (`synced_to_garmin` back to 0). The design implements
  the state this section called impossible — two sections of one document
  contradicting each other, with the false one load-bearing for a decision.
- **Path B — the lost acknowledgement.** One HTTP POST is atomic in *transport*,
  not in *acknowledgement*. If Garmin commits the upload but the response is lost,
  we record `synced_to_garmin: 0` for data Garmin holds. Under Track B this is
  worse than before: the row is now inside a dedup window, so the next POST hits
  sub-case 2, enriches, and re-pushes — producing a double Garmin record from a
  path nobody modelled. **Stated behavior: no change, and deliberately so.** A
  false negative on `synced_to_garmin` is the safe direction to be wrong, nothing
  reconciles it (§4.4), and a retry would double-push. This is documented rather
  than fixed.
- **Path C — acceptance is not ingestion.** Atomic upload says nothing about
  Garmin's server-side handling of individual FIT fields. §3.5 implicitly relies
  on it being able to accept some and not others. Nothing detects that:
  `synced_to_garmin: 1` is set on HTTP success, so a silently-dropped field reads
  as fully synced.

The partial-success axis that **does** exist is the one already in the code:
**Garmin push fails entirely, local write succeeds.** Behavior is unchanged from
today — the exception is caught, `synced_to_garmin: 0` is stored, and the
response carries `synced_to_garmin: false` plus `garmin_error`. With Track B, the
composition columns are still written; the local database is the complete record
and Garmin is the lagging replica. The response shape is in §4.4.

**We deliberately do not add a weight-only retry fallback** (catch the failure,
strip the composition fields, push again). **This conclusion survives the
correction above** — the DA attacked the premise and explicitly did not ask for
the fallback. It would produce a Garmin record that disagrees with our stored
row, with no marker distinguishing "composition never sent" from "composition
rejected". The only failure mode such a retry could rescue is a composition value
the FIT encoder cannot pack (G4), and §3.2's range bounds already make that
unreachable. If a real-world Garmin-side rejection of composition surfaces at the
Phase 3 live checkpoint, revisit with evidence.

Path A above means the enrichment re-push cannot be assessed independently of
this section — see §3.7's enrichment caveat and `01-plan.md` B4.

### 5.4 Migration interrupted mid-run

Scenario: the container is killed during its first boot after the upgrade, part
way through `init_db()`.

`init_db()` is awaited in the lifespan **before the app serves any request**, so
there is no window in which a half-migrated schema is visible to a client of the
service doing the migration.

Because each `ALTER TABLE ADD COLUMN` auto-commits independently (verified,
§1.3), a kill leaves a prefix of the columns applied and the remainder absent —
never a torn column. On the next boot, the applied ones raise
`duplicate column name` and are swallowed, the remainder are applied, and the
schema converges. Re-running to convergence is the design's normal path, not a
recovery path.

The cross-service case: while the weight service is mid-migration, the dashboard
may already be serving on the old image. It is unaffected — every one of its
queries names columns explicitly (§3.3), and additive nullable columns are
invisible to them. The reverse (dashboard migrates first, weight service still
old) is symmetric.

The concurrent case: both containers start together, both attempt the same
`ADD COLUMN`. One wins, the other gets `duplicate column name` and swallows it.
This is exactly why the guard is attempt-and-swallow rather than
`PRAGMA table_info` — a pre-check would let both observe "absent" and one would
then raise an unhandled error at boot.

The lock case: if the second container cannot acquire the write lock within
`aiosqlite`'s connect timeout, `database is locked` propagates, the lifespan
fails, and Docker restarts the container — the correct outcome. It is explicitly
**not** swallowed.

Rollback: additive nullable columns are readable by the previous image, so
rollback is a plain image redeploy with no data step. Phase 1's per-package
rollback verification should confirm this by pointing the previous image at a
migrated fixture DB.

Residual risk, stated honestly: `ADD COLUMN` here is O(1) because none of the new
columns has a non-constant `DEFAULT`. Any future migration that adds a defaulted
column rewrites the table and reintroduces a real interruption window. That
constraint should be written into `shared/database.py` as a comment at
implementation time.

### 5.5 Two bridges POSTing the same weigh-in seconds apart

Fully specified in §3.7. Summary: dedup keyed on
`abs(weight_grams − incoming) <= 50` — a tolerance, not an exact match, because
the two bridges may send the same weigh-in in different units and the conversion
rounds differently — within a **60-second** window, ignoring `source`. The DB
read happens *before* the Garmin push so an ordinary duplicate is never pushed
twice; a hit returns **200 with `deduplicated: true`** rather than 409, because a
4xx makes bridges retry. Two genuine weigh-ins ten minutes apart are 600 s apart
and both stored.

**The naive version of this does not work, and that was a blocking review
finding.** Check-then-act across `await` points lets two concurrent POSTs both
observe "no duplicate" and both push — failing the exact scenario the feature
exists for, on a single uvicorn worker, via asyncio interleaving alone. §3.7 now
specifies `BEGIN IMMEDIATE` spanning the duplicate `SELECT` and **whichever write
follows it** — `INSERT` for a new row, `UPDATE` for an enrichment (§6 D5).
Scoping it to the `INSERT` alone would have left the enrichment sub-case racing
in exactly the same way.

The ordering is **read → write → commit → push → update-flag**: the Garmin call
sits *outside* the transaction, so the lock covers only local DB work and needs
no timeout mechanism to bound it. This reverses the current route's
push-before-write ordering for every request, not just duplicates — see §1.4.

**Two limits of this answer, both accepted and documented rather than fixed:**
the window is anchored to *receipt* time, so a burst delivery can silently
discard distinct readings (§3.7's residual-risk block, §6 D4); and the ±50 g
tolerance means collapsed records are *not* byte-identical, so the justification
is "the difference is negligible", not "nothing is lost".

The one lossy sub-case — a weight-only duplicate arriving either side of a full
payload — is handled by in-place enrichment plus a re-push at the original
timestamp, so composition is never silently discarded. That re-push is the single
place the design knowingly sends two Garmin uploads for one weigh-in; §3.7's
enrichment-push caveat reconciles it against §5.3 and names the Phase 3 live
checkpoint that settles whether Garmin collapses them.

### 5.6 Additional failure modes found while designing

- **Unbounded weight → `struct.error` → 500-shaped failure.** Pre-existing
  (§1.4); closed by the 2–500 kg bound (§3.2).
- **Non-ASCII password → 500 on `POST /auth/login`.** Pre-existing (D2). Same
  root cause as the token issue and a one-line fix in `check_credentials`.
  Recommend fixing it in Track A's auth PR since the file and the reasoning are
  already open; flag it if the scope is unwelcome.
- **`/api/weight/trend` compares an ISO-8601 `+00:00` timestamp against
  SQLite's `datetime('now')`** (`app.py:146`), which emits
  `YYYY-MM-DD HH:MM:SS`. The string comparison works only because `'T' > ' '`.
  Pre-existing, out of scope for both tracks, recorded so it is not mistaken for
  something Track B introduced.

---

## 6. NEEDS JD DECISION — all resolved 2026-08-22

D1–D3 were raised by this design and confirmed as-is. **D4 and D5 were raised by
the Phase 2 devil's-advocate review** (`02-validation.md`, the two blocking
findings) and decided by JD; unlike D1–D3, they *did* require design changes,
which are folded into §3.7, §4.4, and §4.5.

### D4 — CONFIRMED: Option 2. Receipt-time dedup — add `measured_at`, or document the risk?

**The finding (F2, blocking).** Dedup's 60-second window is anchored to server
receipt time, not measurement time, because §3.1 declined to add `measured_at`.
A store-and-forward client flushing a backlog puts distinct weigh-ins inside one
window, where the ±50 g tolerance becomes the only discriminator between
different days' readings — and a collision discards one permanently, silently,
reported to the client as success.

**Option 1** — add optional client-supplied `measured_at`, key dedup on it, store
it in a sixth nullable column. Removes the bug at the root. Costs more than one
column: it must also become the pushed Garmin timestamp (or the local row and the
Garmin record disagree about when the weigh-in happened), and it introduces
untrusted clock input needing its own bounds.

**Option 2 — CHOSEN.** Keep `measured_at` out of scope; rewrite the Bascule
contract so the client knows `deduplicated: true` is not proof of durable storage
and must not blind-flush a buffer. **This accepts a real, silent, permanent data
loss risk on burst delivery** — documented in §3.7's residual-risk block and
§4.5 rule 4 rather than softened. Revisit if Bascule's replay milestone makes
burst delivery routine.

### D5 — CONFIRMED: Option A. How to make dedup atomic?

**The finding (F1, blocking).** Dedup as originally specified is TOCTOU-racy and
fails its own motivating scenario — demonstrated, not argued.

**Option A — CHOSEN.** `BEGIN IMMEDIATE` on one connection spanning the duplicate
check and the write that follows it. **Option B** (rejected) — a partial unique
index on a quantised `(weight_grams, timestamp)` bucket, letting SQLite arbitrate.

**Refined twice after the initial call, both times by fix-verification
(`02b-fix-verification.md`), neither reopening D5's mechanism:**

1. **Scope.** The first revision bounded the transaction to "the `SELECT` and the
   row `INSERT`", which covers one of three sub-cases. Sub-case 2 (enrichment) is
   a read-modify-write with the identical race. The transaction now spans the
   `SELECT` and **whichever write follows** — `INSERT` or `UPDATE`.
2. **Ordering.** The first revision kept read → push → write, putting the Garmin
   upload inside the transaction, and made that acceptable by requiring an
   explicit call timeout. **No such timeout exists**: neither
   `add_body_composition` nor `Garmin.__init__` takes one, the call bottoms out
   in garth's session (adjacent to the protected auth flow), and it is
   synchronous, so `asyncio.wait_for` cannot bound it either. The ordering is now
   **read → write → commit → push → update-flag**, which removes the need for any
   timeout mechanism because the lock never spans the network call.

The earlier warning against moving the push after the commit — that it strands
rows at `synced_to_garmin = 0` with no repair path — **was explicitly retracted by
its author** in `02b-fix-verification.md`: it holds only if nothing comes back to
set the flag, and step 6 does. The residual exposure is a crash in the sub-second
window between push and flag-update, which is far narrower than holding a write
lock across an unbounded network call.

---

### D1–D3 (raised by this design, confirmed as-is)

No design changes were required for these — §3.1, §3.4, §4.3, and §7 already
assumed these outcomes; they are kept for the record.

### D1 — CONFIRMED: Option A. Muscle: percentage or mass? *(blocks the Bascule full-payload contract)*

`garminconnect` has **no muscle-percentage field**. `add_body_composition`
accepts `muscle_mass` in **kilograms** (§1.5 G1). The task brief specifies
"muscle %". One of the two has to give, and the choice is visible in the wire
contract, so Bascule cannot finalize the full-payload form until it is settled.

**Option A — wire field `muscle_pct`, server derives the mass.**
`muscle_mass_kg = weight_kg × muscle_pct / 100`.
*For:* matches what BIA scales (BF720 included) actually report, so the bridge
forwards the device value unmodified with no client-side arithmetic to get wrong.
*Against:* the stored percentage and the pushed mass can disagree if the derived
value is ever recomputed against a different weight; a reader of the Garmin
record cannot tell the mass was derived.

**Option B — wire field `muscle_mass_kg`, 1:1 with Garmin.**
*For:* no derivation, no ambiguity, exact parity with the upstream field.
*Against:* pushes the conversion into every client, where a wrong body weight or
a lbs/kg slip produces silently wrong data — the exact failure the `_kg` naming
convention exists to prevent.

**This design provisionally assumes Option A** so Phase 1 is not blocked. §3.1,
§3.4, and §4.3 are all written against it. Switching to Option B changes one DTO
field name, one bound, one mapping row, and one contract table.

### D2 — CONFIRMED: keep. Should the `weight_log` composition columns exist at all, given §3.5?

Composition reaches the dashboard via `weight_history` (Garmin round-trip), not
via `weight_log`. That makes `weight_log`'s composition columns a local
write-only record. They are still worth having — they are the only evidence of
what we *sent* when a Garmin push fails, and the only source for the enrichment
logic in §3.7 sub-case 2 — but if JD would rather not carry duplicated data,
dropping them would simplify Track B measurably (no dedup enrichment, fewer
columns, less migration). **Recommendation: keep them**, for the failure-forensics
reason. Confirm.

This note previously said the decision might be made for us — that if Garmin's
read path lacked water/muscle/bone, `weight_log` would become the *only* source
for dashboard composition and the columns would be mandatory. **That branch is
now ruled out** (§3.5, resolved 2026-08-22): the keys exist. "Keep" therefore
stands on its own merits — the failure-forensics argument above — rather than by
necessity.

### D3 — CONFIRMED: include. Fix the pre-existing non-ASCII password 500 (D2 in §1.2) inside Track A?

It is a two-line change in `check_credentials` (compare bytes), in a file Track A
is already editing, with the same root cause as the token fix. But it is
unrelated to bearer auth and expands a PR that Bascule is blocked on.
**Recommendation: include it** — the reasoning is already loaded and a separate
PR for two lines costs more than it saves. Confirm, or it ships as a follow-up.

**Not escalated, though it looked like a candidate:** the
`PASS unset × TOKEN set` matrix cell (A3). It is resolved by the "existing
clients unaffected" ground rule — see §2.5 A3 for the reasoning. The Phase 2 DA
upheld the decision and added the missing mitigation (F6, the §2.4 startup
warning), while noting the process point: an ambiguous matrix cell was resolved
in JD's favour without asking JD. Recorded so Phase 4 does not re-litigate it.

---

## 7. Exit gate assessment

**Gate 1 — Behavior matrix complete (all cells): PASS.**
40 of 40 cells specified in §2.5 — four configurations (`VITALFORGE_PASS`
set/unset × `VITALFORGE_API_TOKEN` set/unset) × ten credential forms, including
the empty-value, wrong-scheme, non-ASCII, and both-credentials-presented cases.
The requirement was "all 8+ cells". Every cell has a stated principal and a
stated result for both `/api/*` and HTML paths. The one cell-group that could be
argued either way (A3) is resolved by an explicit ground rule with the reasoning
recorded, not left ambiguous.

**Gate 2 — Migration plan mentally tested against "container killed during first
boot after upgrade": PASS.**
Analysed in §5.4 against four distinct interruption scenarios: mid-`init_db()`
kill, cross-service version skew, concurrent DDL from both containers, and write-
lock contention. The analysis rests on two behaviors verified by running SQLite
rather than assumed — `ADD COLUMN` auto-commits without an explicit `commit()`,
and the duplicate error text is exactly `duplicate column name: <name>` — plus
the O(1) no-default property that makes the interruption window negligible. The
attempt-and-swallow guard was chosen *because* `PRAGMA table_info` is TOCTOU-racy
under this repo's two-container-one-file topology. Rollback safety is established
by the fact that every existing query names its columns explicitly.

**Gate 3 — Bascule contract doc ready to hand off as-is: PASS (updated 2026-08-22,
D1 confirmed).**

§6 D1 is resolved: Option A (`muscle_pct`, server-derived mass) is confirmed, not
provisional. §4.3's full-payload contract, including the muscle field, its bounds,
and its mapping to Garmin's `muscle_mass` kwarg, is final and may be handed to the
Bascule effort as-is — no field names remain pending. The weight-only form (§4.2)
was already complete. Nothing in §4 is blocked.

---

## 8. Notes for Phase 1

Not part of the Phase 0 deliverable; recorded so the planning phase does not
rediscover them.

- **Behavior-matrix tests must monkeypatch `shared.auth` module attributes**
  (`_PASS`, `_API_TOKEN`), not just env vars — the globals are read at import
  (§1.7). Tests that vary `_SECRET` must also rebuild `_serializer`.
- **`FakeGarminClient.add_body_composition` discards `**kwargs`**
  (`conftest.py:45-47`). Until it captures them, every Track B mapping assertion
  is vacuously true. This is the highest-value single line to change first.
- ~~**`tests/fixtures/garmin/weigh_ins.json` has no water/muscle/bone keys.**~~
  **Resolved 2026-08-22** — real response captured, keys confirmed
  (`bodyWater`/`boneMass`/`muscleMass`), fixture updated (§3.5). The new
  implementation note in its place: the fixture's values are **synthetic**
  because the live account's are all `null` until Track B's first push, so the
  null path needs a test of its own (`01-plan.md` B5).
- ~~**A migration fixture DB matching the current production schema does not
  exist in the repo.**~~ **Resolved 2026-08-22** —
  `tests/fixtures/production_schema.sql` captured read-only from the live volume,
  structure only, and confirmed to match `shared/database.py` with **zero drift**.
  Note `scripts/seed_db.py` is *not* a starting point: it never inserts into
  `weight_log`, the one table Track B migrates. Loading details and the
  `sqlite_sequence` gotcha are in `01-plan.md` §4.1.
- **CI already runs `ruff` + `pytest` + Playwright** as a gating `test` job
  (§1.7). Phase 2's "CI pinned" item mostly exists; `mypy` is not configured and
  adding it is out of scope ("extend, don't replace").
- **Track A touches no `app.py`.** Track B touches both. Per CLAUDE.md, any
  `shared/` change requires re-checking both services.
