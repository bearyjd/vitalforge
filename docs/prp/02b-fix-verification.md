# 02b — Fix verification: did the Phase 2 DA findings actually land?

**Phase:** 2, follow-up. **Scope:** a targeted correctness check of the revisions
to `00-design.md` (1749 lines) and `01-plan.md` (927 lines) against the 13
findings in `02-validation.md`. **Not** a fresh adversarial pass — anything new
below was noticed while checking a specific fix, and is flagged as such.
Per the protocol, this pass still writes no code.

**Verdict: 10 clean RESOLVED, 3 PARTIALLY-RESOLVED, 1 NEW-ISSUE-INTRODUCED.**
**One blocking item remains before Phase 2 item 2 can start on B4.**

The revisions are, on the whole, unusually good: the authors corrected claims
rather than defending them, escalated the two blocking findings to JD as D4/D5,
added 12 named tests each traceable to a finding, and in several places stated
the residual risk *more* harshly than the review did. F2's contract rewrite and
F4's checkpoint gate are better than what the review asked for.

The problem is concentrated in one place. **F1's fix is correct for the path it
covers and silent about the path it doesn't**, and the mitigation it depends on
has no API to hang it on.

---

## Summary table

| # | Finding | Verdict |
|---|---|---|
| F1 | Dedup TOCTOU race | **PARTIALLY-RESOLVED** — insert path fixed; enrichment path uncovered |
| F2 | Receipt-time dedup data loss | **RESOLVED** (one minor defect in new client guidance) |
| F3 | §5.3 "structurally impossible" | **RESOLVED** |
| F4 | Garmin composition units unverified | **RESOLVED** |
| F5 | `**kwargs` fake + unpinned dep | **RESOLVED** |
| F6 | A3 silently-open server | **RESOLVED** |
| F7 | Bearer weakens revocation | **RESOLVED** |
| F8 | 400/422 + PWA `[object Object]` | **RESOLVED** |
| F9 | Enrichment re-push cost | **PARTIALLY-RESOLVED** — cost corrected; F1 interaction half-analysed |
| F10 | FIT truncation | **RESOLVED** |
| F11 | Column naming | **RESOLVED** — verified consistent everywhere |
| F12 | ±50 g boundary | **RESOLVED** |
| F13 | Artifacts uncommitted | **PARTIALLY-RESOLVED** — owner assigned, not yet done |
| N1 | *(new)* F1's fix mandates a Garmin timeout it names no mechanism for | **NEW-ISSUE-INTRODUCED** by F1's fix (high) |
| N2 | *(new)* F1 fix cites evidence that understates its own cost 5× | **NEW-ISSUE**, low |

---

## 1. The one blocked package: B4

### F1 — PARTIALLY-RESOLVED

**What landed, and it is right.** §3.7 gained a dedicated "Atomicity" block, §6
gained D5 (JD chose Option A), §5.5 was rewritten, and B4 was re-scoped from 4h
to 5h with a new `tests/test_dedup_concurrency.py` carrying four tests —
including `test_two_concurrent_identical_posts_store_one_row`, which is
precisely the test whose absence let F1 through. Plan §5 gained a standing rule
("Concurrency must be tested, not reasoned about"). The DA's repro output is
quoted in §3.7. This is a genuine fix for the insert path.

**What is missing: the transaction scope stops short of the enrichment path.**
Every one of the four places the transaction is described bounds it the same way:

```
$ grep -n "BEGIN IMMEDIATE" docs/prp/00-design.md docs/prp/01-plan.md
01-plan.md:465:  ...spanning the duplicate `SELECT` and the row `INSERT`...
00-design.md:913: ...spanning the duplicate `SELECT` and the row `INSERT`...
00-design.md:1540:...spanning the check and the insert...
00-design.md:1607:...spanning the duplicate check and the row insert...
```

But §3.7 defines **three** sub-cases, and only one of them inserts:

- **Sub-case 1** (collapse) — no write at all.
- **Sub-case 2** (enrichment) — `UPDATE` the NULL columns **+ re-push to Garmin**.
- **Sub-case 3** (conflict) — no write, logs a WARNING.

Sub-case 2 is a read-modify-write, which is the same check-then-act shape F1 was
about. Two concurrent composition-bearing POSTs matching the same stored row both
`SELECT` it, both see NULL composition columns, both `UPDATE`, and **both
re-push** — a lost-update race plus the double Garmin upload §3.7 spends four
paragraphs worrying about, arrived at through the path the fix did not cover.
Under a correct transaction the second POST would serialise, observe the
now-non-NULL columns, and fall through to sub-case 1 or 3.

**No test covers this.** B4's four concurrency tests are: two identical POSTs →
one row; two identical POSTs → one Garmin push; two *distinct* POSTs → both
stored; and a concurrent-writer starvation check. **None issues two concurrent
*enrichment* POSTs.** The gap that let F1 through — "every one of this package's
original eleven tests issued one POST at a time" — is reproduced exactly, one
sub-case over.

**Disposition: must-fix-before-Phase-2-item-2.** This is cheap and purely
specification-level: state that the transaction spans the duplicate `SELECT` and
**whichever write follows it — `INSERT` or `UPDATE`** — and add
`test_two_concurrent_enrichment_posts_update_once_and_push_once` to B4's
concurrency file. The red contract tests for B4 should not be written against the
current wording, because it would bake the gap into the tests.

---

### N1 — NEW-ISSUE-INTRODUCED: F1's fix mandates a timeout it names no mechanism for

**Severity: high** (must-fix-before-implementing-B4, not a reopen of D5).

Option A puts the Garmin HTTP upload **inside** the write transaction. Both
documents recognise the cost and both mandate the same mitigation. §3.7:

> the Garmin call inside the transaction must carry an explicit timeout shorter
> than the SQLite busy timeout, so the stall can never outlast the lock.

Plan B4 repeats it as a hard implementation constraint. **There is no such
timeout to set.** Verified against the pinned version (`garminconnect==0.3.11`,
the version B3 now pins):

```
$ python3 -c "import inspect, garminconnect; ..."
add_body_composition params: ['self', 'timestamp', 'weight', 'percent_fat',
  'percent_hydration', 'visceral_fat_mass', 'bone_mass', 'muscle_mass',
  'basal_met', 'active_met', 'physique_rating', 'metabolic_age',
  'visceral_fat_rating', 'bmi']
any timeout param? False
Garmin.__init__ any timeout param? False
```

`add_body_composition` ends in `self.client.post("connectapi", url, files=files,
api=True)` — `self.client` is garth's client, constructed inside
`shared/garmin_client.py:authenticate()`. Enforcing a timeout means reaching into
that session object, which is adjacent to the one path the ground rules protect:
*"Do not modify the Garmin client's auth flow. It is fragile, reverse-engineered,
and working."*

**A second, independent obstacle: the call is synchronous and blocks the event
loop.** Verified:

```
$ python3 -c "...; print('push_weight is coroutine fn?', inspect.iscoroutinefunction(g.push_weight))"
push_weight is coroutine fn? False
```

`vitalforge-weight/app.py:87` calls `push_weight(weight_grams, now)` with no
`await` inside an `async def` route. That is pre-existing, but it means any
timeout must be enforced synchronously (a thread wrapper, a signal, or a session
setting) — `asyncio.wait_for` will not work — and that for the duration of the
Garmin call the weight service's entire event loop is blocked, not merely its
write lock.

**One caveat on my own analysis, stated because I initially got it wrong.**
`garminconnect` does carry an automatic retry loop (`retry_attempts: int = 3`,
exponential backoff `retry_min_wait=1.0` … `retry_max_wait=10.0`), which would
have made the worst-case in-transaction hold ~30 s. It does **not** apply here:
`@_handle_api_errors` decorates only `connectapi`, the web-proxy call, and
`download` (`__init__.py:647`, `:652`, `:657`), and `add_body_composition`
bypasses all three by calling `self.client.post` directly. So the upload is one
attempt at our layer. Whether *garth* retries beneath it is unverifiable here —
garth is not installed in this environment — and that is the same blind spot F5
is about.

**To be clear about what this is and is not.** I established that no `timeout`
parameter exists on the documented API and that the call is synchronous. I did
**not** establish that a timeout is unachievable — `self.client` is a garth client
object, and setting a timeout on its underlying session is plausibly a one-line
change at the `push_weight` call site that touches no login or token code. Calling
that "adjacent to the protected auth flow" is a judgment about proximity, not a
finding that the ground rule forbids it.

So the accurate statement is narrower than "the mitigation is impossible": **the
mitigation as written names no mechanism, and every candidate mechanism is
currently unverified.** B4 cannot treat "carry an explicit timeout" as a settled
constraint when nobody has confirmed how to satisfy it.

**Disposition: must-fix-before-implementing-B4.** Identify the mechanism and
verify it against the pinned `garminconnect==0.3.11` *before* writing the
transaction, since the lock bound is the whole justification for Option A. If no
mechanism survives contact, *then* D5 reopens — but that escalation should follow
evidence, not the absence of a parameter. Three candidates for the implementer:

1. **Set a timeout on garth's session** at the `push_weight` call site. Cheapest
   if it works; verify it actually bounds the upload rather than only the connect
   phase, and that it survives a token refresh.
2. **`BEGIN IMMEDIATE` → `SELECT` → write → `COMMIT` → push → `UPDATE synced`.**
   The lock covers only the DB work, so no timeout is needed at all. My original
   F1 note warned against moving the push after the commit, and I should be
   explicit that **that warning was too strong**: it holds only if nothing comes
   back to set the flag. A same-request `UPDATE` does come back, leaving rows stuck
   at 0 only if the process dies in a sub-second window — far narrower than holding
   a write lock across an unbounded network call. If my warning is what steered
   this to Option A, it steered it wrong, and this note is the correction.
3. **Keep Option A and raise `busy_timeout` deliberately** on `get_db()`, accepting
   a longer cross-service stall as an explicit chosen number rather than an
   inherited default.

---

### N2 — NEW-ISSUE (low): the F1 fix quotes evidence that understates its own cost 5×

Both documents state the mechanism correctly — *"`get_db()` sets no
`busy_timeout`, so sqlite3's 5 s default applies"* — and then paste a repro that
contradicts it:

> ```
> second writer FAILED after 1.00s -> OperationalError: database is locked
> ```

Reproduced against `get_db()` exactly as written (`shared/database.py:9-15`,
WAL, no `busy_timeout` override):

```
default busy_timeout (ms): 5000
concurrent READ ok -> 0
concurrent writer: FAILED after 5.00s -> OperationalError: database is locked
```

The reasoning is right and the reader-unaffected claim is right; the pasted
number is from a run with a non-default timeout. It makes the stall look like one
second when it is five, in the one place a reader is deciding whether the cost is
acceptable. **Disposition: correct the quoted figure in §3.7 and plan B4.**

---

## 2. Partially resolved

### F9 — PARTIALLY-RESOLVED

**The cost correction landed well.** Plan B4 now says the fallback "is not cheap",
walks through the permanently-`synced_to_garmin = 0` consequence, ties it to
§5.3 Path B's reconciliation vacuum and §3.5's dashboard routing, and revives the
rejected store-a-second-row option as worth reconsidering. §4.4's
"reconciliation signal" language is fixed. B3's checkpoint brief now records
whether Garmin collapses same-timestamp uploads (item 3). That is exactly the
disposition.

**The F1 interaction is analysed in one direction only.** B4 says:

> **Note the F1 interaction:** until the atomicity fix lands, the enrichment path
> is not reliably reachable in the scenario it targets.

True, and a good catch. But it treats the atomicity fix as something that *makes*
enrichment reachable, without checking whether the fix as scoped actually covers
enrichment. It does not — see F1 above. The note should read the other way round:
the enrichment path needs the transaction too, and currently is not specified to
get it.

**Disposition: folds into F1's fix.** No separate action.

### F13 — PARTIALLY-RESOLVED

Acknowledged and assigned rather than done. Plan's revision note: *"Two findings
are deliberately not applied to `main`: F5's dependency pin lands on B3's branch,
and F13's commit hygiene is the team lead's to action."* Deferring the pin to B3's
branch is the right call and is a direct improvement on my disposition. But the
working tree is unchanged:

```
$ git status --short
 M tests/fixtures/garmin/weigh_ins.json
?? docs/prp/
?? tests/fixtures/production_schema.sql
```

The modified fixture still sits uncommitted on `main`, and CI still has never run
against it. There are now five artifacts, not four (this file). Suite and lint
re-confirmed locally: `54 passed, 3 deselected`, `ruff check .` clean.

**Disposition: unchanged — commit on a branch before Track A branches are cut.**

---

## 3. Clean resolutions

### F2 — RESOLVED

Better than asked for. JD chose Option 2 (document rather than add `measured_at`),
and the documentation is genuinely unsoftened. §3.7 gained a residual-risk block
labelling the loss **silent / permanent / rare-but-non-zero / client-visible only
as success**; §4.4's duplicate-response section carries a callout; §4.5 rule 4 is
rewritten to distinguish "retry one in-flight reading" (safe) from "blind-flush a
buffer" (not), with three concrete client instructions; §6 D4 records the
decision as *"a real, silent, permanent data loss risk"*. Nothing hedges.

The one thing I would flag, noticed in passing: rule 4's suggested reconciliation
— *"reconcile afterwards via `GET /api/weight/recent`"* — points at an endpoint
hardcoded to `LIMIT 10` (`vitalforge-weight/app.py:122`). A client reconciling a
backlog larger than ten readings cannot see the rest. Low severity, but it is new
guidance in a pinned contract; either raise the limit, add a `?limit=`, or drop
that half-sentence.

### F3 — RESOLVED

§5.3 rewritten to the precise claim — *"cannot fail independently **within a
single upload**"* — with Paths A/B/C each named and dispositioned, and Path B
given an explicit "no change, deliberately so" behavior statement. §4.4's
`synced_to_garmin: false` documentation now says plainly that no reconciliation
process exists and that the flag can be a false negative. The conclusion I did
not ask them to change (no weight-only retry fallback) is preserved and now
correctly labelled as surviving the correction.

### F4 — RESOLVED, and stronger than the disposition

§3.5 separates names (confirmed) from units (unverified, and unverifiable until
the first push), states the grams hypothesis with its evidence
(`sync.py:221` → `weight_history.weight_grams`), and explains why the fixture
makes B5's tests pass under any unit. B3's checkpoint brief gained a numbered
item 2 requiring the raw read-back and the observed units to be *recorded*. Most
importantly, plan §1 and §4.3 make **B5 a hard dependent of the B3 live
checkpoint**, not merely of B3 merging — which is a real schedule change, not a
note. That exceeds what I asked for.

### F5 — RESOLVED

B3 pins `garminconnect==0.3.11` in both requirements files with the reasoning
recorded, and adds `test_real_client_signature_accepts_our_kwargs` — the exact
`inspect.signature` assertion — flagged in the plan as *"the highest-value test in
this package… the only test here that exercises something we do not control."*
Landing the pin on B3's branch rather than loose on `main` is a correct
refinement of my disposition.

### F6 — RESOLVED

§2.4 narrowed in writing to *"no logging **on the per-request auth path**"*, with
the startup warning quoted verbatim and its rationale (silently-open server,
undetectable by any client) recorded. A1 owns it, with three named tests
including `test_startup_warning_contains_no_token_value`. §2.5's A3 block records
both the upheld reasoning and the process point about resolving an ambiguous cell
without asking JD.

### F7 — RESOLVED

Three places, all correct. §2.1 records that the ordering was attacked and upheld,
including *why* the raise-inside-`_bearer_token_valid` attack fails. §2.5's
exemption sentence is rewritten to "exempt from **middleware enforcement**" with
the `/auth/login` behavior spelled out. §4.1 documents the revocation asymmetry,
that responding to a compromise means rotating **both**, and adds the
`DELETE`-scope follow-up as explicitly non-blocking. A3 carries
`test_readme_documents_both_revocation_procedures`; A2 carries
`test_auth_login_with_valid_bearer_redirects_to_root`, which pins the corrected
claim rather than just asserting it in prose.

### F8 — RESOLVED, all three parts

(a) The new weight bound is a `model_validator(mode="after")` → 422, in §3.1,
§4.4, and B2. (b) §3.1 states the falsified premise explicitly. (c) B2 fixes the
PWA toast at `index.html:370` and adds `test_pwa_toast_flattens_array_detail`;
§4.4 warns Bascule that array entries are objects. The legacy `unit` 400 is kept
and correctly relabelled — §4.4 now says *"a legacy quirk retained because
changing it has no benefit — not a backward-compatibility guarantee"* — and
`test_invalid_unit_still_returns_400_not_422` is retained with its purpose
re-described as pinning the one legacy 400 against the 422 migration.

### F10 — RESOLVED

§3.4's mapping table marks the three pass-through rows with an asterisk and
explains that "pass-through" means no unit conversion, not no loss, with the
`int(18.4*100) == 1839` evidence inline. §3.5's "round-trip verification" claim is
retracted in favour of *"what the round trip actually verifies is presence… not
fidelity"*, and the "any future assertion comparing a pushed value to a read-back
value must use a tolerance" instruction is recorded. Accepted-not-fixed, as
dispositioned.

### F11 — RESOLVED, and I verified the completeness the brief asked about

The fix is applied in every location, not one. §3.5 item 1 says "unit-suffixed
mass columns"; item 3 separates the `METRIC_TABLES` **key** (unsuffixed, so the
URL surface is unchanged) from the **column** (suffixed) — a cleaner resolution
than I proposed. B5's files-touched list and its "Column naming (F11)" block both
carry it, and the choice is correctly deferred to the F4 checkpoint with both
variants named so the implementer picks rather than invents. Grepped for
survivors of the old bare names:

```
$ grep -n 'muscle_mass REAL\|bone_mass REAL\|("weight_history", "bone_mass")\|("weight_history", "muscle_mass")' docs/prp/00-design.md docs/prp/01-plan.md
  none found
```

### F12 — RESOLVED

§3.7 specifies `abs(weight_grams − incoming) <= 50`, "inclusive — exactly 50 g
collapses", with the reason the boundary cannot be left to the implementer (it is
a contract guarantee via §4.5). B4 replaces the bracketing pair with
`test_dedup_tolerance_exactly_50g_collapses`. The "Why 60 seconds" paragraph is
corrected to admit the collapsed records are *not* byte-identical and that the
argument rests on "the difference is negligible" — and §3.7 adds that this
reasoning does not extend to burst delivery, correctly linking it to F2.

---

## 4. Minor staleness noticed in passing

Not findings; listed so they can be swept in the next edit.

- **§3.5 lines 833–852** retain the superseded *"Key names — captured
  2026-08-22 … The design above therefore stands as written"* block, immediately
  after the new units-unverified text that supersedes it. Not contradictory (it is
  about names) but redundant and confusing on first read.
- **§1.7 line 258** still says `weigh_ins.json` *"contains only `{weight, bmi,
  bodyFat}`"*, which §3.5 and the working tree both contradict. Pre-existing.
- **Plan §6** still describes B2's change as *"absurd weight: 200 → 400"*; F8
  made it 422. One word.

---

## 5. Bottom line

**Not clean to proceed to Phase 2 item 2 on B4.** One blocking item: F1's fix
must be re-scoped to cover the enrichment write, and B4 needs a concurrent-
enrichment test. Writing B4's red contract tests against the current wording
would bake the enrichment gap into the test suite, which is the specific way F1
got through the first time.

N1 rides along with it — B4 should confirm how it will bound the lock before
implementing the transaction, since that bound is Option A's entire
justification. It does not by itself need JD.

**Clean to proceed everywhere else.** Track A (A1–A3) is fully resolved across
F6, F7, and F8 and is not touched by any open item; B1, B2, B3, B5, and B6 are
clean. Since the ground rules merge Track A completely before Track B begins,
**the open items do not block Track A, the critical path Bascule is waiting on.**
