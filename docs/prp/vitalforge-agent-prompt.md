# AGENT PROMPT: VitalForge — token auth + body-comp intake, full lifecycle

You are the lead engineering agent for two sequential changes to
**bearyjd/vitalforge** (Python/FastAPI, MIT, two services sharing
`shared/auth.py` and a SQLite volume):

- **Track A — bearer token auth** for unattended clients. Spec:
  `docs/prp/vitalforge-token-auth-pr.md`. Ships first; the Bascule Android
  effort (parallel, separate repo) is blocked on it.
- **Track B — body-composition intake**: extend `POST /api/weight` to accept
  optional BIA fields (body fat %, body water %, muscle %, bone mass), store
  them, and push them to Garmin alongside weight.

Work phase by phase; do not start a phase until the prior gate passes. This is
a **live system with real data** (JD's weigh-in history and Garmin credentials)
— migration safety and backward compatibility are hard requirements, not
niceties. Existing clients (PWA, Tasker cookie flow, dashboard sync) must be
unaffected at every merge point.

Where this prompt and the spec docs conflict, the specs' *requirements* win;
this prompt's *process* wins.

---

## Model dispatch (orchestrator: read first)

The orchestrating agent (Sonnet) sequences phases, checks exit gates, and
dispatches work. It does **not** absorb design or review work into its own
context. Hard rules:

| Work | Executor | Mechanism |
|---|---|---|
| Phase 0–1 (design, planning docs) | Opus | `claude -p --model opus`, or code-plan combo |
| Phase 2 & 4 devil's advocate | Opus, **fresh session** — never the same context that produced the design | separate `claude -p --model opus` invocation |
| Phase 3 implementation packages | Sonnet | orchestrator directly, or `claude -p` subprocess per package |
| Phase 4 adversarial review | Codex | `codex -q --model gpt-5.1-codex` |
| Commit messages, PR descriptions, changelog | cheap-think | OmniRoute API downshift |

The devil's advocate fresh-session rule is load-bearing: a DA sharing context
with the designer defends the design instead of attacking it. Dispatch it cold
with only the committed artifacts as input.

Extra weight on Opus for this repo's Phase 0: this is a live system with real
weigh-in history and Garmin credentials. The behavior matrix and migration
design are where a shallow pass causes real data damage — do not let these
drift to the orchestrator "to save a dispatch."

This project runs in its own session/workdir, parallel to the Bascule effort.
The only cross-project output is the Track A contract doc, delivered as a
file at merge time. Do not share context between the two projects.

---

## Ground rules (all phases)

- Branch-per-feature, PRs into `main`, squash-merge on green CI only.
- Track A merges completely before Track B implementation begins. The moment
  Track A is on `main`, notify the Bascule effort with the final contract
  (header format, error responses) — it is pinned from then on.
- Every behavior change lands with tests in the same PR. The existing test
  suite must never go red at a merge point.
- Never log credentials, tokens, or Authorization headers. Audit any
  middleware/logging you touch for this.
- SQLite migrations must be additive (new nullable columns), idempotent,
  and run automatically on container start against a copy-tested fixture of
  the current schema. No destructive migration under any circumstances.
- Do not modify the Garmin client's auth flow. It is fragile,
  reverse-engineered, and working. Extend the weigh-in push payload only.

## Phase 0 — Design

Produce `docs/prp/00-design.md` covering both tracks:

1. **Read the actual code first.** The specs were written against the README;
   `shared/auth.py`'s real structure, the weight route's dependency wiring,
   and the current DB schema are ground truth. Document the as-is state before
   designing the to-be.
2. Track A design: where the bearer check sits relative to session validation,
   constant-time comparison, behavior matrix for all combinations of
   (`VITALFORGE_PASS` set/unset × `VITALFORGE_API_TOKEN` set/unset × credential
   presented). All 8+ cells specified.
3. Track B design: request DTO with optional fields and validation ranges
   (reject physically impossible values — specify them); schema migration;
   Garmin push payload mapping (confirm exactly which fields `garminconnect`'s
   add_body_composition accepts and their units); dashboard exposure via the
   existing `/api/metrics/{name}` pattern; `source` column ('pwa' / 'bascule' /
   'bridge' / 'tasker', nullable for historical rows) for provenance.
4. Contract document for Bascule: exact request/response JSON for both the
   weight-only and full-payload forms, all error shapes (401, 422, 500),
   and the rule that unknown fields are rejected (not ignored) so client
   drift is caught loudly.
5. Failure review: token set but empty string; header injection attempts;
   Garmin push succeeds for weight but the composition call fails (define
   the partial-success behavior and response); migration interrupted mid-run;
   two bridges POSTing the same weigh-in seconds apart (dedup policy —
   specify window and fields).

**Exit gate:** behavior matrix complete; migration plan tested mentally against
"container killed during first boot after upgrade"; Bascule contract doc ready
to hand over.

## Phase 1 — Planning

Produce `docs/prp/01-plan.md`:

1. Ordered work packages, each ≤ half a day, each with files touched and named
   tests. Track A: (auth helper + tests) → (dependency wiring + behavior
   matrix tests) → (env/README/Tasker-section rewrite). Track B: (migration +
   fixture tests) → (DTO + validation tests) → (Garmin payload mapping +
   fake-client tests) → (dedup) → (metrics exposure) → (docs).
2. Test strategy: FastAPI TestClient for route behavior; a fake Garmin client
   injected at the seam (never call real Garmin in CI); migration tests run
   against a fixture DB file matching the current production schema with
   representative rows.
3. Rollback plan per package: since migrations are additive, rollback =
   deploy previous image; verify each package leaves the DB readable by the
   previous version.

**Exit gate:** every package has named tests; migration fixture exists and
matches production schema (ask JD for a schema dump if the repo doesn't
contain one — do not guess).

## Phase 2 — Validation of the plan

1. **Devil's advocate pass** (protocol below) on the design: minimum five
   objections. Mandatory targets to attack: the auth ordering (can the bearer
   path ever weaken the cookie path?), the partial-Garmin-success behavior,
   and the dedup window (what does it do to two *real* weigh-ins ten minutes
   apart?).
2. Write contract/behavior-matrix tests first, red, against unimplemented
   behavior.
3. CI pinned: pytest, ruff, mypy (or the repo's existing linters — extend,
   don't replace), running on PRs.

**Exit gate:** red contract tests committed; CI green otherwise; DA findings
dispositioned in writing.

## Phase 3 — Implementation (incremental)

Per work package: branch → implement to green (package tests + full suite) →
PR with design-doc traceability → squash-merge on green. Track A fully merges,
contract is handed to Bascule, then Track B begins.

Live-system checkpoints (coordinate with JD — his deployment):
- After Track A merge: JD deploys, generates a token, verifies PWA login still
  works AND a curl with the bearer header logs a weight. Recorded in
  `docs/prp/03-live-validation.md`.
- After Track B migration package: JD deploys, confirms existing history
  intact, dashboard renders, sync runs clean.
- After Garmin mapping package: one real weigh-in with composition data
  verified visible in Garmin Connect. This is the only test that can't be
  faked — schedule it explicitly.

**Exit gate:** all packages merged; all three live checkpoints recorded with
results.

## Phase 4 — Holistic review

1. **End-to-end scenario tests**: token client full flow; cookie client
   regression flow; mixed clients interleaved; full-payload POST → stored →
   metrics endpoint serves it → fake-Garmin received correct mapping;
   duplicate POST within dedup window collapsed; out-of-range body fat
   rejected with 422.
2. **Devil's advocate gate** on the merged whole.
3. **Adversarial review by a second model** (Codex CLI or equivalent — per
   house convention, a different model than the implementer). Brief: auth
   bypass attempts (header casing, whitespace, unicode in token, method
   confusion), SQLite concurrent-write behavior between the two services,
   injection via the new DTO fields, and anything that lets a request skip
   `compare_digest`. Triage every finding: fix or written won't-fix. No
   silent dismissals.
4. Docs pass: README env table, Authentication section rewritten (both
   credential types), Tasker section rewritten to bearer (delete the
   cookie-copying instructions), API reference updated with the extended
   schema.

**Exit gate:** scenario tests green in CI; both review tracks dispositioned in
`docs/prp/04-review-findings.md`; docs merged.

## Phase 5 — Release gate

1. CI fully green on `main`.
2. Tag a release; CI publishes images to Docker Hub + GHCR (existing
   workflow); versioned tags confirmed present.
3. JD pulls on the production host; post-deploy smoke: `/health` both
   services, one token-auth weigh-in, dashboard sync.
4. `docs/prp/05-retrospective.md`: contract deltas Bascule needs to know
   about, DA objections that proved load-bearing, and the state of the
   replay-path dependency (what Bascule's milestone 7 can now assume).

---

## Devil's advocate protocol (phases 2 and 4)

Persona whose job is to kill the design. Objections must be specific and
falsifiable; minimum five per gate; at least one against something you're
confident about; "looks fine" is a failed review. Record attack, severity,
evidence, disposition. Findings go to the implementer persona; the DA writes
no code.

## Escalation to JD (stop and ask)

- Anything touching `shared/garmin_client.py` auth flow
- Any migration that is not purely additive
- Discovery that `garminconnect` cannot push a composition field the design
  assumed (changes the Bascule contract)
- Any auth behavior matrix cell where the safe answer is ambiguous
- Before both live-system deployment checkpoints
