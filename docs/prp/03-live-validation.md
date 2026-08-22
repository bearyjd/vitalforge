# 03 — Live validation: the B3 checkpoint

**Status: auth fix deployed and confirmed working (2026-08-22 22:32 UTC).**
Both services authenticated successfully on restart — `mobile+cffi`/
`mobile+requests` still hit Garmin's 429 (that part of the rate limit
hadn't cleared yet), but garminconnect 0.3.11's cascading login chain fell
through to a later strategy and succeeded, with tokens persisted correctly
to `garmin_tokens.json`. No crash, no AttributeError. See "2026-08-22
incident: root cause and fix" below for the full story. **Still needed:**
step 1's composition push was never actually delivered to Garmin (it
predates this fix) — push a fresh weigh-in now that auth is healthy, then
do steps 2–4 for real.

**What's blocked on this:** B5 (dashboard exposure of composition data) and
B6 (final docs pass). Both are otherwise ready to implement the moment this
file has real answers in it.

**What's already merged and live-deployable:** Track A (bearer auth, #3–#5),
B1–B4 (migration, DTO/validation, Garmin mapping, atomic dedup, #6–#9) — see
`main`. The production images (`bearyj/vitalforge-weight:latest`,
`bearyj/vitalforge-dashboard:latest`) rebuild automatically on every merge to
`main`, so they already contain all of this — the only step needed before
the checkpoint is pulling and restarting the containers on the production
host.

---

## 0. Deploy the latest images

On the production host (`knowledge`, `192.168.1.21`, `~/docker/vitalforge/`):

```bash
cd ~/docker/vitalforge
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
curl http://localhost:8085/health   # {"status":"ok","service":"vitalforge-weight"}
curl http://localhost:8086/health   # {"status":"ok","service":"vitalforge-dashboard"}
```

I can run this step for you if you'd rather not — it's a standard, idempotent
`pull && up -d`, already the documented update procedure in the README. Say
the word. Steps 1–3 below need you regardless (a real scale, your own Garmin
account, and eyes on Garmin Connect).

## 1. Push one real weigh-in with composition

Via the PWA (`http://<host>:8085` or your configured domain), or a direct
`curl`/Tasker POST if you'd rather not touch a UI, log a real weigh-in that
includes at least one composition field your scale reports — ideally all
four (`body_fat_pct`, `body_water_pct`, `muscle_pct`, `bone_mass_kg`).

```bash
curl -X POST http://localhost:8085/api/weight \
  -H "Content-Type: application/json" \
  -d '{"weight": <your reading>, "unit": "lbs", "body_fat_pct": <x>, "body_water_pct": <x>, "muscle_pct": <x>, "bone_mass_kg": <x>, "source": "pwa"}'
```

Confirm the response has `"success": true` and `"synced_to_garmin": true`. If
`synced_to_garmin` is `false`, check `garmin_error` in the response and
`docker compose logs vitalforge-weight` before continuing — the rest of this
checkpoint needs the push to have actually reached Garmin.

## 2. Confirm it's visible in Garmin Connect

Open Garmin Connect (app or web) → today's weigh-in. Confirm the weight
matches, and that body composition shows up (which fields it displays
depends on Garmin's own UI, not on anything this repo controls).

**Record here:** ______________________________________________

## 3. Read back the raw response and record the observed units — blocks B5

This is the one answer B5 is actually waiting on. `sync.py`'s
`get_weight_range()` → `get_weigh_ins()` is what the dashboard will read;
after step 1's push, it should no longer be `null`.

```bash
docker exec vitalforge-vitalforge-dashboard-1 python3 -c "
from datetime import datetime, timedelta, timezone
from shared import garmin_client
today = datetime.now(timezone.utc).date()
data = garmin_client.get_weight_range((today - timedelta(days=1)).isoformat(), today.isoformat())
latest = data['dailyWeightSummaries'][-1]['latestWeight']
print('boneMass:', latest.get('boneMass'), '  muscleMass:', latest.get('muscleMass'))
print('bodyFat:', latest.get('bodyFat'), '  bodyWater:', latest.get('bodyWater'))
"
```

The working hypothesis from design (§3.5) is **grams**, by precedent from
`weight_history.weight_grams` — e.g. a real bone mass of ~3.2 kg would read
back as `boneMass: 3200`, not `3.2`. But don't assume it — that's exactly
what this step exists to settle.

**Record here (values only, no need to paste full JSON):**
- `boneMass` observed value + inferred unit (kg or g): ______________________
- `muscleMass` observed value + inferred unit (kg or g): ______________________
- `bodyFat` / `bodyWater` sanity check (should already have worked pre-Track-B, confirms nothing regressed): ______________________

**This determines B5's column names** (`bone_mass_kg`/`muscle_mass_kg` vs.
`bone_mass_g`/`muscle_mass_g` on `weight_history` — see
`docs/prp/00-design.md` §3.5/§4.3 and `01-plan.md` §B5).

## 4. (Optional but valuable) Does Garmin collapse two same-timestamp uploads?

This settles the open question in §3.7's enrichment-push design and B4's
fallback cost (`01-plan.md` §B4). Only worth doing if you can arrange it
without polluting your real weigh-in history too much — skip if it's not
worth the hassle, B5 doesn't depend on this one.

1. POST weight-only for a *new* reading (don't reuse today's — this'll
   create a second Garmin record if it doesn't collapse).
2. Within 60 seconds, POST the same weight again with composition fields.
   The second POST should come back `"deduplicated": true, "enriched": true`
   from *our* API (that part's already tested — B4 handles our side
   correctly regardless of what Garmin does).
3. Check Garmin Connect: one weigh-in entry for that reading, or two?

**Record here:** ______________________________________________

If Garmin keeps both records, the fallback in `01-plan.md` §B4 is "enrich
locally, do not re-push" — not free (see that section's cost note before
deciding to switch to it).

---

## 2026-08-22 attempt

Deployed latest images to `knowledge` (step 0 — `docker compose pull && up
-d`, both services healthy, migration confirmed applied to `weight_log`).

Pushed a real weigh-in via `curl` (session-cookie auth, since
`VITALFORGE_API_TOKEN` isn't set in production — only `VITALFORGE_PASS` is
configured, so the Bascule bearer-token path is untested by this):

```
weight: 200.4 lbs, body_fat_pct: 19.9, body_water_pct: 54.4,
muscle_pct: 40.7, bone_mass_kg: 3.72 (converted from 8.2 lbs)
```

Response: `"success": true"`, all fields echoed back correctly,
**`"synced_to_garmin": false"`, `"garmin_error": "'Garmin' object has no
attribute 'garth'"`.

**Root cause (from container logs, present since container startup at
19:10:51 — not caused by this push):** Garmin Connect is rate-limiting
login (`429 GarminConnectTooManyRequestsError: IP rate limited by Garmin`)
from this host's IP. The resume-from-saved-token path
(`shared/garmin_client.py:authenticate()`) is also failing — silently,
since it's wrapped in a bare `except Exception: pass` with no logging — so
every request falls through to a fresh login, which then hits the 429.
Saved `oauth1_token.json`/`oauth2_token.json` exist (~18:15 today) but
whatever's wrong with resuming them can't be diagnosed without adding
logging to that except block.

**Local write is a legitimate proof point on its own:** the composition
DTO validation, mapping, and persistence (B2/B3's actual scope) worked
correctly end-to-end — the failure is entirely in the pre-existing Garmin
auth layer, not in anything Track A/B added.

**Decision: wait for the rate limit to clear, then retry.** Stopped making
further Garmin calls to avoid extending the lockout. Next attempt should
be a single push, not a retry loop.

**Open question for whoever retries:** what to do with this local-only row
(has real composition data, `synced_to_garmin: false`, never reached
Garmin) — leave it, or delete via `DELETE /api/weight/{id}` and re-push
once auth is restored, to avoid an orphaned local-only entry alongside the
eventually-synced one.

## 2026-08-22 incident: root cause and fix

The `garmin_error: "'Garmin' object has no attribute 'garth'"` from the
attempt above turned out not to be Garmin-side rate limiting as the primary
cause — that was a downstream symptom. Root cause, confirmed by reading the
actually-installed `garminconnect` package source (not just its changelog):

**What changed:** Commit `49fa674` (this session, part of B3) bumped
`garminconnect` from `>=0.2.38` to `==0.3.11` for `add_body_composition`
support. Between those versions, upstream (`cyberjunky/python-garminconnect`)
rewrote the client to drop its dependency on the external `garth` OAuth
library entirely — a built-in 5-strategy login chain
(mobile+cffi/mobile+requests/widget+cffi/portal+cffi/portal+requests)
and a new native token format (single `garmin_tokens.json` holding
`di_token`/`di_refresh_token`/`di_client_id`) replaced it. Zero references
to `garth` remain anywhere in 0.3.11's source.

**Why it broke silently:** `shared/garmin_client.py::authenticate()` still
assumed the old API:
- Resume: `client.login(tokenstore=token_path)` — the old garth-era pattern
  expected two files (`oauth1_token.json`/`oauth2_token.json`); 0.3.11
  expects one (`garmin_tokens.json`). That file never existed, so resume
  raised `FileNotFoundError` -> `GarminConnectConnectionError` on every call.
- Persistence: `.garth.dump(token_path)` — `.garth` doesn't exist on 0.3.11's
  `Garmin` at all; this is exactly the `AttributeError` in the logs.
- Both were swallowed by a bare `except Exception: pass`, so every request
  silently fell through to a **full fresh username/password login** instead
  of resuming a session — something the old garth-based flow essentially
  never needed after initial setup. Garmin's 429 was its own defense against
  that repeated real-credential-login traffic; we'd never triggered it
  before because resume used to actually work.
- Side effect: this is the same shared module both services import, so the
  dashboard's scheduled sync (sleep/HRV/RHR/stress/steps/etc., every 2h) was
  very likely broken the same way, not just this weight-composition path.

**Fix:** `Garmin(email=, password=).login(tokenstore=path)` in 0.3.11
already does resume-with-credential-fallback *and* persists tokens
internally in one call — the old two-block "try resume, except: fresh
login, then `.garth.dump()`" structure was both broken and unnecessary.
`authenticate()` is now a single straight-line call to that API. See the
function's own comment for the same summary, and
`tests/test_garmin_client_api.py` — a new regression test that imports the
*real* `garminconnect.Garmin` class (no network I/O; every other test in
this suite fakes Garmin entirely, which is why nothing caught this) and
asserts the exact call shape `authenticate()` depends on. Both
`requirements.txt` pins now carry a comment pointing back here for the next
version bump.

**2026-08-22 22:32 UTC deploy confirmation:** deployed to `knowledge`
(commits through `1273a27` — the auth fix plus an unrelated dedup-window fix
found investigating a separate flaky test, see `git log`). Both services'
lifespan `authenticate()` succeeded on restart:

```
Authenticating with Garmin Connect...
mobile+cffi returned 429: IP rate limited by Garmin
mobile+requests returned 429: IP rate limited by Garmin
Garmin authenticated; tokens persisted to /app/data/.garth
```

The 429 on the first two login strategies confirms Garmin's rate limit
hadn't fully cleared, but 0.3.11's 5-strategy cascade fell through to a
later one and succeeded — proving the fix works even under partial
rate-limiting, not just once Garmin fully clears. `garmin_tokens.json` now
exists (`0600`, matches the library's own hardening). Deleted the orphaned
`oauth1_token.json`/`oauth2_token.json` from `/app/data/.garth/` now that
they're confirmed unused.

**Still not done:** step 1's original composition push (200.4 lbs / 19.9% /
54.4% / 40.7% / 3.72kg) predates this fix and was never actually delivered
to Garmin — a fresh push is needed now that auth is healthy, then steps 2–4
for real (Garmin Connect visual check + the units read-back that unblocks
B5).

## Next steps once this file has real answers

1. Tell me (or whoever picks this up) it's done — I'll read this file and
   proceed with B5 using the confirmed units from step 3.
2. If Garmin's units turn out to be something other than the grams
   hypothesis, that's fine — it just changes a column suffix, not the
   design (`01-plan.md` §B5 already names both branches).
3. B6 (docs) follows once B5 merges, then Track B as a whole is done and
   Phase 4 (adversarial review of the merged whole + a second-model pass)
   is next per `docs/prp/vitalforge-agent-prompt.md`.
