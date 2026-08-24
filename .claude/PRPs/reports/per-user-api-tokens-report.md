# Implementation Report: Per-user API tokens

## Summary

Phase B replaced the runtime shared bearer secret with named, DB-backed credentials owned
by real user accounts. Tokens are generated with 256 bits of entropy, stored only as
SHA-256 hashes, shown once, resolved to the owner's live identity/role, and independently
revocable by the owner or an administrator.

## Delivered

- Added `api_tokens` and durable `auth_migrations` tables plus the ownership index.
- Added account create/list/revoke APIs and UI, administrator list/revoke UI, password
  step-up, transactional cleanup on user deletion, and best-effort last-use telemetry.
- Migrated an existing `VITALFORGE_API_TOKEN` once after first-admin bootstrap.
- Ported the bearer behavior matrix and end-to-end scenarios to DB-backed tokens.
- Updated README, `.env.example`, code maps, and drift guards.

## Deviations and review fixes

- Used `POST /auth/tokens/{id}/revoke` instead of a JSON-bearing DELETE for reliable
  first-party client behavior.
- Returned a full account-bound identity from the token lookup rather than resolving a
  username and querying its role separately, removing a deleted/recreated-name race.
- Replaced the plan's empty-token-table migration guard with a durable marker. Otherwise a
  revoked migrated credential would return after restart while the env var remained set.
- Serialized token issuance against account deletion and bound it to the authenticated
  session version, because this project does not enable SQLite foreign keys.
- Marked the one-time raw-token response `Cache-Control: no-store`.

## Validation

- Local: Ruff, diff hygiene, docs drift tests, and both inline auth-page scripts parsed by
  Node.
- PR #22 CI: full Python 3.12 suite, Playwright smoke suite, and both container builds.
- Adversarial review covered identity/role binding, cross-user authorization, step-up,
  migration races and restart behavior, account deletion, secret exposure, caching, and
  untrusted UI data. Every finding was fixed before merge.
