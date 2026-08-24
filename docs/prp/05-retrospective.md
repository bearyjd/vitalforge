# 05 — Release retrospective

**Status:** repository release gate complete pending the coordinated production-host smoke.

VitalForge's token-auth and body-composition tracks are implemented, reviewed, and covered
by CI. The later account-model work also replaced the original single shared browser login
and bearer secret with DB-backed users and per-user tokens. This document is the final
contract handoff for Bascule and records what its replay milestone may safely assume.

## What shipped

- `POST /api/weight` accepts weight-only or independently optional body-composition fields,
  validates their physical ranges, persists them atomically, deduplicates retries, and
  reports Garmin delivery separately from local persistence.
- Body fat, body water, bone mass, and muscle mass are available through the metrics API.
  Garmin read-back confirmed bone and muscle mass are grams.
- Browser authentication is DB-backed and multi-user. Passwords use salted scrypt hashes;
  roles are read live; password changes invalidate existing sessions.
- Machine authentication uses named, per-user bearer tokens created at `/auth/account`.
  Raw values are shown once, only SHA-256 hashes are stored, and tokens inherit their
  owner's live role. Admins can inspect and revoke all tokens from `/auth/admin/users`.
- The old `VITALFORGE_API_TOKEN` value is an upgrade input only. It is imported once for
  the first admin with a durable marker, so revoking it cannot make it reappear on restart.
- Session cookies never use the public placeholder secret and gain `Secure` whenever the
  client-facing request is HTTPS.

## Bascule contract deltas

This section supersedes the authentication lifecycle text in `00-design.md` §4.1. The
weight payload and response shapes in §4.2–§4.5 remain authoritative, with the additions
below.

1. The wire header is unchanged: `Authorization: Bearer <token>`.
2. The token is no longer a deployment-wide env secret. An operator creates a named token
   for the Bascule-owning account at `/auth/account` and copies the raw value when shown.
3. Tokens do not expire and have no refresh flow. A 401 remains terminal until the operator
   supplies a new token.
4. Revocation is per token and immediate; rotating `VITALFORGE_SECRET` only revokes browser
   cookies. Deleting the owning user revokes that user's sessions and tokens.
5. Authorization is the owning account's current role. A normal `user` token can use the
   health APIs but cannot call administrator routes. Bascule must not depend on admin access.
6. Keep the token in encrypted platform storage and never include it in application logs,
   crash reports, analytics, or screenshots.
7. A deduplicated conflict response can include `conflict: true` and `conflict_fields`.
   Values named there were rejected in favor of the previously stored values even though
   the HTTP status is 200.
8. `synced_to_garmin: false` means the row was stored locally but Garmin delivery is not
   known to have succeeded. No server reconciliation worker exists; do not retry solely
   because that flag is false, since a lost Garmin response can create a false negative.

## Load-bearing review findings

The following objections materially changed the implementation or contract:

- A public fallback signing secret made cookie forgery possible. Missing/placeholder
  secrets now generate a process-local random value with a loud warning.
- TLS termination was external to the compose files, so an unconditional `Secure` cookie
  would break local HTTP. Request-aware TLS detection protects production without changing
  the development path.
- The first dedup design could race across the two services. `BEGIN IMMEDIATE` makes the
  match-and-write sequence atomic against a shared SQLite file.
- Non-finite numbers and JSON booleans exposed Pydantic/JSON edge cases. Both now produce
  terminal validation errors instead of a 500 or silent numeric coercion.
- Source-only enrichment accidentally re-triggered Garmin uploads and could corrupt the
  stored sync flag. Garmin work is now gated only by composition changes.
- A migrated env token keyed only to an empty token table would be resurrected after
  revocation. A durable, atomic migration marker closes that restart path.
- SQLite foreign keys are declared but not enabled. User deletion removes tokens in the
  same transaction, and token issuance serializes against deletion to avoid orphan rows.

## Replay-path dependency — Bascule milestone 7

Milestone 7 **cannot safely blind-flush buffered historical readings yet**. The API assigns
the measurement timestamp at server receipt and accepts no `measured_at` field. Deduplication
therefore compares readings received within 60 seconds and within 50 grams. Two distinct
historical readings delivered in one burst can collapse permanently while returning a
successful `deduplicated: true` response.

Bascule may currently assume:

- retrying one in-flight request after a connection failure or timeout is safe;
- a plain successful response represents a locally stored row;
- `deduplicated: true` means a server-side match, not proof that this specific buffered
  reading was newly stored;
- 400/401/422 are terminal for the current payload or credential; and
- only 500, connection failure, or timeout are retry candidates, with backoff.

Until the server contract gains a client-supplied measurement timestamp and a replay-safe
idempotency key, Bascule must retain a local copy after `deduplicated: true` and space
historical submissions more than 60 seconds apart. Adding `measured_at` is a coordinated
server/client contract change, not an Android-only milestone.

## Release evidence and remaining live checkpoint

- All implementation PRs were squash-merged only after green lint, Python 3.12 tests,
  Playwright smoke tests, and container builds.
- Phase 4's adversarial findings and their dispositions are recorded in
  `04-review-findings.md`; the two escalated auth findings shipped in PR #21.
- Per-user token Phase B shipped in PR #22 after a second adversarial review; all findings
  were fixed before merge.
- Release tagging and registry publication are performed after this retrospective merges.
- The final production-host checkpoint remains an operator-coordinated action: pull the
  tagged images, check both `/health` endpoints, submit one token-authenticated weigh-in,
  and run/verify dashboard sync. No agent should touch the live host or its secrets without
  that explicit coordination.
