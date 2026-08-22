# 02 — Validation: devil's advocate pass on the Phase 0 design and Phase 1 plan

**Phase:** 2, item 1 (devil's advocate) only. **Status:** complete.
**Persona:** adversarial reviewer, dispatched cold — no shared context with the
authors of `00-design.md` or `01-plan.md`. Per the protocol in
`vitalforge-agent-prompt.md`, **this pass writes no code**. Contract tests
(Phase 2 item 2) are a separate persona's job and are not here.

**13 objections. 2 blocking, 3 high, 6 medium, 2 low.**

Every claim below was re-checked against the working tree. Where the design
represented something as "verified by running the code," I re-ran it. Two of
those re-runs falsified a design claim; those are recorded as defects in Phase 0,
not as documentation nits.

Verification commands are shown inline so a reviewer can re-run them. Anything I
checked that **held** is recorded in §3 — a review that reports only failures is
not a credible review.

---

## 0. Mandatory-target coverage

The prompt and `01-plan.md` §8 name six targets. Each has a real attempt and a
disposition; none is dispositioned "looks fine."

| # | Mandatory target | Finding | Disposition |
|---|---|---|---|
| 1 | Can the bearer path weaken the cookie path? (§2.1) | **F7** | Yes — via **revocation**, not authentication. Must-fix (docs + one env row) before Track A merges. |
| 2 | Partial Garmin success is "structurally impossible" (§5.3) | **F3** | Premise falsified by the design's own §3.7. Must-fix-before-Phase-3. |
| 3 | Dedup window vs. two real weigh-ins 10 min apart (§3.7) | **F1, F2, F12** | 10-minute case is genuinely safe. But dedup **does not work under concurrency at all** (F1, blocking) and silently destroys data on a batch flush (F2, blocking). |
| 4 | §2.5 A3 "resolved by ground rule" | **F6** | Ground rule is right; the *mitigation* is missing. Hides a silently-open server. Must-fix-before-Phase-3 (one log line). |
| 5 | Deliberate 400/422 inconsistency (§3.1) | **F8** | Keeping the legacy `unit` 400 is fine. **Extending** it to a brand-new bound is not, and the PWA cannot render a 422 at all. Must-fix-before-Phase-3. |
| 6 | Enrichment re-push (§3.7) | **F9** (+ F1, F3) | The risk is worse than stated *and* the plan's escape hatch does not exist as described. Must-fix-before-Phase-3. |

Per the protocol's "at least one objection against something you are confident
about": **F1**, **F3**, **F4**, and **F5** each attack a claim the design states
with high confidence ("verified", "Confirmed", "RESOLVED", "structurally
impossible", "cannot"). F1 and F3 are falsified by evidence, not by argument.

---

## 1. Blocking

### F1 — The dedup design is TOCTOU-racy and fails the exact scenario it exists for

**Severity: blocking.** **Attacks:** §3.7, §5.5, `01-plan.md` B4. **Target 3, 6.**

**Attack.** §3.7 specifies dedup as *read for duplicate → maybe return early →
push to Garmin → write row*. That is check-then-act across multiple `await`
points with no transaction, no lock, and no unique constraint. §5.5's motivating
scenario is literally **"Two bridges POSTing the same weigh-in seconds apart."**
Two such POSTs both execute the `SELECT` before either executes the `INSERT`,
both see no duplicate, and both push to Garmin. The feature does not merely
degrade under its motivating scenario — it produces exactly the outcome it was
built to prevent.

This is doubly damning because §3.3 **rejected `PRAGMA table_info` for the
migration specifically on TOCTOU grounds** ("both can observe 'absent' and both
then attempt the `ADD COLUMN`"). The design identified the hazard, applied
attempt-and-swallow to protect a schema change that happens twice in the
system's life, and then used the unprotected check-then-act pattern for the path
that runs on every weigh-in.

**Evidence.** No unique constraint exists on `weight_log` — confirmed against the
live schema, not just the source:

```
$ grep -n "UNIQUE\|CREATE INDEX" tests/fixtures/production_schema.sql
(no results)
```

The route is interleavable: `vitalforge-weight/app.py:95-103` does
`await get_db()` → `await db.execute(...)` → `await db.commit()` →
`await db.close()`, four suspension points between a would-be dedup `SELECT` and
its `INSERT`. A single uvicorn worker (`vitalforge-weight/Dockerfile:19` — no
`--workers` flag) is sufficient; asyncio interleaving alone breaks it.

I modelled the §3.7 route faithfully — aiosqlite, one connection per request,
WAL, awaits at every I/O point — and ran the two-bridge case concurrently
against a serialized control. Repro
(`<scratchpad>/dedup_race.py`, re-runnable):

```python
async def post_weight(grams, tag):
    """§3.7: read for duplicate -> maybe return early -> push -> write row."""
    now = datetime.now(timezone.utc)
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, timestamp FROM weight_log "
            "WHERE ABS(weight_grams - ?) <= 50 "
            "AND julianday(timestamp) >= julianday('now','-60 seconds') LIMIT 1",
            (grams,))
        dup = await cur.fetchone()
    finally:
        await db.close()

    if dup:
        return f"{tag}: deduplicated -> existing id={dup['id']}"

    PUSHES.append((tag, grams))                      # Garmin upload
    db = await get_db()
    ...  # INSERT + commit
```

Result — the same two POSTs, concurrent vs. sequential:

```
--- CONCURRENT (asyncio.gather), same weigh-in ---
['bridgeA: stored new row', 'bridgeB: stored new row']
rows in weight_log : 2
Garmin uploads     : 2 [('bridgeA', 84096), ('bridgeB', 84100)]

--- SEQUENTIAL (control) ---
bridgeA: stored new row
bridgeB: deduplicated -> existing id=1
rows in weight_log : 1
Garmin uploads     : 1 [('bridgeA', 84096)]
```

The control proves the dedup *logic* is correct and that the concurrent failure
is the race, not a bad predicate.

Note the second-order damage: two Garmin uploads is the **same double-record
outcome** §3.7's enrichment caveat agonises over — arrived at by a completely
different route that the design never considers. And every one of `01-plan.md`
B4's eleven named tests issues one POST at a time, so the entire package can
ship green with the feature broken.

**Disposition: must-fix-before-Phase-3.** B4 cannot be planned as written. The
requirement is narrow and I am deliberately not designing past it: **the
duplicate check and the row insert must be a single atomic operation**, so that
no two concurrent requests can both observe "no duplicate."

Two candidate shapes, for the implementer to choose between:

- `BEGIN IMMEDIATE` spanning the select and the insert on one connection,
  keeping §3.7's *read → push → write* ordering intact.
- A partial unique index on a quantised `(weight_grams, timestamp)` bucket,
  letting SQLite arbitrate and treating the constraint violation as the dedup
  hit.

**A warning about the obvious third option:** moving the Garmin push to *after*
the row is committed makes the transaction trivially short, and will look like
the cheapest fix. It is not free — it creates rows that exist at
`synced_to_garmin = 0` with nothing in the system that will ever set them to 1,
which is the same defect as **F9(b)** landing in the same reconciliation vacuum
established in **F3**. If B4 goes that way, it must ship with a repair path, and
that is a larger change than it appears.

Either way B4 needs a named concurrency test. `01-plan.md` already demonstrates
the authors know how to write one (`test_concurrent_init_db_both_succeed`), so
the omission here is an oversight, not a capability gap.

---

### F2 — Receipt-time dedup + no `measured_at` silently destroys real data on a batch flush, and the pinned contract tells the client that is success

**Severity: blocking.** **Attacks:** §3.1, §3.7, §4.5 rule 4. **Target 3.**

**Attack.** The prompt's mandatory question is "what does the window do to two
*real* weigh-ins ten minutes apart?" §3.7's answer is correct — 600 s is well
outside 60 s, both stored, and `01-plan.md` B4 tests it
(`test_weighins_600s_apart_both_stored`). **That is the wrong question to have
stopped at**, because the window is not measured from when the weigh-ins
*happened*. §3.1 declines to add `measured_at`, so `timestamp` is
`datetime.now(timezone.utc)` at **server receipt** (`app.py:80-81`). The window
therefore keys on delivery time, not measurement time.

Construct the failure: a store-and-forward client (a phone that was out of BLE
or network range) buffers four days of weigh-ins and flushes them on reconnect.

The mechanism, stated precisely: **a burst delivery puts N *distinct* weigh-ins
inside a single 60-second window, so the dedup predicate is asked to compare
pairs it was never designed to see.** §3.7 reasoned entirely about the same
physical weigh-in arriving twice; it never considers different weigh-ins
arriving together. Once they are co-resident in one window, the only thing
separating them is the ±50 g tolerance — a quantity chosen to absorb *unit
conversion rounding between two bridges*, and now silently repurposed as the
sole discriminator between two different days' measurements. Any collision
discards the later reading permanently: no row, no Garmin push, no error.

How often that collision fires is a secondary question and not the argument:
50 g is 0.11 lb, so it takes two buffered readings landing within about a tenth
of a pound of each other. That is not every flush. But it does not need to be —
the loss is silent, permanent, unlogged, and reported to the client as success,
so it is undetectable at any rate above zero, and the rate rises with the number
of readings in the burst.

The client cannot detect it. §4.4 returns **200** with `deduplicated: true`, and
§4.5 rule 4 instructs Bascule that this is safe: *"Retrying after a timeout is
safe: the 60-second dedup window (§3.7) collapses a redelivery of the same
weigh-in."* A conforming client treats the 200 as durable and drops its local
copy. The loss is silent, permanent, and client-side.

This is not a hypothetical future: the prompt's Phase 5 deliverable explicitly
tracks *"the state of the replay-path dependency (what Bascule's milestone 7 can
now assume)"*. Replay is a known, planned Bascule capability. §3.1 records
`measured_at`'s absence as "a known gap so Bascule's milestone planning does not
assume it exists" — but it never connects that gap to dedup, and §3.7 never
considers that its window is anchored to the wrong clock.

**Evidence.** `vitalforge-weight/app.py:80-81`:

```python
now = datetime.now(timezone.utc)
timestamp = now.isoformat()
```

No client value is consulted. §3.7's key is "an existing row whose `timestamp` is
within 60 seconds of **now**" — receipt time on both sides. §4.3's request table
has no time field; §4.4 confirms *"`timestamp` is server-assigned UTC ISO-8601 —
the client's clock is not consulted."*

**Disposition: must-fix-before-Phase-3**, and it is the more urgent of the two
blockers to decide, because **it is baked into the Bascule contract** (§4.2–§4.5),
which §4's preamble pins the moment Track A merges. F1 is an implementation
defect that can be fixed in B4 without touching the wire format; F2 changes the
wire format, and changing a pinned contract after Bascule has built against it is
the expensive failure mode this whole phased process exists to avoid.

Two acceptable resolutions, in preference order:

1. **Add optional `measured_at` (ISO-8601, client-supplied) to the DTO now**, key
   dedup on it when present and on receipt time when absent, and store it in a
   sixth nullable column. This removes the anchor bug at the root, and §3.1
   rejected it as scope without the dedup interaction in view.

   **Do not cost this as "one nullable column."** §3.1 correctly lists three
   things a client timestamp changes — the dedup key, the Garmin `timestamp`
   argument, and the contract — and only the first is addressed above. The
   second is real work: `push_weight` derives the FIT timestamp from server
   `now` (`shared/garmin_client.py:64-67`), so if `measured_at` becomes the
   dedup anchor it must also become the pushed timestamp, or the local row and
   the Garmin record disagree about when the weigh-in happened. That drags in
   **F9(a)**'s second-resolution, timezone-stripped `strftime` conversion and
   **F10**'s precision caveat. Also note a client-supplied time is now untrusted
   input on a boundary (§3.2's framing), needing its own bounds — a clock-skewed
   phone sending a timestamp years out re-keys dedup and mis-dates the Garmin
   record.
2. If `measured_at` stays out of scope, **§4.5 rule 4 must be rewritten** to tell
   Bascule that a `deduplicated: true` response is *not* proof the reading was
   stored, and Bascule must not flush buffered readings in a burst. That pushes a
   permanent correctness burden onto a client in another repo to protect a server
   shortcut, which is why it is the second choice.

---

## 2. High

### F3 — §5.3's "structurally impossible" partial success is manufactured by the design's own §3.7

**Severity: high.** **Attacks:** §5.3, §3.5, §3.7. **Target 2.**

**Attack.** §5.3 states: *"There is no sequence of events in which Garmin accepts
the weight and rejects the composition — the upload either lands or it does
not."* It uses that to justify a decision: *"We deliberately do not add a
weight-only retry fallback... It would manufacture the partial success the system
otherwise cannot have."* The premise is false, and it is falsified inside the
same document.

**Path A — the design's own enrichment re-push.** §3.7 sub-case 2: POST 1
(weight-only) uploads to Garmin successfully. POST 2 arrives with composition,
enriches the row, and **re-pushes**. If that second upload fails, Garmin holds
the weight and does **not** hold the composition. That is precisely
weight-succeeded / composition-failed. §3.7 even specifies the handling
(`synced_to_garmin` back to 0, `garmin_error` alongside `enriched: true`) — so the
design implements the state §5.3 declares cannot exist, and §3.7's caveat, which
does try to reconcile the two sections, frames the tension only as "Garmin may
end up holding two records." It never notices it also created the partial
success. Two sections of one design doc contradict each other, and the false one
is load-bearing for a decision.

**Path B — the lost response.** `add_body_composition` is one HTTP POST
(verified below). If the server commits the upload but the response is lost —
connection reset, read timeout — the client raises, `app.py:89-92` catches it,
stores `synced_to_garmin: 0`, and returns `garmin_error`. Garmin has the data;
we record that it does not. §5.3 reasons entirely about *transport atomicity* and
never about *acknowledgement*. Under Track B this is worse than today: the row is
now inside a dedup window, so the next bridge's POST hits sub-case 2, enriches,
and re-pushes — producing the double Garmin record from a path nobody modelled.

**Path C — acceptance is not the same as ingestion.** The upload being atomic
says nothing about Garmin's server-side handling of individual FIT fields. The
design knows this: §3.5 justifies reading composition back from Garmin because it
*"makes the dashboard show what Garmin actually **accepted**, turning every chart
into a round-trip verification of the push."* That sentence only makes sense if
Garmin can accept some fields and not others — which is the partial success §5.3
calls impossible. And nothing detects it: `synced_to_garmin: 1` is set on HTTP
success, so a silently-dropped field is recorded as fully synced.

**Evidence.** G3 itself is correct — one call, one multipart upload:

```
$ python3 -c "import inspect,garminconnect; print(inspect.getsource(garminconnect.Garmin.add_body_composition))"
...
        fitEncoder.write_weight_scale(dt, weight=weight, percent_fat=percent_fat, ...)
        fitEncoder.finish()
        files = {"file": ("body_composition.fit", fitEncoder.getvalue())}
        return self.client.post("connectapi", url, files=files, api=True)
```

So the *mechanism* claim holds. The *conclusion* drawn from it does not.

**Disposition: must-fix-before-Phase-3 (documentation + one behavior decision).**
The design conclusion — don't add a weight-only retry fallback — is still
**correct**, and I am not asking for the fallback. What must change:

- §5.3's premise must be restated accurately ("weight and composition cannot
  fail independently *within a single upload*"), because as written it is used to
  close off analysis that Paths A–C reopen.
- Path B is a real, un-modelled divergence and needs a stated behavior. At
  minimum §4.4's *"`synced_to_garmin: false` is a reconciliation signal"* must be
  honest that no reconciliation process exists.
- Path A means the enrichment re-push cannot be assessed independently of §5.3.
  See F9.

---

### F4 — §3.5/§4.2's "RESOLVED / Confirmed, not guessed" covers key *names* only; the *units* were never observed, and the fixture is green under either unit

**Severity: high.** **Attacks:** §3.5, `01-plan.md` §4.2 and B5.

**Attack.** §3.5 is emphatic: *"**Confirmed:** `bodyFat`, `bodyWater`,
`boneMass`, `muscleMass`"*, and `01-plan.md` B5 says *"Key names are confirmed,
not guessed."* Both are true and both are about **names**. Neither document
anywhere states the **units** of `boneMass` and `muscleMass` on the read path,
and the units cannot have been observed, because both documents also state that
every composition field in the live account is `null`.

That matters because the one adjacent field whose unit *is* observable is in
**grams**, not kg. `sync.py:221` reads `latestWeight.weight` and writes it
straight into `weight_history.weight_grams`, and the dashboard exposes it as
`"weight": ("weight_history", "weight_grams")`. So Garmin's weigh-in payload uses
grams for mass. It is therefore likely — not certain — that `boneMass` and
`muscleMass` are grams too.

The failure is structural rather than probabilistic: **B5's tests cannot detect
it either way.** `tests/fixtures/garmin/weigh_ins.json` carries synthetic values
chosen by the fixture author (`boneMass: 3.2`, `muscleMass: 34.0` against
`weight: 81200`, i.e. kg alongside grams in one object). `sync.py` will store
whatever number is in the fixture, and
`test_sync_populates_composition_from_weigh_ins_fixture` will assert it round-trips.
Green under kg, green under grams, green under furlongs. The one thing the test
suite exists to catch here is the one thing it structurally cannot.

If the units are grams, the dashboard plots bone mass as ~3200 and muscle mass as
~34000 against a `weight_log` that stores 3.2 kg, silently, with no error.

**Evidence.**

```
$ grep -n "weight_grams=weight_g\|weight_g = latest" vitalforge-dashboard/sync.py
221:            weight_g = latest.get("weight")
227:                    weight_grams=weight_g,
$ grep -n '"weight":' vitalforge-dashboard/app.py
40:    "weight": ("weight_history", "weight_grams"),
```

Fixture, showing kg and grams side by side in one object:

```json
"latestWeight": { "weight": 81200, "bmi": 24.1, "bodyFat": 18.4,
                  "bodyWater": 55.2, "boneMass": 3.2, "muscleMass": 34.0 }
```

The fixture's own `_comment` concedes the values are synthetic; it does not
concede that this makes the unit unverifiable by test.

**Disposition: must-fix-before-Phase-3**, as an added *verification requirement*
rather than a design change. The Phase 3 live checkpoint (`01-plan.md` B3) is
already scheduled as "one real weigh-in with composition, verified visible in
Garmin Connect" — its brief must be extended to **read back the raw
`get_weigh_ins()` response after that push and record the observed units of
`boneMass`/`muscleMass`**, because that push is the first and only event that can
ever make them non-null. Until then, §3.5, §4.2 and B5 should say "key names
confirmed; units unverified" rather than "RESOLVED." B5 must not merge ahead of
that checkpoint on the strength of a fixture that cannot fail.

---

### F5 — `FakeGarminClient` can never fail a signature mismatch, and `garminconnect` is unpinned

**Severity: high.** **Attacks:** §1.5, `01-plan.md` B3 and §5.

**Attack.** §1.5 presents the `add_body_composition` parameter/unit table as
verified, and it is — I re-derived it and it is correct. The problem is what
protects it going forward. Track B's entire value depends on four kwarg names
(`percent_fat`, `percent_hydration`, `bone_mass`, `muscle_mass`) reaching a
library the repo does not pin, and **no test in the plan can ever notice if they
stop matching.**

`FakeGarminClient.add_body_composition` signs as
`(self, timestamp, weight, **kwargs)`. `01-plan.md` B3 sub-task 0 correctly fixes
it to *record* the kwargs instead of discarding them — but recording them does
not validate them. A `**kwargs` fake accepts every possible kwarg name. All ten
of B3's mapping tests assert that our code passed `percent_fat=18.4` to *the
fake*; none asserts that the real library has a parameter by that name. If
upstream renames it, every test stays green and production silently stops
recording body fat.

The dependency is a floor, not a pin: `garminconnect>=0.2.38`. Containers resolve
it at image-build time, so two builds a month apart can ship different versions
from identical source, and CI installs from the same unpinned line. The ground
rule *"Do not modify the Garmin client's auth flow. It is fragile,
reverse-engineered, and working"* is an argument **for** pinning a
reverse-engineered dependency, and the design never raises it.

To be fair to Phase 0: §1.5 openly states the path it inspected
(`/home/user/.local/lib/python3.14/site-packages`), so it did not conceal that it
read the host's copy rather than the container's — and the existing production
call has evidently worked across the `>=0.2.38` range, so I have no evidence of
actual drift. The finding is the **blind spot**, not a claim that the table is
currently wrong.

**Evidence.**

```
$ grep -n "garminconnect" vitalforge-*/requirements.txt
vitalforge-weight/requirements.txt:5:garminconnect>=0.2.38
vitalforge-dashboard/requirements.txt:5:garminconnect>=0.2.38

$ grep -n "def add_body_composition" tests/conftest.py
45:    def add_body_composition(self, timestamp, weight, **kwargs):
```

Host resolves to `0.3.11` on Python 3.14; containers are `python:3.12-slim`
(`vitalforge-weight/Dockerfile:1`) and resolve independently.

**Disposition: must-fix-but-can-land-as-followup**, with one line of it pulled
into B3. Pin `garminconnect==0.3.11` in both `requirements.txt` files (a
one-line, reversible change that does not touch the auth flow and so does not
trip the escalation rule), and add one conformance test to B3 that closes the
blind spot permanently:

```
assert set(inspect.signature(garminconnect.Garmin.add_body_composition).parameters)
       >= {"percent_fat", "percent_hydration", "bone_mass", "muscle_mass"}
```

That single assertion is worth more than B3's ten fake-based mapping tests
combined, because it is the only one testing something we do not control.

---

## 3. Medium

### F6 — A3's "resolved by ground rule" is right, but it hides a silently-open server the token client cannot detect

**Severity: medium.** **Attacks:** §2.5 A3, §2.4, §6. **Target 4.**

**Attack.** I accept the reasoning. `VITALFORGE_PASS` as the single master switch
is correct, and making a token load-bearing when `PASS` is empty would break
existing deployments — the ground rule genuinely resolves the *design* question.
The footgun is not in the decision; it is in what the decision leaves
undetectable.

Configuration A3 is reachable by ordinary operator error on a **live system with
real health data**: JD deploys Track A, sets `VITALFORGE_API_TOKEN` for Bascule,
and at some later point `VITALFORGE_PASS` ends up empty — a `.env` edit, a
secrets mount that fails to populate, a `docker-compose.prod.yml` that omits the
variable. Both services now serve **completely unauthenticated** on every path.

The operator has *positive evidence that auth is working*: Bascule's bearer
requests keep succeeding (cell A3-C3 → allow). A token client cannot distinguish
"my token is being validated" from "auth is off and everything is allowed" —
both are 200. Neither can a `curl` with a deliberately wrong token, which also
returns 200 in A3 and 401 in A1. The only way to notice is to already suspect it.

The mitigation is one log line at import — and §2.4 has explicitly ruled it out:
*"`shared/auth.py` currently logs nothing... The new code adds no log
statements."* That policy is well-argued for *failed token attempts* (log volume,
and the slippery slope to logging token prefixes). It is being applied by
extension to a **startup configuration warning**, which shares neither problem:
it fires once per boot, contains no credential, and is exactly the kind of thing
the repo's other startup paths already log (`app.py:27`, `app.py:29`,
`app.py:33`).

Finally, the prompt lists *"Any auth behavior matrix cell where the safe answer
is ambiguous"* as a stop-and-ask-JD trigger. §6 explicitly declines to escalate
A3 (*"Not escalated, though it looked like a candidate"*). I think that call was
right on the merits and wrong on the process — the design resolved an ambiguous
cell in JD's favour without asking JD, on a system holding his real data.

**Evidence.** `shared/auth.py:23-24, 39-41` — `_is_auth_configured()` returns
`bool(_PASS)` and `get_current_user()` short-circuits to `"anonymous"` before any
token logic can run. Confirmed empirically that the middleware then passes
everything through (`shared/auth.py:174-175`).

**Disposition: must-fix-before-Phase-3.** Land in A1 or A2, one statement:

```python
if _API_TOKEN and not _PASS:
    logger.warning(
        "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty — "
        "auth is DISABLED and the token is inert. Set VITALFORGE_PASS to enable auth."
    )
```

The matrix is unchanged; A3 stays open-access. This costs one line and converts a
silent, undetectable open server into a loud one. §2.4's no-logging policy should
be narrowed in writing to "no logging on the per-request auth path", which is
what it actually means to protect.

---

### F7 — The bearer path *does* weaken the cookie path — through revocation, not authentication

**Severity: medium.** **Attacks:** §2.1, §2.5, §4.1. **Target 1.**

**Attack.** §2.1's specific claim is narrow and I could not break it:
*"step 2 returning `False` always falls through to step 3, so a wrong token never
blocks a valid cookie... step 2 has no side effects and no early `return None`."*
I tried to construct a raise inside `_bearer_token_valid` that would escape
`get_current_user()` and turn a valid-cookie request into a 500 — the classic way
an added pre-check weakens an existing path. It does not work: Starlette decodes
headers as latin-1, so every header value is in U+0000–U+00FF, and `.encode("utf-8")`
on such a string cannot raise. `partition`, `.lower()`, and `.strip()` cannot
raise. **On authentication, §2.1's answer is correct.**

The weakening is on a different axis the design never considers: **revocation**.

Today there is exactly one lever that invalidates every credential in the system.
`_serializer = URLSafeTimedSerializer(_SECRET)` (`shared/auth.py:20`) — rotating
`VITALFORGE_SECRET` invalidates every outstanding `vf_session` cookie
simultaneously. That is the "log everyone out" button, and after Track A it is
**no longer complete**: a leaked bearer token survives a `VITALFORGE_SECRET`
rotation untouched, and rotating `VITALFORGE_API_TOKEN` does not invalidate
cookies. An operator responding to a suspected compromise by rotating the secret
— the obvious move, and the only one the README will describe — leaves a
long-lived credential live.

The blast radius is not small. §4.1 tells Bascule the token *"grants full API
access to both services, including `DELETE /api/weight/{id}`"*, and it has **no
expiry** while cookies expire in 30 days (`_MAX_AGE`). So Track A introduces a
strictly more powerful, strictly longer-lived credential and simultaneously
breaks the single-lever revocation story, and neither §2.1 nor §4.1 mentions the
interaction.

**Secondary, and a factual error in a table certified complete.** §2.5 states:
*"`/health`, `/auth/*`, and `/static/*` are exempt in all 40 cells and are never
affected by any configuration."* False once the bearer check moves inside
`get_current_user()`, because `GET /auth/login` calls it directly
(`shared/auth.py:145`) and branches on the result. Verified against the real app:

```
$ python3 probe_auth.py
/api/thing     -> 500 'text/plain; charset=utf-8' 'Internal Server Error'   # D1 confirmed
/page          -> 302 None ''
/auth/login    -> 200 'text/html; charset=utf-8' '<!DOCTYPE html>...'
/auth/login with valid session -> 302 /
```

A request presenting a valid bearer token to `/auth/login` will get a 302 to `/`
instead of the login page. Harmless in practice — browsers do not send bearer
headers — but §7 Gate 1 rests on "40 of 40 cells specified," and the exemption
claim underneath it is wrong.

**Disposition: must-fix (docs) before Track A merges; the ordering itself is
accepted as designed.** No code change to §2.1. Required:

- The README **Authentication** section (A3) must state both revocation
  procedures and that neither implies the other. This is cheap now and expensive
  after an incident.
- §4.1 should tell Bascule the token has no expiry *and* is not covered by a
  secret rotation, since Bascule's operators will reason about credential
  lifetime.
- Correct §2.5's exemption sentence to scope it to middleware enforcement.

Worth JD's attention as a follow-up (not blocking): a token that can
`DELETE /api/weight/{id}` is broader than Bascule needs, which only POSTs.

---

### F8 — The 400/422 split is defensible for `unit` and indefensible for the *new* bound — and the PWA cannot render a 422 at all

**Severity: medium.** **Attacks:** §3.1, §4.4. **Target 5.**

**Attack.** Three separate problems, of increasing seriousness.

**(a) The stated justification does not cover the new error.** §3.1 keeps `unit`
as a route-level 400 because *"Converting it to a `Literal` would turn the
existing 400 into a 422 and break `tests/test_weight_api.py:48-50`. Backward
compatibility wins."* Fine. But §3.2 then adds a **brand-new** validation — the
2–500 kg post-conversion bound — and §4.4 makes it a 400 as well, explicitly:
*"These two are the only 400s."* A validation that has never existed has no
backward-compatibility obligation to anything. The design extended a legacy quirk
into new surface area using a rationale that by construction cannot apply to it.

**(b) The technical premise for (a) is false.** §3.1 puts the weight bound in the
route *"not on the field, because the limit is in kg and the input may be lbs."*
That is a false dichotomy: Pydantic's `model_validator(mode="after")` handles
derived/cross-field constraints and produces a normal 422. Verified:

```
$ python3 -c "...model_validator(mode='after') raising ValueError; POST weight=99999..."
model_validator status: 422
body: {'detail': [{'type': 'value_error', 'loc': ['body'],
       'msg': 'Value error, weight must be between 2 and 500 kg after unit conversion', ...}]}
```

So the new bound could return a consistent 422 *and* keep the legacy `unit` 400
untouched. Nothing was trading off.

**(c) The one real client cannot display a 422.** This inverts the section's own
reasoning. The PWA renders the error body directly:

```js
// vitalforge-weight/templates/index.html:370
showToast(data.detail || "Failed to log weight", "error");
```

On a 400, `detail` is a string and the user sees "unit must be 'lbs' or 'kg'". On
a 422, `detail` is an **array of objects**. Verified:

```
$ node -e 'const detail=[{type:"extra_forbidden",loc:["body","bodyFat"],msg:"..."}];
           let el={}; el.textContent = detail || "Failed"; console.log(String(el.textContent));'
toast shows: [object Object]
```

Track B adds five new 422 sources to this endpoint (`extra="forbid"`, four range
bounds, and the `source` `Literal`), and B2 adds `source: "pwa"` to this very
template. §3.1 checks that `extra="forbid"` will not *reject* the PWA — it never
checks whether the PWA can *display* the new errors. It cannot. The design's
instinct to protect the PWA was right; it protected the wrong half.

**Disposition: must-fix-before-Phase-3, scoped small.**

1. Move the new weight-range bound to a `model_validator` → 422, using (b). Keep
   `unit` as 400 exactly as designed, with `01-plan.md` B2's
   `test_invalid_unit_still_returns_400_not_422` unchanged. Then §4.4 has **one**
   legacy 400, honestly labelled, rather than a growing family.
2. One line in the PWA to flatten an array `detail` before display — B2 is
   already editing that file.
3. Stop describing the `unit` 400 as backward compatibility in §4.4. The only
   thing depending on it is `tests/test_weight_api.py:48-50`, a test this team
   owns, which asserts `resp.status_code == 400` and nothing about the body. No
   external client parses it. Call it what it is: a legacy quirk retained because
   changing it has no benefit.

---

### F9 — The enrichment re-push risk is understated, and the plan's escape hatch does not exist as described

**Severity: medium.** **Attacks:** §3.7's enrichment caveat, `01-plan.md` B4. **Target 6.**

**Attack.** §3.7 is unusually honest here — it flags the double-upload, admits
Garmin's collapsing behavior is *"unverified and unverifiable without a live
account"*, and names the Phase 3 checkpoint. I am attacking it harder on two
axes it did not cover.

**(a) The stated mitigation is weaker than it sounds.** §3.7 reuses the original
timestamp *"precisely so Garmin has the best chance of treating it as the same
weigh-in."* But `push_weight` formats it as
`timestamp.strftime("%Y-%m-%dT%H:%M:%S")` (`shared/garmin_client.py:67`) — second
resolution, and **timezone-stripped**, so a UTC instant is handed to
`datetime.fromisoformat()` inside `garminconnect` as a naive local-looking value.
That is pre-existing and out of scope to fix, but it means the re-push's
"identical timestamp" property depends on a lossy format conversion that nobody
has tested for idempotency. Worse, the same truncation means **two genuinely
different weigh-ins in the same second also collapse to one FIT timestamp** — so
if Garmin *does* dedup on timestamp, it dedups slightly more aggressively than we
intend, and if it does not, our mitigation buys nothing. The design asserts a
"best chance" without establishing the mechanism it depends on.

**(b) The plan's fallback is not one line.** `01-plan.md` B4 states: *"if the
Phase 3 checkpoint shows Garmin keeps both records, the fallback (enrich locally,
do not re-push) is a **one-line change plus deleting two tests**."* That
understates it materially. §3.7 defines `synced_to_garmin` as *"this row's
current contents are in Garmin."* Under the fallback, an enriched row's contents
are by definition **not** in Garmin, so it must be set to 0 — and nothing in the
system will ever set it back to 1, because the fallback's whole point is that
nothing re-pushes. The result is a permanent class of rows marked unsynced with
no repair path, in a system that (per F3) has no reconciliation process at all,
while §4.4 tells Bascule `synced_to_garmin: false` is *"a reconciliation signal,
not a failure."* Combined with §3.5 — composition reaches the dashboard only via
the Garmin round-trip — that composition is then invisible everywhere except the
`weight_log` columns §6 D2 kept purely for failure forensics.

§3.7's own prose actually concedes the outcome (*"accepting that composition never
reaches Garmin for a weigh-in that arrived split across two POSTs"*). The plan
does not carry that concession forward, and B4's estimate is written as though it
were a toggle. Whoever executes B4 after a bad checkpoint result will discover the
gap under time pressure.

**Note the interaction with F1:** under concurrency the two POSTs may not even
reach the enrichment path — both may see no duplicate and store separate rows.
So the enrichment logic's correctness is currently untestable in the scenario it
targets until F1 is fixed.

**Disposition: must-fix-before-Phase-3 (planning correction, not a redesign).**
The decision to ship the re-push and gate the fallback on the live checkpoint is
**sound** and I would not change it. What must change: `01-plan.md` B4 must state
the fallback's real cost — including that it strands rows at
`synced_to_garmin = 0` permanently — so the checkpoint is evaluated with the true
alternative in view. If JD wants the fallback to be genuinely cheap, the third
option §3.7 rejected (store a second row) deserves a second look, because "two
local rows plus two Garmin records" is at least self-consistent, whereas "one
local row that permanently claims to be unsynced" is not.

---

### F10 — FIT encoding truncates, so §3.4's "pass-through" is lossy and §3.5's "round-trip verification" is guaranteed to mismatch

**Severity: medium.** **Attacks:** §3.4, §3.5, §1.5 G4.

**Attack.** §3.4's mapping table says `body_fat_pct` → `percent_fat` is
"pass-through" and §1.5 G4's analysis of the encoder stops at the *overflow*
risk. It missed that the encoder **truncates rather than rounds**:
`FitBaseType.pack` does `value = int(value)` after `_build_content_block`
multiplies by the scale. With binary floats, that loses a hundredth on ordinary
values — including the exact one used in the design's own contract example.

Verified:

```
$ python3 -c "print(int(18.4*100), int(55.2*100), int(40.1*100), int(3.2*100))"
1839 5520 4010 320
```

`body_fat_pct: 18.4` — the literal value in §4.3's full-payload example and
§4.4's 200 response — is transmitted to Garmin as **18.39**. `weight_log` stores
18.4. They will never agree.

This lands squarely on §3.5's justification for reading composition back from
Garmin: *"turning every chart into a round-trip verification of the push."* The
round trip is lossy by construction, so the "verification" will show a permanent
sub-unit discrepancy on roughly half of all values, and anyone later building
reconciliation on top of it (or on §3.7 sub-case 3's "a different non-`NULL`
value for a field already set", if it is ever compared against Garmin's read-back
rather than only against incoming POSTs) inherits a guaranteed false-positive
source.

For the record this is **pre-existing for weight** — `84.096 kg` already encodes
as `84.09` today — but it is new for all four composition fields, and it is the
first time the design has claimed the round trip verifies anything.

**Evidence.** `garminconnect/fit.py:179-183` and `:249-251`:

```python
if basetype["#"] in (1, 2, 3, 4, 5, 6, 10, 11, 12):
    value = int(value)          # truncation, not round()
...
elif scale is not None:
    value *= scale
```

**Disposition: accepted-risk, with two documentation corrections.** A hundredth
of a percent is clinically irrelevant and I am **not** asking for a fix — the
encoder is third-party, and pre-rounding our values before the call would be a
change to a fragile reverse-engineered path for no user-visible gain. What must
change is the two places the design overstates precision: §3.4's "pass-through"
should say values are truncated to 0.01 by the FIT encoder, and §3.5's
"round-trip verification of the push" should not be relied on as an equality
check. If B3 ever adds an assertion comparing a pushed value to a read-back
value, it must use a tolerance.

---

### F11 — B5 names the column `bone_mass`, dropping the `_kg` suffix the design calls "the only defense"

**Severity: medium.** **Attacks:** §3.5, §4.3, `01-plan.md` B5.

**Attack.** §4.3 makes the naming convention load-bearing in unusually strong
terms: range validation *"cannot detect pounds sent where kilograms are
expected... The `_kg` suffix is **the only defense**; do not strip it when mapping
from the device SDK."* §3.2 repeats it: *"Only the field name defends against
that, which is why it is `bone_mass_kg` and not `bone_mass`."*

§3.5 and `01-plan.md` B5 then add the columns
`body_water REAL, muscle_mass REAL, bone_mass REAL` to `weight_history` — and
register `METRIC_TABLES["bone_mass"] = ("weight_history", "bone_mass")`. The
design strips the suffix it just declared to be the only defense, in the same
document, for the same quantity.

The result is that one system stores the same physical measurement in two tables
under two names with no unit marker on one of them — `weight_log.bone_mass_kg`
(kg, from our DTO) and `weight_history.bone_mass` (unit unverified, likely grams
per **F4**). Anyone joining, comparing, or charting these has nothing in the
schema to warn them. That is precisely the failure mode §4.3 wrote three
sentences to prevent.

**Evidence.** Convention asserted at `00-design.md` §3.2 and §4.3; violated at
§3.5 item 1 and item 3, and carried into `01-plan.md` B5's files-touched list and
its `test_metric_tables_includes_body_water_muscle_bone`.

**Disposition: must-fix-before-Phase-3, trivial.** Name the `weight_history`
columns for their observed units once **F4** settles them — `bone_mass_g` /
`muscle_mass_g` if grams, `_kg` if kg. The cost is zero: these columns do not
exist yet, nothing reads them, and the metric key in `METRIC_TABLES` is a
separate string that can stay `bone_mass` for the API surface if a suffix in the
URL is unwanted. Doing this after B5 merges means an `ALTER ... RENAME`, which
the ground rules forbid as non-additive.

---

## 4. Low

### F12 — The ±50 g boundary is unspecified and deliberately untested

**Severity: low.** **Attacks:** §3.7, `01-plan.md` B4. **Target 3 (boundary).**

**Attack.** §3.7 says the key is `weight_grams` *"within ±50 g"*. Neither
document says whether 50 g exactly collapses. `01-plan.md` B4 tests
`test_dedup_tolerance_49g_collapses` and
`test_dedup_tolerance_51g_creates_second_row` — bracketing the boundary from both
sides while never landing on it. An implementer will pick `<=` or `<` by coin
flip and both plans' tests will pass.

It is not purely academic. §4.5 rule 4 makes retry-safety a **contract
guarantee** to Bascule, and that guarantee is defined by this predicate. A
0.05 kg-resolution scale straddles it exactly: 84.10 kg vs 84.15 kg is 50 g.

Related, and worth stating since the prompt asked for the unit-conversion
assumptions to be stressed: **50 g is 0.110 lb, which is larger than one display
increment on a 0.1 lb scale (45.36 g).** So two genuinely different consecutive
readings one display step apart collapse. §3.7's justification does not cover
this — it argues the residual false positive is *"a real step-off/step-on re-weigh
within 60 s that reports the **exact same gram value**... the two records would be
byte-identical."* That sentence describes an **exact-match** key. The design's key
is a tolerance, so the collapsed records are *not* byte-identical and the stated
"loses nothing" argument does not hold for the 1–49 g case. The conclusion
survives (a 45 g difference is noise), but the reasoning in the doc is for a
different design than the one specified.

**Disposition: must-fix-but-can-land-as-followup** — fold into F1's rework of B4.
Specify `abs(delta) <= 50` explicitly in §3.7, add
`test_dedup_tolerance_exactly_50g`, and correct the "Why 60 seconds" paragraph so
it argues about the tolerance it actually specifies.

---

### F13 — The artifacts this review was dispatched against are not committed

**Severity: low.** **Attacks:** process, not design.

**Attack.** The protocol says to *"dispatch it cold with only the committed
artifacts as input."* Nothing under review is committed:

```
$ git status --short
 M tests/fixtures/garmin/weigh_ins.json
?? docs/prp/
?? tests/fixtures/production_schema.sql
```

Two consequences beyond the procedural one. First, `tests/fixtures/garmin/weigh_ins.json`
— a test fixture — was modified **on `main`, uncommitted**, during Phase 0/1,
against the ground rule *"Branch-per-feature, PRs into `main`."* Second, because
it is uncommitted, **CI has never run against it**; `01-plan.md` §4.2's "suite
green at 54 passed (re-verified locally)" is a local result only. I re-confirmed
it locally (§5 below), so the claim is true — but the gate it satisfies is
supposed to be CI.

**Disposition: not-a-real-issue-for-the-design; must-fix-for-process before
Phase 3.** Commit the four artifacts on a branch — the three above plus this
file, which I have just added to the same untracked `docs/prp/` — and let CI
run. The fixture change in particular should not sit uncommitted on `main` while
Track A branches are cut from it.

---

## 5. Checked, and held

Recorded so the dispositions above are read against a real baseline rather than
an assumption that everything is broken.

| Claim | Source | Result |
|---|---|---|
| `require_auth` has zero call sites | §1.1 | **Holds.** `grep -rn "require_auth\|get_current_user" --include=*.py . \| grep -v shared/auth.py` → no results. |
| `/api/*` returns 500, not 401, when unauthenticated | §1.2 D1 | **Holds**, reproduced: `/api/thing -> 500 'text/plain' 'Internal Server Error'`, `/page -> 302`. |
| `compare_digest` raises `TypeError` on non-ASCII `str` | §1.2 D2 | **Holds.** `TypeError: comparing strings with non-ASCII characters is not supported`; the bytes form returns cleanly. |
| `hmac.compare_digest("", "")` is `True` | §1.2 D3 | **Holds.** Both empty guards in §2.2 are justified. |
| Starlette `headers.get()` returns the **first** `Authorization` | §5.2 | **Holds.** Two raw headers → `.get()` = `'Bearer JUNK'`, `.getlist()` = both. |
| `add_body_composition` kwargs, units, scales, base types | §1.5 | **Holds.** Re-derived from `garminconnect/fit.py:490-497`; §1.5's table is accurate for 0.3.11. (Durability of that: **F5**.) |
| No muscle-*percentage* field exists (G1) | §1.5 | **Holds.** Only `muscle_mass`, in kg. |
| One call → one FIT record → one multipart upload (G3) | §1.5 | **Holds** as a *mechanism*. The conclusion drawn from it does not: **F3**. |
| `None` composition fields encode as the FIT invalid sentinel | §3.4 | **Holds.** `fit.py:248-251`: `if value is None: value = basetype["invalid"]`, and the scale is correctly not applied. |
| Derived `muscle_mass_kg` max (450 kg) is under the 655.35 encoder ceiling | §3.2 | **Holds.** |
| `weight_log` has no unique constraint or index | §1.3 | **Holds** against the live schema dump. (Which is the root of **F1**.) |
| `production_schema.sql` matches `shared/database.py` with zero drift | plan §4.1 | **Holds.** Compared table by table; `weight_log` and `weight_history` DDL identical. |
| The dump cannot be loaded verbatim (`sqlite_sequence`) | plan §4.1 | **Holds** — `CREATE TABLE sqlite_sequence(name,seq)` is present at line 54. |
| `/api/metrics/{name}` f-string interpolation is not injectable | §1.6 | **Holds.** Table/column come from the hardcoded `METRIC_TABLES` dict; `days` is bound. |
| Existing queries name columns explicitly → additive migration is rollback-safe | §3.3, §5.4 | **Holds.** `app.py:98`, `:122`, `:146`; `sync.py:27-30`. |
| Suite green, lint clean | plan §4.2 | **Holds.** `pytest -q` → `54 passed, 3 deselected`; `ruff check .` → `All checks passed!`. (Not in CI: **F13**.) |
| SQLite handles the stored ISO-8601 `+00:00` timestamp correctly | *not claimed* | **Checked because §5.6 shows this repo gets this wrong elsewhere.** `datetime()` and `julianday()` both normalise `2026-08-22T15:07:36.706296+00:00` correctly, so a dedup predicate using `julianday(...)` is sound — **provided B4 does not use the naive string comparison `/api/weight/trend` relies on** (`app.py:146`, which works only because `'T' > ' '`). Worth one explicit note in B4. |

---

## 6. Summary for the implementer persona

**Do not start Phase 3 without resolving F1 and F2.** F1 means B4 as planned
ships a feature that does not work under its own motivating scenario; F2 means
the Bascule contract — which pins on Track A merge — encodes silent data loss for
a client capability (replay) the prompt already treats as planned. F2 is the more
time-critical of the two, because contract changes get expensive the moment Track
A lands.

**Track A is in good shape.** F6 and F7 are a one-line log statement and a
README/§4.1 correction; neither touches §2.1's ordering, which I attacked
directly and could not break. Track A can proceed on that basis.

**Track B needs three corrections before B2/B3/B5 are executable as written:**
F4 (units unverified, and the fixture cannot fail), F5 (`**kwargs` fake plus an
unpinned dependency), F11 (column naming, free now and non-additive later).

**Two design claims should be restated because decisions rest on them:** §5.3's
"structurally impossible" (F3) and §3.4's "pass-through" (F10).
