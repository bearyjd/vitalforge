# 03 — Live validation: the B3 checkpoint

**Status: not yet run.** This is the one step in the whole Track A/B effort
that can't be faked or automated — it needs a real scale reading pushed
through the real code to a real Garmin account. Everything below is
prepared; nothing has been executed yet.

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

## Next steps once this file has real answers

1. Tell me (or whoever picks this up) it's done — I'll read this file and
   proceed with B5 using the confirmed units from step 3.
2. If Garmin's units turn out to be something other than the grams
   hypothesis, that's fine — it just changes a column suffix, not the
   design (`01-plan.md` §B5 already names both branches).
3. B6 (docs) follows once B5 merges, then Track B as a whole is done and
   Phase 4 (adversarial review of the merged whole + a second-model pass)
   is next per `docs/prp/vitalforge-agent-prompt.md`.
