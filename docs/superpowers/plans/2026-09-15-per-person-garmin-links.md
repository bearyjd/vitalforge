# Per-person Garmin links — Phase 3

**Status:** approved and implemented

## Goal

Replace deployment-wide Garmin credentials with privacy-safe, per-person links.

## Approach

- Store only link metadata, generations, bounded error codes, and rate-limit state in SQLite; keep tokens in private versioned filesystem stores.
- Use one registry boundary for every Garmin login, read, write, upload, and reconciliation operation. It provides a durable global call budget, person lock, generation recheck, and bounded errors.
- Add cookie-session, `manage`-authorized link lifecycle routes with current-password step-up; never expose credentials, account email, token paths, or provider error text.
- Migrate dashboard sync and weight/activity calls to the registry. Unlinked people receive an explicit local-only/link-required result.
- Bind a verified legacy flat store once to the then-primary person; archive/unlink makes the state non-reusable.

## Acceptance criteria

- Different people cannot use each other's Garmin client or token store.
- Relink, unlink, archival, rate limiting, session races, invalid credentials, and concurrent startup are covered without live Garmin access.
- Existing behavior remains covered by Ruff and the non-browser pytest suite.

## Out of scope

- Phase 4 multi-person scheduling/fairness.
- Phase 5 household comparison UI.
