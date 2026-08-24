# Plan: Phase 5 security fixes (session secret default, TLS-aware cookie)

## Summary
Closes the two items escalated to JD in `docs/prp/04-review-findings.md` (Codex findings
#1 and #3) that are blocking Phase 5 (Release gate) of `docs/prp/vitalforge-agent-prompt.md`.
Both live entirely in `shared/auth.py`, the auth module both `vitalforge-weight` and
`vitalforge-dashboard` import. Two independent fixes, one plan, one PR: (1) never sign
session cookies with the source-visible default secret, and (2) mark the session cookie
`Secure` when the real client-facing connection is HTTPS.

## User Story
As the operator of a live VitalForge deployment,
I want the session-signing secret to never silently fall back to a public, source-visible
value, and the session cookie to be marked `Secure` whenever the client connection is
actually HTTPS,
So that a forged session (from anyone who's read this public repo) or a plaintext-network
cookie interception can't be used to reach real health data.

## Problem → Solution

**Secret default:** `_SECRET = os.environ.get("VITALFORGE_SECRET", "default-dev-secret")`
(`shared/auth.py:14`). If `VITALFORGE_SECRET` is unset in production, `_SECRET` is a
literal string visible in this public repo's source and README — anyone can forge a valid
signed session cookie for any username via
`URLSafeTimedSerializer("default-dev-secret").dumps({"user": "anyone"})`, and
`validate_session` never checks the returned username against `VITALFORGE_USER`. →
**Auto-generate a random secret per-process when the configured value is missing or still
the placeholder, and log a loud warning** (JD's choice — see Risks for the one real
trade-off this introduces).

**No TLS awareness:** the session cookie is set with `httponly=True, samesite="lax"` but no
`secure` flag (`shared/auth.py:193`), so it's sent over plaintext HTTP even when JD's
production deployment is behind a TLS-terminating reverse proxy/tunnel (confirmed:
production *is* behind TLS upstream, but this repo can't see that from `docker-compose*.yml`
alone — neither compose file references `nginx/nginx.conf`, per `CLAUDE.md`). →
**Compute whether the client-facing connection was HTTPS per-request** (trusting
`X-Forwarded-Proto` first, falling back to `request.url.scheme`) **and set `secure=` on
that cookie accordingly.** No new env var, no deployment-side config needed — see Approach
below for why this is safe to do unconditionally.

## Metadata
- **Complexity**: Small (1 core file + tests + docs, no new dependencies, no schema change)
- **Source PRD**: `docs/prp/04-review-findings.md` (Codex findings #1, #3) — not a PRD with
  phases, treated as free-form input per Phase 0's detection table
- **PRD Phase**: N/A
- **Estimated Files**: 5 (`shared/auth.py`, `tests/test_auth_token.py`,
  `tests/test_auth_middleware.py`, `README.md`, `.env.example`)

---

## UX Design

Internal change — no user-facing UX transformation. The login page, its form, and the
redirect flow are all unchanged. The only user-visible effect is that a session created
over HTTP now works exactly as it does today (no `Secure` flag, since `secure=` evaluates
false there), and a session created over the HTTPS-terminating proxy now gets a cookie the
browser will only replay over HTTPS — invisible unless you inspect response headers.

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| Service startup, `VITALFORGE_SECRET` unset | Silently signs with a public, source-visible default | Generates a random secret for this process, logs a WARNING naming the risk | See Risks: breaks cross-service SSO until set explicitly |
| `POST /auth/login` response, HTTP client | `Set-Cookie: vf_session=...; HttpOnly; SameSite=lax` | Same | No change — `secure=False` when `X-Forwarded-Proto`/`request.url.scheme` isn't `https` |
| `POST /auth/login` response, HTTPS-fronted client | `Set-Cookie: vf_session=...; HttpOnly; SameSite=lax` (missing `Secure`) | `Set-Cookie: vf_session=...; HttpOnly; SameSite=lax; Secure` | Browser now refuses to replay this cookie over a plaintext connection |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `shared/auth.py` | 1-22 | Module-level secret/config resolution — exactly what's being changed |
| P0 | `shared/auth.py` | 184-194 | The `login` route — where `set_cookie` is called, where `secure=` gets added |
| P0 | `shared/auth.py` | 24-32 | `_warn_if_misconfigured()` — the existing pattern for a startup-time warning; mirror its style and its "called once at import, independently callable for tests" shape |
| P1 | `tests/test_auth_token.py` | 1-70, 113-139 | Existing test fixtures (`set_token`) and the precedent for testing an import-time-triggered warning by calling the function directly after `monkeypatch.setattr` on the module's globals — **do not** try to re-import the module or manipulate `os.environ` mid-test, this codebase's established pattern avoids that entirely |
| P1 | `tests/test_auth_middleware.py` | 1-50 | `_build_matrix_app()` / `matrix_client` fixture and `configured_auth` fixture — the pattern for a throwaway FastAPI app wired with `add_auth_routes` for isolated middleware/route tests, used for the new `secure=` cookie test |
| P2 | `README.md` | 90-107, 159-179 | Env var table and Authentication section — both need a sentence added, not restructured |
| P2 | `.env.example` | 1-13 | `VITALFORGE_SECRET` line and its comment |

## External Documentation
No external research needed — feature uses established internal patterns (`itsdangerous`,
`os.environ`, Starlette `Request`/`Response.set_cookie`, all already used elsewhere in this
file) plus Python stdlib `secrets.token_urlsafe`, already used identically in this repo's
own README (`Generate a random secret: python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).

---

## Patterns to Mirror

### STARTUP_WARNING_PATTERN
// SOURCE: shared/auth.py:24-32
```python
def _warn_if_misconfigured():
    if _API_TOKEN and not _PASS:
        logger.warning(
            "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty — "
            "auth is DISABLED and the token is inert. Set VITALFORGE_PASS to enable auth."
        )


_warn_if_misconfigured()
```
A plain module-level function, called once at import time, that logs via the module
`logger` (never raises, never blocks startup). New code (`_resolve_secret`) follows the
same shape: a plain function called once at import time to compute `_SECRET`, warning via
`logger.warning` when it takes the fallback path.

### TESTING_IMPORT_TIME_BEHAVIOR
// SOURCE: tests/test_auth_token.py:114-119
```python
def test_startup_warns_when_token_set_and_pass_empty(monkeypatch, caplog):
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "sometoken")
    monkeypatch.setattr(shared_auth, "_PASS", "")
    with caplog.at_level(logging.WARNING):
        shared_auth._warn_if_misconfigured()
    assert "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty" in caplog.text
```
Never re-import the module or touch `os.environ` in a test — call the already-imported
function directly (optionally after `monkeypatch.setattr` on a *different* module global it
reads). For `_resolve_secret`, this is even simpler: make it a pure function of its
argument (`_resolve_secret(configured: str) -> str`), so tests call it directly with a
literal string — no monkeypatching needed at all.

### THROWAWAY_APP_FIXTURE
// SOURCE: tests/test_auth_middleware.py:17-38
```python
def _build_matrix_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)

    @app.get("/api/thing")
    async def api_thing():
        return {"ok": True}
    ...
    return app


@pytest.fixture
async def matrix_client():
    transport = ASGITransport(app=_build_matrix_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def configured_auth(monkeypatch):
    """VITALFORGE_PASS set, no token -- auth is on, plain cookie auth."""
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "")
```
Reuse `matrix_client` and `configured_auth` as-is for the new `secure=` cookie tests — they
already exercise `/auth/login` (see `test_auth_login_with_valid_bearer_redirects_to_root`
in `tests/test_auth_middleware.py` for a `POST /auth/login`-shaped test to mirror, adjusted
to check response headers instead of the redirect).

### LOGGING_NO_SECRET_LEAKAGE
// SOURCE: tests/test_auth_token.py:137-139 (`test_startup_warning_contains_no_token_value`)
The existing warning test suite explicitly asserts a warning message never contains the
secret/token value itself. Mirror this for the new secret warning: assert the generated
random value never appears in `caplog.text`.

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `shared/auth.py` | UPDATE | Replace the literal default-secret fallback with `_resolve_secret()`; add `_request_is_https()` and wire `secure=` into the `login` route's `set_cookie` call |
| `tests/test_auth_token.py` | UPDATE | New tests for `_resolve_secret` (generates random when default, passes through when configured, never logs the generated value, generates a *different* value each call) |
| `tests/test_auth_middleware.py` | UPDATE | New tests for `secure=` on the login response cookie: absent with no forwarded-proto header, present with `X-Forwarded-Proto: https` |
| `README.md` | UPDATE | `VITALFORGE_SECRET` row gets a one-line note on the auto-generate-with-warning behavior and the cross-service-SSO consequence (see Risks); Authentication section unchanged otherwise |
| `.env.example` | UPDATE | Strengthen the `VITALFORGE_SECRET` comment to explain what happens if left unset (not silently insecure anymore, but not free either) |

## NOT Building
- No new environment variable for the TLS/proxy behavior — trusting `X-Forwarded-Proto`
  unconditionally is safe here (see Approach) and needs no operator configuration.
- No shared-secret file on the Docker volume to keep both services' auto-generated secrets
  in sync when `VITALFORGE_SECRET` is unset — documented as a known trade-off instead (see
  Risks), not solved. The fix for it is the one-line `.env` change the warning already asks
  for.
- No change to `VITALFORGE_USER`/username validation, no change to `check_credentials`, no
  change to the bearer-token path (`_bearer_token_valid`) — out of scope, unrelated to
  either finding.
- No `uvicorn --forwarded-allow-ips` / `ProxyHeadersMiddleware` configuration — the fix
  reads `X-Forwarded-Proto` directly in application code instead of depending on uvicorn's
  own (IP-allowlist-gated) proxy-header handling, so it works regardless of how the
  Dockerfile invokes uvicorn.

---

## Approach

**Secret resolution — auto-generate, don't fail startup (JD's decision).** Extract a pure
function `_resolve_secret(configured: str) -> str` that returns `configured` unchanged
unless it equals the known placeholder default, in which case it generates
`secrets.token_urlsafe(32)` and logs a warning. Called once at import time exactly like
`_warn_if_misconfigured()` already is.

**Alternative considered and rejected:** fail startup (raise/`sys.exit`) when the secret is
still the default. Rejected per JD's explicit choice — this session can't verify whether
production's `.env` currently sets `VITALFORGE_SECRET` (README lists it as not required),
so a hard failure risks bricking the next deploy. Auto-generate can't make the app refuse
to boot.

**TLS detection — read the header directly, don't rely on uvicorn's proxy-header handling.**
`_request_is_https(request) -> bool` checks `request.headers.get("x-forwarded-proto", "").lower()
== "https"`, falling back to `request.url.scheme == "https"` if the header is absent (covers
a hypothetical direct-HTTPS deployment with no proxy in front). This is deliberately
*independent* of uvicorn's built-in `ProxyHeadersMiddleware`/`--forwarded-allow-ips`, which
would require verifying and likely changing both Dockerfiles' `CMD` and trusting the right
IP range for whatever proxy JD runs — none of which this session can verify. Reading the
header directly in application code needs zero deployment changes.

**Why trusting `X-Forwarded-Proto` unconditionally (no IP allowlist) is safe for this
specific use:** the only thing this value controls is whether the `Secure` attribute gets
added to `Set-Cookie`. Browsers independently refuse to *store* a `Secure`-flagged cookie
that arrived over an actual plaintext connection (RFC 6265bis; verified real-browser
behavior, not just spec text) — so if an attacker with direct network access to the
published ports spoofs `X-Forwarded-Proto: https` on a real plaintext request, the
resulting `Secure` cookie simply gets rejected by the browser, breaking that one login
attempt. It does not grant access to anything, downgrade any existing protection, or let
an attacker read/forge a cookie they couldn't already forge. Worst case is a self-defeating
functional failure, not a security bypass — the asymmetry that makes the unconditional
version acceptable instead of needing an IP-allowlist mechanism.

**Alternative considered and rejected:** add `VITALFORGE_TRUST_PROXY_HEADERS` as an opt-in
env var, default off. Rejected as unnecessary ceremony given the risk analysis above shows
no real security downside to reading the header unconditionally, and JD confirmed
production is already behind TLS — an opt-in flag would just be one more thing to remember
to set for no safety benefit.

---

## Step-by-Step Tasks

### Task 1: `_resolve_secret` — random secret instead of the public default
- **ACTION**: Replace `shared/auth.py:14`'s direct assignment with a function + call.
- **IMPLEMENT**:
  ```python
  import secrets  # new import, alongside the existing hmac/logging/os/time block

  _DEFAULT_SECRET_SENTINEL = "default-dev-secret"


  def _resolve_secret(configured: str) -> str:
      """Never actually sign sessions with the public default -- generate a
      random secret and warn instead. Every VitalForge deployment that
      hasn't set VITALFORGE_SECRET currently uses this exact literal
      string, which is visible in this repo's own source and README, so
      relying on it lets anyone forge a valid session cookie."""
      if configured != _DEFAULT_SECRET_SENTINEL:
          return configured
      generated = secrets.token_urlsafe(32)
      logger.warning(
          "VITALFORGE_SECRET is unset (or still the placeholder default) -- "
          "generated a random secret for THIS PROCESS instead of using the "
          "public default. Every existing session cookie is now invalid, "
          "this will happen again on every restart, and (if you run both "
          "services) they will each generate a DIFFERENT secret, breaking "
          "single sign-on between them, until you set VITALFORGE_SECRET in "
          ".env -- see README's Environment Variables section."
      )
      return generated


  _SECRET = _resolve_secret(os.environ.get("VITALFORGE_SECRET", _DEFAULT_SECRET_SENTINEL))
  _serializer = URLSafeTimedSerializer(_SECRET)
  ```
- **MIRROR**: STARTUP_WARNING_PATTERN above (`_warn_if_misconfigured`'s shape and tone).
- **IMPORTS**: `import secrets` — stdlib, no new dependency, already used identically in
  this repo's own README instructions and in `vitalforge-weight/app.py`'s existing
  patterns for generating tokens elsewhere.
- **GOTCHA**: Keep `_resolve_secret` a pure function of its single argument — no reads of
  `os.environ` or other module globals inside it. This is what makes it directly
  unit-testable without any monkeypatching (see TESTING_IMPORT_TIME_BEHAVIOR).
- **VALIDATE**: `pytest -q tests/test_auth_token.py -k resolve_secret -v`

### Task 2: `_request_is_https` and wiring `secure=` into the login cookie
- **ACTION**: Add a helper and use it in the `login` route.
- **IMPLEMENT**:
  ```python
  def _request_is_https(request: Request) -> bool:
      forwarded = request.headers.get("x-forwarded-proto", "").lower()
      return forwarded == "https" or request.url.scheme == "https"
  ```
  Then in `login` (`shared/auth.py:193`), change:
  ```python
  response.set_cookie(_COOKIE_NAME, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax")
  ```
  to:
  ```python
  response.set_cookie(
      _COOKIE_NAME, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax",
      secure=_request_is_https(request),
  )
  ```
- **MIRROR**: N/A — new helper, no existing precedent in this file for header-based
  decisions; keep it as small and single-purpose as `_bearer_token_valid` is.
- **IMPORTS**: None new — `Request` is already imported.
- **GOTCHA**: Starlette lower-cases header names when matching via `request.headers.get`,
  but the *value* (`"https"`/`"HTTPS"`/`"Https"`) is attacker/proxy-controlled and not
  guaranteed lower-case — hence `.lower()` on the value, not just relying on the header-name
  lookup being case-insensitive (which it already is).
- **VALIDATE**: `pytest -q tests/test_auth_middleware.py -k secure -v`

### Task 3: Tests for Task 1
- **ACTION**: Add to `tests/test_auth_token.py`, near the existing
  `_warn_if_misconfigured` tests (matches that section's subject).
- **IMPLEMENT**:
  ```python
  def test_resolve_secret_passes_through_configured_value():
      assert shared_auth._resolve_secret("a-real-secret-value") == "a-real-secret-value"


  def test_resolve_secret_generates_random_when_still_default():
      result = shared_auth._resolve_secret("default-dev-secret")
      assert result != "default-dev-secret"
      assert len(result) > 20  # token_urlsafe(32) is well over 20 chars


  def test_resolve_secret_generates_a_different_value_each_call():
      a = shared_auth._resolve_secret("default-dev-secret")
      b = shared_auth._resolve_secret("default-dev-secret")
      assert a != b


  def test_resolve_secret_warning_names_the_risk_but_not_the_value(caplog):
      with caplog.at_level(logging.WARNING):
          result = shared_auth._resolve_secret("default-dev-secret")
      assert "VITALFORGE_SECRET" in caplog.text
      assert result not in caplog.text
  ```
- **MIRROR**: TESTING_IMPORT_TIME_BEHAVIOR and LOGGING_NO_SECRET_LEAKAGE above.
- **IMPORTS**: `logging` and `shared_auth` are already imported at the top of
  `tests/test_auth_token.py` (`from shared import auth as shared_auth`) — reuse.
- **GOTCHA**: Do not monkeypatch `shared_auth._SECRET` or `shared_auth._serializer` in
  these tests — they're testing the pure function, not the module's already-resolved
  import-time state. A different test (Task 4) covers the actual `_SECRET`/`_serializer`
  wiring indirectly via the login/cookie flow.
- **VALIDATE**: `pytest -q tests/test_auth_token.py -v` (full file, confirm nothing else
  broke)

### Task 4: Tests for Task 2
- **ACTION**: Add to `tests/test_auth_middleware.py`, near
  `test_auth_login_with_valid_bearer_redirects_to_root` (same route under test).
- **IMPLEMENT**:
  ```python
  async def test_login_cookie_not_secure_over_plain_request(monkeypatch, matrix_client):
      monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
      resp = await matrix_client.post(
          "/auth/login", json={"username": "admin", "password": "correct-pass"}
      )
      assert resp.status_code == 200
      assert "Secure" not in resp.headers["set-cookie"]


  async def test_login_cookie_secure_when_forwarded_https(monkeypatch, matrix_client):
      monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
      resp = await matrix_client.post(
          "/auth/login",
          json={"username": "admin", "password": "correct-pass"},
          headers={"X-Forwarded-Proto": "https"},
      )
      assert resp.status_code == 200
      assert "Secure" in resp.headers["set-cookie"]
  ```
- **MIRROR**: THROWAWAY_APP_FIXTURE above — reuse `matrix_client` verbatim, it already has
  `add_auth_routes` wired (so `/auth/login` exists) and doesn't need a new fixture.
- **IMPORTS**: Nothing new — `matrix_client`, `shared_auth`, `monkeypatch` all already
  imported/available in this file.
- **GOTCHA**: `matrix_client`'s underlying `AsyncClient` uses `ASGITransport` with
  `base_url="http://test"` — verified empirically (not assumed) that `request.url.scheme`
  is `"http"` in this harness with no forwarded header, and the header is read correctly
  when set (`{'scheme': 'http', 'xfp': 'https'}`). The no-header test is a genuine negative
  case, not vacuously true.
- **VALIDATE**: `pytest -q tests/test_auth_middleware.py -v` (full file)

### Task 5: Docs
- **ACTION**: Update `README.md` and `.env.example`.
- **IMPLEMENT**: In `README.md`'s env var table (~line 95), change the `VITALFORGE_SECRET`
  row's description to note the new behavior, e.g.: *"Secret key for signing session
  cookies. If unset (or left as the placeholder default), a random one is generated per
  process at startup and a warning is logged — sessions won't survive a restart, and if you
  run both services, they'll stop sharing sign-on until this is set explicitly."* In
  `.env.example` (~line 8), extend the comment above `VITALFORGE_SECRET=` similarly.
- **MIRROR**: `test_docs_drift.py`'s existing pattern of pinning specific README substrings
  — after this edit, consider (not required, see NOT Building — no test file changes beyond
  Tasks 3/4 are in scope) whether a future doc-drift guard is warranted; not adding one now
  keeps this plan's diff minimal.
- **IMPORTS**: N/A
- **GOTCHA**: Don't touch the existing `test_readme_documents_both_revocation_procedures`
  drift guard's assumptions (`tests/test_docs_drift.py:31-34`) — it only checks that
  `VITALFORGE_SECRET` and `VITALFORGE_API_TOKEN` both appear as substrings within the
  `### Authentication` ... `## Deployment` slice of the README; the env-var-table edit is
  outside that slice and doesn't affect it, but re-run the drift suite to confirm.
- **VALIDATE**: `pytest -q tests/test_docs_drift.py -v`

---

## Testing Strategy

### Unit Tests
| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `test_resolve_secret_passes_through_configured_value` | `"a-real-secret-value"` | same value returned unchanged | — |
| `test_resolve_secret_generates_random_when_still_default` | `"default-dev-secret"` | a different, long random string | the exact defect being fixed |
| `test_resolve_secret_generates_a_different_value_each_call` | `"default-dev-secret"` (twice) | two different values | confirms randomness, not a second hardcoded fallback |
| `test_resolve_secret_warning_names_the_risk_but_not_the_value` | `"default-dev-secret"` | warning logged, generated value never appears in the log | secret-leakage-via-logs regression |
| `test_login_cookie_not_secure_over_plain_request` | POST `/auth/login`, no forwarded header | `Set-Cookie` has no `Secure` | today's local-dev-safe behavior, unchanged |
| `test_login_cookie_secure_when_forwarded_https` | POST `/auth/login`, `X-Forwarded-Proto: https` | `Set-Cookie` has `Secure` | the exact defect being fixed |

### Edge Cases Checklist
- [x] Empty/unset `VITALFORGE_SECRET` — covered (`_resolve_secret` receives the sentinel
      default via `os.environ.get(..., _DEFAULT_SECRET_SENTINEL)`, same code path as
      explicitly setting it to the literal placeholder string)
- [x] `VITALFORGE_SECRET` explicitly set to a real value — covered (passthrough test)
- [x] `X-Forwarded-Proto` header present with wrong casing (`HTTPS`, `Https`) — covered by
      `.lower()` in the implementation; not separately tested given it's a one-line,
      low-risk normalization identical to a pattern already trusted elsewhere
      (`shared/auth.py`'s existing `scheme.lower() != "bearer"` check) — add a test only if
      review flags this as insufficiently covered
- [x] No forwarded header, direct HTTPS (`request.url.scheme == "https"`) — not separately
      tested; `ASGITransport`'s test harness can't produce a real HTTPS scope, and the
      header-based path already covers the security-relevant case for this deployment. Note
      this as an untested fallback branch in the PR description rather than skip it silently.
- [ ] Concurrent access — N/A, no shared mutable state introduced
- [ ] Permission denied — N/A, no filesystem/permission changes

---

## Validation Commands

### Static Analysis
```bash
source .venv/bin/activate && ruff check .
```
EXPECT: All checks passed

### Unit Tests (affected files)
```bash
source .venv/bin/activate && pytest -q tests/test_auth_token.py tests/test_auth_middleware.py tests/test_docs_drift.py -v
```
EXPECT: All new and existing tests pass, zero failures

### Full Test Suite
```bash
source .venv/bin/activate && pytest -q
```
EXPECT: 255 passed (pre-plan baseline) + 6 new = 261 passed, 3 deselected, no regressions

### Playwright (separate invocation, per CLAUDE.md — never merge with the above)
```bash
source .venv/bin/activate && pytest -q -m playwright
```
EXPECT: 3 passed (unaffected by this change — no template/UI edits)

### Manual Validation
- [ ] `docker compose up --build`, confirm both services still start cleanly with no
      `VITALFORGE_SECRET` set locally (should see the new warning in logs, not a crash)
- [ ] Log in via the browser locally (plain HTTP) — confirm session still works exactly as
      before (no `Secure` flag, since local dev has no forwarded-HTTPS header)
- [ ] If convenient: log in through JD's actual TLS-terminating proxy in a non-prod
      context, inspect the `Set-Cookie` response header in browser devtools, confirm
      `Secure` is present

---

## Acceptance Criteria
- [ ] All 5 tasks completed
- [ ] All validation commands pass
- [ ] 6 new tests written and passing (4 for `_resolve_secret`, 2 for `secure=`)
- [ ] No lint errors
- [ ] Matches UX design (no user-facing behavior change except the two fixed defects)

## Completion Checklist
- [ ] Code follows discovered patterns (STARTUP_WARNING_PATTERN, THROWAWAY_APP_FIXTURE)
- [ ] Error handling matches codebase style (never raise on misconfiguration, warn instead
      — consistent with `_warn_if_misconfigured`)
- [ ] Logging follows codebase conventions (module `logger`, never logs secret values)
- [ ] Tests follow test patterns (direct function calls over module reload, `monkeypatch`
      on module globals, `matrix_client`/`configured_auth` reuse)
- [ ] No hardcoded values beyond the necessary `_DEFAULT_SECRET_SENTINEL` comparison
      constant
- [ ] Documentation updated (README env table, `.env.example`)
- [ ] No unnecessary scope additions (no new env var, no shared-secret-file mechanism, no
      `uvicorn` proxy-header config changes — see NOT Building)
- [ ] Self-contained — no questions needed during implementation

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| When `VITALFORGE_SECRET` is unset, `vitalforge-weight` and `vitalforge-dashboard` each generate their own independent random secret (separate processes, separate imports) — cross-service single sign-on (documented as intentional in `CLAUDE.md`) silently breaks until the operator sets `VITALFORGE_SECRET` explicitly | Certain, whenever the fallback path is taken | Medium — degrades a working feature as a side effect of the security fix, though only in an already-misconfigured state | The warning message (Task 1) explicitly names this consequence, not just "sessions won't survive a restart." README update (Task 5) states it too. Full fix is the one-line `.env` change the warning already directs the operator to make — not attempting the shared-secret-file alternative (see NOT Building) to keep this plan's scope and risk surface small |
| `X-Forwarded-Proto` fallback-to-`request.url.scheme` branch has no automated test (ASGI test transport can't simulate a real HTTPS scope) | Low — the code is a one-line boolean `or`, low complexity | Low — if wrong, worst case is the `Secure` flag missing on a hypothetical direct-HTTPS-no-proxy deployment, i.e. today's status quo, not a regression | Documented as an explicit known gap in Testing Strategy rather than silently skipped; manual validation step included |
| A stale session cookie signed with a *previous* auto-generated secret becomes unverifiable after a restart (by design — that's what invalidation means) | Certain on every restart while `VITALFORGE_SECRET` is unset | Low — user just has to log in again, `validate_session` already returns `None` on `BadSignature` cleanly (existing, tested behavior) | No mitigation needed; this is the intended behavior JD chose over failing startup |

## Notes
- Both fixes are scoped to `shared/auth.py` only — per `CLAUDE.md`'s standing instruction,
  re-check both `vitalforge-weight` and `vitalforge-dashboard` after this merges (their
  `add_auth_routes(app)` call sites are the only place either service touches this module's
  route-level behavior; no service-specific code changes are anticipated, but the full test
  suite run in Validation Commands covers both services' existing auth tests either way).
- This plan intentionally does NOT touch the settings-menu / user-management /
  token-management design discussed earlier in this session — that's a separate,
  larger architectural project, sequenced to start after this plan ships (per the user's
  explicit sequencing choice).
- Source escalation: `docs/prp/04-review-findings.md`, "Escalated to JD" section, Codex
  findings #1 and #3.
