# PR: Bearer token auth for machine clients

**Repo:** bearyjd/vitalforge (MIT)
**Branch:** `feat/api-token-auth`
**Motivation:** Unattended clients (Bascule Android bridge, `ble-scale-sync` on
Atlas, Tasker) currently have to impersonate a browser session by pasting a
`vf_session` cookie value, which expires after 30 days with no re-auth path.
A background BLE service cannot prompt for login, so it silently stops working.

This adds a long-lived, revocable static token as an **alternative** credential.
Cookie session auth is unchanged and remains the path for human/browser use.

---

## Design

- New env var `VITALFORGE_API_TOKEN`. If unset/empty, bearer auth is disabled
  entirely and behaviour is identical to today.
- Requests may authenticate with **either**:
  - existing signed `vf_session` cookie (browser), or
  - `Authorization: Bearer <token>` header (machine clients)
- Token is compared with `secrets.compare_digest` (constant time).
- No expiry. Revocation = rotate the env var and restart the container.
- Applies to both services (`vitalforge-weight`, `vitalforge-dashboard`) since
  they share `shared/auth.py`.

### Why a static token rather than JWT/OAuth
Single-operator, self-hosted, LAN-or-Tailscale deployment. A rotating-credential
scheme adds a refresh flow the Android service would have to implement and fail
at, to defend against a threat model (token exfiltration from a device you
control, on a network you control) that a rotation window barely mitigates.
Static token + rotation-on-suspicion is the proportionate choice here.

---

## Implementation

### 1. `shared/auth.py`

Add alongside the existing session logic. Exact integration depends on the
current shape of the file — this assumes a FastAPI dependency named something
like `require_auth`. Adapt names to match.

```python
import os
import secrets

API_TOKEN = os.getenv("VITALFORGE_API_TOKEN", "").strip()


def _bearer_token_valid(request) -> bool:
    """Constant-time check of the Authorization: Bearer header."""
    if not API_TOKEN:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return secrets.compare_digest(value, API_TOKEN)
```

Then, in the existing auth dependency, short-circuit **before** the session
cookie check:

```python
async def require_auth(request: Request):
    # Auth disabled entirely (no VITALFORGE_PASS) — unchanged behaviour
    if not AUTH_ENABLED:
        return

    # Machine clients: bearer token
    if _bearer_token_valid(request):
        return

    # Humans: existing signed session cookie path (unchanged)
    ...existing cookie validation...
```

Ordering matters: bearer check first means a machine client never pays the cost
of cookie parsing, and a malformed/absent cookie can't shadow a valid token.

### 2. `.env.example`

```diff
 VITALFORGE_USER=admin
 VITALFORGE_PASS=your-password-here
 VITALFORGE_SECRET=your-random-secret-here
+# Optional: long-lived token for unattended API clients (scale bridges, Tasker).
+# Leave empty to disable bearer auth. Generate with:
+#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
+VITALFORGE_API_TOKEN=
```

### 3. `README.md`

Add to the environment variable table:

| `VITALFORGE_API_TOKEN` | No | Long-lived bearer token for unattended API clients. Empty disables bearer auth. |

Update the **Authentication** section to note both credential types, and replace
the Tasker "copy your session cookie, re-login every 30 days" instructions with:

```
Headers: Authorization: Bearer YOUR_API_TOKEN
```

That subsection currently documents a workaround that this PR obsoletes — worth
rewriting rather than appending to, so nobody follows the cookie path by default.

### 4. `tests/`

- bearer token accepted when `VITALFORGE_API_TOKEN` set and header matches
- bearer rejected on mismatch (and on `Bearer` with empty value)
- bearer rejected when `VITALFORGE_API_TOKEN` unset, even if header present
- session cookie path still works with token auth enabled (no regression)
- both paths bypassed when `VITALFORGE_PASS` empty (auth fully disabled)
- `compare_digest` used — assert no early-exit on prefix match if feasible

---

## Security notes

- **Token in transit.** Bearer over plain HTTP on the LAN is the same exposure
  the cookie already had. If the weight service is reachable outside the LAN
  (via the nginx subdomain), TLS is doing the work — confirm certbot is covering
  `weight.*` before enabling a token that never expires.
- **Token at rest on Android.** Bascule must store this in
  `EncryptedSharedPreferences`, never plain `SharedPreferences` or source.
- **Logging.** Confirm the FastAPI/uvicorn access log config does not log request
  headers. Default uvicorn access logs do not, but any custom middleware might.
- **Blast radius.** This token grants full API access to both services, including
  `DELETE /api/weight/{id}`. Acceptable for a single-operator deployment; if
  scope ever matters, the natural next step is a read/write distinction rather
  than per-endpoint tokens.

---

## Follow-on (separate PR, not this one)

`POST /api/weight` accepts only `{"weight", "unit"}`, but the dashboard already
tracks `body_fat` as a synced metric, and the BF720 measures body fat, body
water, muscle percentage, and bone mass via BIA. Extending the weight endpoint
to accept optional body-composition fields would let the scale bridges deliver
data the dashboard can already display, instead of decoding and discarding it.

Deliberately out of scope here — this PR should stay a pure auth change so it
can be reviewed and merged fast, since Bascule's network layer is blocked on it.

---

## Checklist

- [ ] `shared/auth.py` — `_bearer_token_valid` + dependency short-circuit
- [ ] `.env.example` — documented, empty by default
- [ ] `README.md` — env table, Authentication section, Tasker section rewrite
- [ ] `tests/` — six cases above
- [ ] Verify no header logging in access log config
- [ ] Confirm behaviour unchanged when `VITALFORGE_API_TOKEN` unset (default)
