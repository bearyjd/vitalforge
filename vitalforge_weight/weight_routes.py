"""POST /p/{slug}/api/weight and the weight read/delete routes.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.
from shared.auth import (
    require_person,
)
from shared.database import (
    get_db,
)
from shared.garmin_client import (
    authenticate,
    push_weight,
)
from vitalforge_weight.garmin_claim import _garmin_claim_is_live
from vitalforge_weight.models import GRAMS_PER_KG, LBS_PER_KG, WeightIn

logger = logging.getLogger(__name__)


DEDUP_WEIGHT_TOLERANCE_GRAMS = 50


DEDUP_WINDOW_SECONDS = 60


COMPOSITION_FIELDS = ("body_fat_pct", "body_water_pct", "muscle_pct", "bone_mass_kg", "bmr", "amr")


# `source` and `bmi`. Kept separate from COMPOSITION_FIELDS (which also names
# exactly what _push_composition forwards to Garmin) so that boundary stays
# explicit; `source` has no Garmin analog and was previously excluded from
# enrichment entirely, so a row's provenance label could permanently
# misattribute composition data actually added by a different, later
# client (Phase 4 adversarial review finding). `bmi` joined it for a
# different reason (codex/devil's-advocate review on the bmi/bmr/amr PR):
# 00-design.md SS3.4 already rejected sending bmi to Garmin -- Garmin derives
# its own from weight + the profile's height, and vitalforge_dashboard/sync.py
# reads that Garmin-computed value back into weight_history.bmi on every
# scheduled sync. Forwarding a second, independently-computed bmi risks
# overwriting Garmin's own on the next push, which then round-trips back into
# a table this repo already treats as Garmin-sourced data. bmi is still
# stored in weight_log and echoed in the response -- only the Garmin push
# (and the composition_changed/re-sync trigger a bmi-only edit would
# otherwise cause) is withheld. bmr/amr have no such round-trip target
# (weight_history has no bmr/amr columns) and stay in COMPOSITION_FIELDS.
ENRICHABLE_FIELDS = (*COMPOSITION_FIELDS, "bmi", "source")


# The column list both existing-row SELECTs in post_weight need (the client_id
# primary match and the timestamp+weight window fallback). Deduplicated after
# a devil's-advocate review: two independently-maintained copies meant a
# future ENRICHABLE_FIELDS addition updated in only one place would raise
# IndexError/KeyError on `existing[field]` for whichever request path hit the
# stale SELECT -- an uncaught exception mid-transaction, not a graceful
# degraded response.
_WEIGHT_LOG_EXISTING_ROW_COLUMNS = (
    "id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
    "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, bmi, bmr, amr, "
    "source, client_id, garmin_claimed_at"
)


def _push_composition(weight_grams: int, timestamp: datetime, composition: dict) -> str | None:
    """Push weight + composition to Garmin; returns an error string, or None
    on success. Never raises -- callers decide what to do with the row."""
    try:
        authenticate()
        muscle_pct = composition.get("muscle_pct")
        muscle_mass_kg = (weight_grams / 1000.0) * muscle_pct / 100 if muscle_pct is not None else None
        push_weight(
            weight_grams,
            timestamp,
            percent_fat=composition.get("body_fat_pct"),
            percent_hydration=composition.get("body_water_pct"),
            muscle_mass_kg=muscle_mass_kg,
            bone_mass_kg=composition.get("bone_mass_kg"),
            # bmi intentionally NOT forwarded -- see the ENRICHABLE_FIELDS
            # comment above. Still stored in weight_log and echoed in the
            # response; only the Garmin push is withheld.
            basal_met=composition.get("bmr"),
            active_met=composition.get("amr"),
        )
        return None
    except Exception as e:
        logger.error("Failed to push weight to Garmin: %s", e)
        return str(e)


def add_weight_routes(app):
    """Register these routes on a FastAPI app.

    Mirrors shared/persons_admin.py's add_person_routes: the decorators need an
    `app` object, and taking it as a parameter is how this repo already registers
    routes defined outside app.py."""

    @app.post("/p/{slug}/api/weight")
    async def post_weight(data: WeightIn, person_id: int = Depends(require_person("manage"))):
        unit = data.unit.lower()
        if unit not in ("lbs", "kg"):
            raise HTTPException(status_code=400, detail="unit must be 'lbs' or 'kg'")

        if unit == "lbs":
            weight_lbs = data.weight
            weight_kg = data.weight / LBS_PER_KG
        else:
            weight_kg = data.weight
            weight_lbs = data.weight * LBS_PER_KG

        weight_grams = round(weight_kg * GRAMS_PER_KG)
        now = datetime.now(timezone.utc)
        # The moment this row's dedup window and stored timestamp anchor on.
        # `captured_at`, when the client sends one, is the true weigh-in moment --
        # not this request's receipt time, which for a delayed replay (A6) can be
        # months later and would otherwise miss every window check entirely.
        # Absent `captured_at` (pwa/tasker/legacy bascule), this is exactly `now`,
        # i.e. today's behavior, unchanged.
        #
        # .astimezone(timezone.utc) is required, not cosmetic: WeightIn only
        # requires captured_at to carry SOME offset, not specifically +00:00 (a
        # client-local "-05:00" is valid input). Every existing row's `timestamp`
        # TEXT column was written as `now.isoformat()`, always +00:00. The
        # sargable prefilter (`timestamp >= ?`, below) is a plain TEXT comparison
        # -- it does not parse offsets -- so a captured_at retained in its
        # original offset can string-compare *before* a same-instant +00:00 row,
        # silently dropping that row from the SELECT before the authoritative
        # julianday() bounds even see it, and inserting a duplicate. The same raw
        # value also reaches Garmin via push_weight's strftime, which has no `%z`
        # and would silently drop a non-UTC offset rather than convert it,
        # corrupting the pushed timestamp by the offset amount. Normalizing once,
        # here, keeps every downstream use (storage, the prefilter, julianday
        # comparisons, and the Garmin push) consistently UTC (codex review P1).
        dedup_anchor = (data.captured_at if data.captured_at is not None else now).astimezone(timezone.utc)
        timestamp = dedup_anchor.isoformat()

        # Atomic: read for a duplicate and (if any) write inside one transaction,
        # so two concurrent requests can never both observe "no duplicate". The
        # Garmin push happens after COMMIT, outside the lock -- see
        # docs/prp/00-design.md SS3.7 for why (no timeout mechanism exists to
        # bound the call otherwise, and it is synchronous).
        db = await get_db()
        try:
            await db.execute("BEGIN IMMEDIATE")

            # TODO(follow-up): existing-row resolution (client_id lookup, then
            # the timestamp+weight window fallback, then the conflict/enrichment
            # merge) is now three sequential decision points before the actual
            # insert/update, on top of the transaction and Garmin-push logic this
            # function already carried. Worth extracting to its own function once
            # this stabilizes in production -- deferred here to keep this PR
            # reviewable as "the A6 fix," not a structural refactor riding along
            # with it (Devil's-advocate review, Round 5).

            # Primary match: an exact client_id hit is a known-identical reading
            # regardless of how far its timestamp is from this request's receipt
            # time -- the whole point of A6. Only when this misses (no client_id
            # sent, or a client_id VitalForge has never seen) does the
            # timestamp+weight window below even run.
            existing = None
            matched_by_client_id = False
            if data.client_id is not None:
                cursor = await db.execute(
                    f"SELECT {_WEIGHT_LOG_EXISTING_ROW_COLUMNS} "
                    "FROM weight_log WHERE person_id = ? AND client_id = ?",
                    (person_id, data.client_id),
                )
                existing = await cursor.fetchone()
                matched_by_client_id = existing is not None

            # `timestamp >= ?` is a sargable prefilter, not the authoritative
            # bound -- plain string comparison is NOT reliably safe here despite
            # every row coming from this same route's own
            # `datetime.now(timezone.utc).isoformat()`: isoformat() omits the
            # fractional part entirely when microseconds are exactly 0
            # ("...11+00:00" vs "...11.482913+00:00"), and '.' (0x2e) sorts
            # after '+' (0x2b), so a zero-microsecond row can sort BEFORE a
            # same-second fractional one -- the format is neither fixed-width
            # nor zero-padded (Phase 4 devil's-advocate review finding,
            # verified: `sorted(["...11+00:00", "...11.482913+00:00"])` puts
            # the fractional one first). This prefilter is still correct only
            # because it's 1s wider than the authoritative window
            # (DEDUP_WINDOW_SECONDS + 1, below) -- that one second of slack
            # absorbs the entire sub-second ordering error, so this clause can
            # never exclude a row the authoritative ABS(julianday()) clause
            # below would accept. Do not narrow that `+ 1` on the strength of
            # this comment's format claim -- it's the slack, not the format,
            # that makes the prefilter safe. That clause is what
            # idx_weight_log_timestamp cannot use directly (wrapping the column
            # in julianday() makes the index unusable for range pruning).
            #
            # The authoritative window is symmetric (+-60s around this request's
            # own `now`), not one-sided ending exactly at `now` -- an earlier
            # version bounded it as [now-60s, now], which (2026-08-22) turned out
            # to silently defeat dedup for genuinely-concurrent requests: two
            # requests each capture their own `now` microseconds apart, and
            # whichever captured the earlier `now` would run a query whose upper
            # bound excluded the other's already-committed row, since that row's
            # timestamp was technically "after" its own `now` snapshot -- both
            # would then see no duplicate and both would insert (see
            # tests/test_dedup_concurrency.py's repro in the same commit). A
            # symmetric window fixes that while still rejecting the case it was
            # originally added for -- a wildly clock-skewed poison row (e.g.
            # minutes or years off) is still far outside +-60s either direction.
            #
            # Bounds use julianday's own `'-60 seconds'`/`'+60 seconds'` modifier
            # arithmetic against `now`, not `ABS(julianday(a) - julianday(b))`:
            # subtracting two independently-rounded Julian day floats (each
            # ~2.46M with ~15-17 significant digits of double precision) loses
            # enough precision that two timestamps exactly 60.000000s apart can
            # compute a difference a few dozen microseconds *above* 60s roughly
            # 9% of the time (confirmed empirically, 200k trials) -- silently
            # excluding a legitimate boundary duplicate from dedup. Comparing
            # against SQLite's own offset computation instead avoids the
            # subtraction/cancellation entirely (0/200k failures). Caught by
            # Codex review the same day the symmetric-window fix landed.
            # Anchored on dedup_anchor (== captured_at when the client sent one),
            # not `now` -- see the comment on dedup_anchor's assignment above.
            # Legacy rows this same replay could match were themselves stamped
            # near their own true capture time (a live v1 delivery's receipt time
            # is a close proxy for it), so anchoring the *new* request's window on
            # its own true capture time is what lets the two line up months apart
            # in wall-clock receipt time. A row whose original delivery was itself
            # delayed by more than the window is a known residual gap (its stored
            # timestamp isn't a close proxy for capture time either) -- undetectable
            # without a capture time VitalForge never had the chance to record.
            sargable_cutoff = (dedup_anchor - timedelta(seconds=DEDUP_WINDOW_SECONDS + 1)).isoformat()
            if existing is None:
                cursor = await db.execute(
                    f"SELECT {_WEIGHT_LOG_EXISTING_ROW_COLUMNS} "
                    "FROM weight_log "
                    "WHERE person_id = ? "
                    "AND timestamp >= ? "
                    "AND ABS(weight_grams - ?) <= ? "
                    "AND julianday(timestamp) >= julianday(?, ?) "
                    "AND julianday(timestamp) <= julianday(?, ?) "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (
                        person_id,
                        sargable_cutoff,
                        weight_grams,
                        DEDUP_WEIGHT_TOLERANCE_GRAMS,
                        timestamp,
                        f"-{DEDUP_WINDOW_SECONDS} seconds",
                        timestamp,
                        f"+{DEDUP_WINDOW_SECONDS} seconds",
                    ),
                )
                existing = await cursor.fetchone()

            updates = {}
            conflicts = []
            if existing is not None:
                for field in ENRICHABLE_FIELDS:
                    incoming_value = getattr(data, field)
                    if incoming_value is None:
                        continue
                    existing_value = existing[field]
                    if existing_value is None:
                        updates[field] = incoming_value
                    elif existing_value != incoming_value:
                        conflicts.append(field)

                # client_id is deliberately not in ENRICHABLE_FIELDS: a mismatch
                # here means two DISTINCT client-side readings collided inside the
                # dedup window, not a value to merge -- silently overwriting one
                # row's identity with the other's client_id would make a later
                # replay of the *original* reading match the wrong row.
                if data.client_id is not None:
                    if existing["client_id"] is None:
                        updates["client_id"] = data.client_id
                    elif existing["client_id"] != data.client_id:
                        conflicts.append("client_id")

                # A client_id match asserts "this is the same reading" -- unlike
                # the window path (bounded to +-50g of measurement noise by its
                # own SQL, so within-tolerance differences there are deliberately
                # silent), the client_id fast path has no weight check at all
                # otherwise. A colliding client_id (client-side bug, id reuse)
                # with a wildly different weight would silently keep the stale
                # stored value with zero signal to anyone. Scoped to the
                # client_id match specifically -- applying this to a window match
                # too would flag every legitimate within-tolerance difference the
                # window's own +-50g exists to accept silently (test_dedup_tolerance_49g_collapses).
                # weight is never overwritten after the fact (same first-write-
                # wins convention as every enrichable field) -- this only makes
                # the disagreement visible.
                if matched_by_client_id and existing["weight_grams"] != weight_grams:
                    conflicts.append("weight")

            if existing is None:
                cursor = await db.execute(
                    "INSERT INTO weight_log (person_id, weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin, "
                    "body_fat_pct, body_water_pct, muscle_pct, bone_mass_kg, bmi, bmr, amr, source, client_id) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        person_id,
                        round(weight_lbs, 2),
                        round(weight_kg, 2),
                        weight_grams,
                        timestamp,
                        data.body_fat_pct,
                        data.body_water_pct,
                        data.muscle_pct,
                        data.bone_mass_kg,
                        data.bmi,
                        data.bmr,
                        data.amr,
                        data.source,
                        data.client_id,
                    ),
                )
                row_id = cursor.lastrowid
            elif updates:
                set_clause = ", ".join(f"{field} = ?" for field in updates)
                await db.execute(
                    f"UPDATE weight_log SET {set_clause} WHERE id = ?",
                    (*updates.values(), existing["id"]),
                )
                row_id = existing["id"]
            else:
                row_id = existing["id"]

            if conflicts:
                logger.warning("Weight POST conflicts with stored row %s on fields: %s", row_id, conflicts)

            # `updates` can now be source-only (ENRICHABLE_FIELDS includes
            # `source`, which has no Garmin analog) -- only an actual
            # composition change should trigger a re-push or touch
            # synced_to_garmin. Gating those two on plain `updates` re-pushed
            # unchanged composition data on every source-only enrich and could
            # flip a previously-true synced_to_garmin to a permanently stale
            # false if that incidental push ever failed (fix-review finding on
            # the ENRICHABLE_FIELDS change above).
            composition_changed = any(field in COMPOSITION_FIELDS for field in updates)

            # The second clause closes a real gap a devil's-advocate review
            # found: without it, a client_id-matched retry of a reading whose
            # Garmin push had failed could never succeed -- nothing in
            # ENRICHABLE_FIELDS changes on an exact resubmit (existing values
            # match, so `updates` stays empty), so composition_changed alone
            # would stay False forever, and no other branch here ever
            # re-attempts the push. Before A6, a sufficiently delayed retry
            # missed the 60s window and landed as a brand-new row instead,
            # getting a fresh push attempt by accident -- client_id closes that
            # window on purpose, so it needs to inherit the retry, not just the
            # identity match. Scoped to client_id specifically (not the
            # window-match path): a client_id resubmission is an explicit "this
            # is the same reading," a stronger signal than an incidental window
            # match, so retrying the push here and not there is deliberate, not
            # an oversight. Named once and reused below (the synced_to_garmin
            # persistence gate) rather than repeating the condition -- the two
            # must never drift apart, or a retried push's outcome would compute
            # correctly but never actually get saved.
            should_attempt_garmin_push = composition_changed or (
                matched_by_client_id and existing is not None and not existing["synced_to_garmin"]
            )

            # THE PUSH DECISION IS TAKEN AND CLAIMED INSIDE THIS TRANSACTION, and
            # deliberately not after the commit below.
            #
            # It used to be computed after. The comment that stood here argued the
            # arrangement was safe because push_weight is synchronous and blocks
            # the event loop for its whole duration, so no second request could
            # interleave -- and noted that a reproduction had been attempted and
            # had failed. That argument is wrong. The gap is not DURING the push,
            # it is AFTER it: the winner writes synced_to_garmin through an
            # `await db.execute(...)` / `await db.commit()` pair, and both of those
            # yield. A second identical retry resumes in that window, opens its own
            # transaction, reads synced_to_garmin still 0, judges the retry live,
            # and pushes a second time. One uvicorn worker, a synchronous push,
            # nothing exotic -- the reproduction just needs the scheduler to land
            # in a narrow window. Measured at 5 failures in 50 runs of
            # test_two_concurrent_identical_retries_push_to_garmin_once, which is
            # the test that turned CI red on this branch.
            #
            # The claim is the mechanism strength_sessions uses for the same
            # problem: taken inside the SAME BEGIN IMMEDIATE that reads or writes
            # the row, so a blocked request sees it the instant it can see the row
            # at all. What serializes the decision is the claim, not the status.
            #
            # This matters more than an ordinary duplicate row: there is no delete
            # path here, so a duplicate weigh-in on Garmin is permanent and has to
            # be removed by hand in Garmin Connect.
            will_push_to_garmin = existing is None or should_attempt_garmin_push
            if will_push_to_garmin:
                if _garmin_claim_is_live(
                    existing["garmin_claimed_at"] if existing is not None else None, now
                ):
                    # Another request is mid-push for this row. Stand down rather
                    # than duplicate it, and report the row as it stands -- the
                    # push is in flight and its outcome is not ours to report. A
                    # client that retries once the winner lands sees
                    # synced_to_garmin true and stops retrying.
                    will_push_to_garmin = False
                    logger.info(
                        "Row %s is already claimed for a Garmin push; skipping the duplicate push",
                        row_id,
                    )
                else:
                    await db.execute(
                        "UPDATE weight_log SET garmin_claimed_at = ? WHERE id = ?",
                        (now.isoformat(), row_id),
                    )

            await db.commit()

            # Push happens outside the transaction -- it is synchronous and must
            # not be held across the write lock (test_concurrent_writer_not_blocked
            # _by_garmin_push pins that); the claim above is what makes doing so
            # safe. This connection stays open only to record the outcome
            # afterward. By this point the row (and any enrichment) is already
            # durably committed, so a failure here must never surface as a 500 over
            # already-successful data -- it would tell the client the whole request
            # failed when it didn't. _push_composition itself never raises; this
            # guards the timestamp parse and the flag-update statement around it.

            garmin_error = None
            synced = False
            try:
                if will_push_to_garmin and existing is None:
                    # dedup_anchor here can be an arbitrarily old captured_at
                    # (a replay of a months-old weigh-in) -- this is the first
                    # code path in this file able to push a backdated timestamp
                    # to Garmin at all; every prior push used either `now` or a
                    # pre-existing row's own timestamp, which the window's +-60s
                    # bound kept close to receipt time. shared/garmin_client.py's
                    # push_weight/add_body_composition does no bound-checking of
                    # its own (verified by reading it), so this is UNVERIFIED
                    # against real Garmin Connect behavior for a large backdate --
                    # confirm with a live account before this path sees real
                    # replay traffic (Devil's-advocate review, Round 2).
                    garmin_error = _push_composition(
                        weight_grams,
                        dedup_anchor,
                        {
                            "body_fat_pct": data.body_fat_pct,
                            "body_water_pct": data.body_water_pct,
                            "muscle_pct": data.muscle_pct,
                            "bone_mass_kg": data.bone_mass_kg,
                            "bmr": data.bmr,
                            "amr": data.amr,
                            # bmi deliberately omitted -- _push_composition never
                            # forwards it, see the ENRICHABLE_FIELDS comment above.
                        },
                    )
                    synced = garmin_error is None
                elif will_push_to_garmin:
                    merged = {field: updates.get(field, existing[field]) for field in COMPOSITION_FIELDS}
                    # Parsed locally, not left to the outer except below: a row
                    # whose timestamp SQLite's own julianday() accepted (so the
                    # dedup match above fired) but Python's fromisoformat()
                    # can't parse used to raise here and skip the
                    # synced_to_garmin flag-update entirely -- the response
                    # correctly said `false`, but the stored row kept whatever
                    # stale value it already had (Phase 4 adversarial review
                    # finding).
                    try:
                        original_ts = datetime.fromisoformat(existing["timestamp"])
                    except ValueError as e:
                        garmin_error = f"could not parse stored timestamp for Garmin push: {e}"
                    else:
                        garmin_error = _push_composition(existing["weight_grams"], original_ts, merged)
                        synced = garmin_error is None
                else:
                    # Nothing needed pushing, or another request holds the claim.
                    # `existing` is None only on the insert path, which always
                    # pushes -- a row this request just created inside its own
                    # transaction cannot already be claimed by anyone else -- so
                    # this branch always has a row. Guarded anyway rather than
                    # subscripting None if that ever stops being true.
                    synced = bool(existing["synced_to_garmin"]) if existing is not None else False

                if will_push_to_garmin:
                    # Releasing the claim is part of recording the outcome, and
                    # happens whether the push succeeded or failed: a failed push
                    # must be retryable immediately, not only once the claim ages
                    # out. If this statement itself fails, the claim is left behind
                    # and the row stays unpushable until it goes stale
                    # (_GARMIN_CLAIM_TIMEOUT_SECONDS) -- the same trade
                    # strength_sessions makes, and better than a permanent
                    # duplicate on Garmin.
                    await db.execute(
                        "UPDATE weight_log SET synced_to_garmin = ?, garmin_claimed_at = NULL WHERE id = ?",
                        (int(synced), row_id),
                    )
                    await db.commit()
            except Exception as e:
                logger.error("Post-commit sync-flag update failed for row %s: %s", row_id, e)
                if garmin_error is None:
                    garmin_error = f"sync status update failed: {e}"
                synced = False
        finally:
            await db.close()

        if existing is None:
            result = {
                "success": True,
                "weight_lbs": round(weight_lbs, 2),
                "weight_kg": round(weight_kg, 2),
                "timestamp": timestamp,
                "synced_to_garmin": synced,
            }
            for field_name in ENRICHABLE_FIELDS:
                value = getattr(data, field_name)
                if value is not None:
                    result[field_name] = value
            if data.client_id is not None:
                result["client_id"] = data.client_id
        else:
            result = {
                "success": True,
                "deduplicated": True,
                "id": row_id,
                "weight_lbs": existing["weight_lbs"],
                "weight_kg": existing["weight_kg"],
                "timestamp": existing["timestamp"],
                "synced_to_garmin": synced,
            }
            if updates:
                result["enriched"] = True
            if conflicts:
                result["conflict"] = True
                result["conflict_fields"] = conflicts
            # Echo the row's client_id on a dedup response too, not just a fresh
            # insert -- otherwise the one response a *retrying* client actually
            # receives never confirms the identity key it matched on, and a
            # window-match client_id conflict never states which client_id
            # actually won (devil's-advocate review). `updates.get(...)` rather
            # than `existing["client_id"]` alone: `existing` is the PRE-update
            # row, so a client_id that was just backfilled this request (existing
            # was NULL) needs the post-update value, not the stale None.
            final_client_id = updates.get("client_id", existing["client_id"])
            if final_client_id is not None:
                result["client_id"] = final_client_id
        if garmin_error:
            result["garmin_error"] = garmin_error

        return result

    @app.get("/p/{slug}/api/weight/recent")
    async def get_recent_weights(person_id: int = Depends(require_person("view"))):
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id, weight_lbs, weight_kg, timestamp, synced_to_garmin FROM weight_log "
                "WHERE person_id = ? ORDER BY timestamp DESC LIMIT 10",
                (person_id,),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        return [
            {
                "id": row["id"],
                "weight_lbs": row["weight_lbs"],
                "weight_kg": row["weight_kg"],
                "timestamp": row["timestamp"],
                "synced_to_garmin": bool(row["synced_to_garmin"]),
            }
            for row in rows
        ]

    @app.get("/p/{slug}/api/weight/trend")
    async def get_weight_trend(person_id: int = Depends(require_person("view"))):
        """Return last 30 days of weights for the trend chart."""
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT weight_lbs, weight_kg, timestamp FROM weight_log "
                "WHERE person_id = ? AND timestamp >= datetime('now', '-30 days') ORDER BY timestamp ASC",
                (person_id,),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        return [
            {"weight_lbs": row["weight_lbs"], "weight_kg": row["weight_kg"], "timestamp": row["timestamp"]}
            for row in rows
        ]

    @app.delete("/p/{slug}/api/weight/{weight_id}")
    async def delete_weight(weight_id: int, person_id: int = Depends(require_person("manage"))):
        db = await get_db()
        try:
            # The dependency authorizes the caller for this person; `person_id` in
            # the predicate is still what stops an id belonging to a *different*
            # person being deleted through a slug the caller does hold.
            cursor = await db.execute(
                "DELETE FROM weight_log WHERE id = ? AND person_id = ?", (weight_id, person_id)
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Weight entry not found")
        finally:
            await db.close()

        return {"success": True, "deleted_id": weight_id}
