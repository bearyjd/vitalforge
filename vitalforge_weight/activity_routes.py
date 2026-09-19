"""POST /p/{slug}/api/activity and the two strength-session read routes.
"""

import json
import logging
from dataclasses import replace
from datetime import date, datetime, timezone

from fastapi import Depends, HTTPException, Query, Response

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
from shared.garmin_registry_errors import (
    LEGACY_GARMIN_TARGET_RETIRED_ERROR as _LEGACY_GARMIN_TARGET_RETIRED_ERROR,
)
from vitalforge_weight.activity_garmin import (
    ActivityPushOutcome,
    _activity_name,
    _mark_activity_outcome_unknown,
    _push_activity,
    _read_activity_outcome,
    _reconcile_activity,
    _record_activity_garmin_outcome,
    bounded_garmin_error,
)
from vitalforge_weight.garmin_claim import _garmin_claim_is_live
from vitalforge_weight.models import ActivityIn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strength sessions (Cadence): POST /p/{slug}/api/activity and its two reads
# ---------------------------------------------------------------------------

# Columns every strength_sessions read in this module needs. One list, for the
# same reason _WEIGHT_LOG_EXISTING_ROW_COLUMNS is one list: two copies drift,
# and the drift surfaces as a KeyError mid-transaction rather than a graceful
# response.
_STRENGTH_SESSION_COLUMNS = (
    "id, person_id, session_id, session_label, start_time_utc, duration_seconds, exercises_json, "
    "notes, source, garmin_status, garmin_activity_id, garmin_error, "
    "garmin_sets_status, garmin_claimed_at, garmin_name_prefix, created_at, updated_at"
)


# Fields compared between an incoming repeat POST and the stored row, named in
# the REQUEST's vocabulary (the warning is read by whoever wrote the client,
# not by whoever wrote the schema) alongside the column that holds each.
_ACTIVITY_CONFLICT_FIELDS = (
    ("start", "start_time_utc"),
    ("duration_min", "duration_seconds"),
    ("exercises", "exercises_json"),
    ("notes", "notes"),
    ("source", "source"),
    ("session_label", "session_label"),
)


# Garmin outcomes a repeat POST is allowed to re-attempt. 'skipped' is
# deliberately absent: a session stored with push_to_garmin false never
# becomes pushable by flipping the flag on a later POST, matching
# should_attempt_garmin_push's shape for weight. A client that wants a
# session on Garmin must say so on the FIRST post of that session_id.
_RETRYABLE_GARMIN_STATUSES = ("pending", "failed")


def _normalise_since(since: str) -> str:
    """Turn a client `since` into a value comparable against start_time_utc.

    start_time_utc is TEXT holding a UTC isoformat string, and SQLite compares
    it as a string, so a value that merely LOOKS date-ish does not error -- it
    silently returns the wrong window. Three cases, deliberately distinct:

    - A bare date ("2026-09-06") is used as a PREFIX. "2026-09-06T..." sorts
      at or after "2026-09-06", so `>=` selects that whole day onward, which
      is what a caller filtering by day means.
    - An offset-aware datetime is normalised to UTC isoformat first. A
      client-local "-04:00" compared raw would string-sort against stored
      "+00:00" values by its wall-clock digits, quietly shifting the window by
      the offset -- the same class of bug the write path normalises away.
    - A naive datetime is REJECTED. Guessing whose clock it belongs to is
      exactly what the write path refuses to do for `start`.
    """
    try:
        parsed_date = date.fromisoformat(since)
    except ValueError:
        pass
    else:
        # .isoformat(), not the raw input. date.fromisoformat() also accepts
        # the compact ("20260906") and ISO-week ("2026-W01-1") forms, and both
        # would be handed straight to a LEXICAL comparison against stored
        # "YYYY-MM-DDT..." values. "20260906" sorts ABOVE every such timestamp
        # ('0' > '-'), so `start_time_utc >= ?` would match nothing and the
        # caller would get an empty list for a date they hold sessions on --
        # exactly the silent-wrong-window failure this function exists to stop,
        # reached through a form it was accepting rather than one it rejected.
        # Normalising keeps every valid ISO 8601 date working and makes the
        # prefix comparison sound.
        return parsed_date.isoformat()

    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="since must be an ISO 8601 date (2026-09-06) or an offset-aware datetime",
        ) from None
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail="since must include a UTC offset when given as a datetime",
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _activity_conflicts(existing, incoming: dict) -> list[str]:
    """Fields where a repeat POST disagrees with the stored row.

    Reported, never applied: first-write-wins, the same convention
    ENRICHABLE_FIELDS follows. A differing body is NOT a 409 -- a client
    replaying a session it edited locally should still get its idempotent
    200, with the disagreement made visible rather than silently resolved
    either way.
    """
    return [
        request_name
        for request_name, column in _ACTIVITY_CONFLICT_FIELDS
        if existing[column] != incoming[column]
    ]


def _serialize_strength_session(row) -> dict:
    """One stored row as the read routes return it, with `exercises` parsed
    back out of the JSON blob and duration re-expressed in minutes (the unit
    the write route accepts)."""
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "session_label": row["session_label"],
        "start_time_utc": row["start_time_utc"],
        "duration_min": row["duration_seconds"] // 60,
        "duration_seconds": row["duration_seconds"],
        "exercises": json.loads(row["exercises_json"]),
        "notes": row["notes"],
        "source": row["source"],
        "garmin_status": row["garmin_status"],
        "garmin_activity_id": row["garmin_activity_id"],
        "garmin_error": bounded_garmin_error(row["garmin_error"]),
        "garmin_sets_status": row["garmin_sets_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add_activity_routes(app):
    """Register these routes on a FastAPI app.

    Mirrors shared/persons_admin.py's add_person_routes: the decorators need an
    `app` object, and taking it as a parameter is how this repo already registers
    routes defined outside app.py."""

    @app.post("/p/{slug}/api/activity", status_code=202)
    async def post_activity(
        data: ActivityIn,
        response: Response,
        person_id: int = Depends(require_person("manage")),
    ):
        """Store a completed strength session; optionally file it on Garmin.

        202 on a fresh insert, 200 on an idempotent repeat. The decorator's
        status_code applies to EVERY return, so the dedup branch overrides it
        explicitly -- without that, a retry also answers 202 and the client
        cannot tell "created" from "already had it" by status alone.
        """
        start_utc = data.start.astimezone(timezone.utc)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        exercise_dicts = [exercise.model_dump() for exercise in data.exercises]
        incoming = {
            "start_time_utc": start_utc.isoformat(),
            "duration_seconds": data.duration_min * 60,
            "exercises_json": json.dumps(exercise_dicts),
            "notes": data.notes,
            "source": data.source,
            "session_label": data.session_label,
            # Present so `incoming` and a strength_sessions Row really do share
            # these key names, which the comment on `stored` below promises. Without
            # it, `stored["garmin_name_prefix"]` is a KeyError on the fresh-insert
            # path and only clause ordering hides that -- a footgun for the next
            # edit, not a property.
            "garmin_name_prefix": None,
        }

        # Atomic: the duplicate lookup and the insert happen inside one
        # transaction, so two concurrent requests can never both observe "no
        # duplicate". The Garmin push happens after COMMIT, outside the lock, for
        # the same reason post_weight does it there -- the call is synchronous
        # with no timeout mechanism to bound it.
        db = await get_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    f"SELECT {_STRENGTH_SESSION_COLUMNS} FROM strength_sessions "
                    "WHERE person_id = ? AND session_id = ?",
                    (person_id, data.session_id),
                )
            ).fetchone()

            conflicts: list[str] = []
            if existing is None:
                # The claim is taken in the INSERT itself, not afterwards: a
                # concurrent request blocked on this transaction must see it the
                # instant it can see the row at all.
                cursor = await db.execute(
                    "INSERT INTO strength_sessions (person_id, session_id, session_label, start_time_utc, "
                    "duration_seconds, exercises_json, notes, source, garmin_status, "
                    "garmin_claimed_at, garmin_name_prefix, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        person_id,
                        data.session_id,
                        data.session_label,
                        incoming["start_time_utc"],
                        incoming["duration_seconds"],
                        incoming["exercises_json"],
                        data.notes,
                        data.source,
                        "pending" if data.push_to_garmin else "skipped",
                        now if data.push_to_garmin else None,
                        # One source for this value: the same key the retry path
                        # reads off `stored`, so insert and re-read cannot drift.
                        incoming["garmin_name_prefix"],
                        now,
                        now,
                    ),
                )
                row_id = cursor.lastrowid
                outcome = ActivityPushOutcome(
                    "pending" if data.push_to_garmin else "skipped", None, None, "not_attempted"
                )
                should_push = data.push_to_garmin
                # A row that did not exist a moment ago has no ambiguous earlier
                # attempt to reconcile against.
                should_reconcile = False
            else:
                # First-write-wins: the stored payload is never modified.
                row_id = existing["id"]
                conflicts = _activity_conflicts(existing, incoming)
                outcome = ActivityPushOutcome(
                    existing["garmin_status"],
                    existing["garmin_activity_id"],
                    existing["garmin_error"],
                    existing["garmin_sets_status"],
                )
                # Client-driven retry, the only retry mechanism this codebase has
                # for anything: a repeat POST of the same session_id re-attempts
                # the push iff it has not already succeeded. There is no
                # background worker, and building one here would be new machinery
                # nothing else in VitalForge has.
                #
                # garmin_status alone is NOT sufficient to gate that, which is the
                # whole reason garmin_claimed_at exists. The winning request
                # writes 'synced' only after its Garmin call returns, long after
                # its transaction committed -- so a concurrent second request
                # reads 'pending', judges it retryable, and files a SECOND
                # activity for the same session. Both the read and the claim have
                # to happen inside this one BEGIN IMMEDIATE for the decision to be
                # serialized.
                # Migration 003 terminalizes a row that may have reached the
                # retired global Garmin account. It must never be retried or
                # reconciled via this person's independent link: either action
                # could create a duplicate in a different account.
                is_retired_global_target = outcome.garmin_error == _LEGACY_GARMIN_TARGET_RETIRED_ERROR
                should_push = (
                    data.push_to_garmin
                    and not is_retired_global_target
                    and outcome.garmin_status in _RETRYABLE_GARMIN_STATUSES
                )
                # 'unknown' is NOT in _RETRYABLE_GARMIN_STATUSES and never becomes
                # retryable, however old its claim gets: the push may already have
                # landed, so the only safe next step is to ASK Garmin. That
                # reconciliation still needs the claim, so two concurrent
                # re-POSTs cannot both fall through the lookup and both push.
                should_reconcile = (
                    data.push_to_garmin
                    and not is_retired_global_target
                    and outcome.garmin_status == "unknown"
                )

                if (should_push or should_reconcile) and _garmin_claim_is_live(
                    existing["garmin_claimed_at"], now_dt
                ):
                    # Someone else is mid-push. On a retryable row report
                    # 'pending' rather than the stored status: a push really is in
                    # flight, and echoing a stale 'failed' would invite an
                    # immediate retry into the same race. An 'unknown' row keeps
                    # saying 'unknown', because that is still true.
                    logger.info(
                        "Session %s already has a live Garmin push claim (%s); not pushing again",
                        data.session_id, existing["garmin_claimed_at"],
                    )
                    if should_push:
                        outcome = replace(outcome, garmin_status="pending", garmin_error=None)
                    should_push = False
                    should_reconcile = False
                elif should_push:
                    if existing["garmin_claimed_at"] is not None:
                        # Only reachable when the previous claimant died mid-push:
                        # a completed attempt clears the claim either way.
                        logger.warning(
                            "Re-claiming session %s after a stale Garmin push claim (%s); the previous "
                            "attempt did not record an outcome. If it actually reached Garmin, this "
                            "creates a duplicate activity.",
                            data.session_id, existing["garmin_claimed_at"],
                        )
                    # The claim, the status and the stale error move together in
                    # ONE statement. A concurrent reader must never see a row
                    # claimed for a fresh attempt while still carrying the
                    # previous attempt's 'failed' and its error text -- it would
                    # report a failure that is already being retried.
                    await db.execute(
                        "UPDATE strength_sessions SET garmin_claimed_at = ?, garmin_status = 'pending', "
                        "garmin_error = NULL, updated_at = ? WHERE id = ?",
                        (now, now, row_id),
                    )
                    outcome = replace(outcome, garmin_status="pending", garmin_error=None)
                elif should_reconcile:
                    # Claim without touching the status: it must stay 'unknown'
                    # for as long as it is unknown, so that a crash during
                    # reconciliation cannot leave behind a row that looks
                    # ordinarily retryable.
                    await db.execute(
                        "UPDATE strength_sessions SET garmin_claimed_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, row_id),
                    )

            await db.commit()

            if conflicts:
                logger.warning(
                    "Activity POST for session %s conflicts with stored row %s on fields: %s",
                    data.session_id, row_id, conflicts,
                )

            if should_push or should_reconcile:
                # Act on the STORED payload, never the incoming one.
                # First-write-wins means the row is the truth, and on a retry the
                # incoming body may legitimately differ -- pushing the newer body
                # would put something on Garmin that no stored row describes.
                # `incoming` and a strength_sessions Row share these key names
                # deliberately, so the fresh and retry cases read identically.
                stored = incoming if existing is None else existing
                # Composed ONCE and used for both the push and the reconciliation
                # lookup. Two compositions that could drift would mean a
                # reconciliation searching for a name the push never sent, which
                # reports "not on Garmin" for something that is, and duplicates.
                # The prefix comes from the STORED row when it has one, not from a
                # fresh persons.display_name read. The name is the only handle
                # reconciliation has -- Garmin offers no idempotency key, so an
                # ambiguous push is resolved by matching the exact title that was
                # sent. display_name is mutable, so an admin renaming the person
                # between the first push and the re-POST would send reconciliation
                # looking for a name that was never sent, it would conclude the
                # activity does not exist, and the retry would file a PERMANENT
                # duplicate (there is no delete path). Same first-write-wins rule
                # the `stored` payload above follows, for the same reason.
                # Current per-person Garmin links always receive the target
                # person's own session. New rows leave the legacy prefix NULL.
                # Migration 003 terminalizes old cross-account rows before they
                # can reach this path, so retaining a historical prefix cannot
                # revive the former provider routing.
                effective_display_name = stored["garmin_name_prefix"]
                activity_name = _activity_name(
                    stored["session_label"],
                    effective_display_name,
                    data.session_id,
                )
                if should_reconcile:
                    # ASK before pushing. The stored status is 'unknown', meaning
                    # the previous attempt may already have created this activity.
                    reconciled = await _reconcile_activity(
                        person_id=person_id,
                        start_time_utc=stored["start_time_utc"], activity_name=activity_name
                    )
                else:
                    reconciled = None

                if reconciled is not None:
                    # Either the activity was found (resolved 'synced'), or the
                    # lookup itself failed (still 'unknown'). Either way, no push.
                    outcome = reconciled
                else:
                    outcome = await _push_activity(
                        person_id=person_id,
                        start_time_utc=stored["start_time_utc"],
                        duration_min=stored["duration_seconds"] // 60,
                        activity_name=activity_name,
                        exercises_json=stored["exercises_json"],
                    )

                try:
                    await _record_activity_garmin_outcome(db, row_id, outcome)
                except Exception:
                    # The row is already durably committed by this point. A
                    # failure here must never surface as a 500 over
                    # already-successful data -- that would tell the client the
                    # whole request failed when it did not, and send it into a
                    # retry that re-pushes an activity Garmin already has.
                    #
                    logger.error(
                        "Post-commit Garmin outcome update failed for row %s (session %s); "
                        "recording a bounded unknown outcome.",
                        row_id, data.session_id,
                    )
                    if outcome.garmin_status in ("synced", "unknown"):
                        # An activity may exist on Garmin that this row does not
                        # point at, and the row currently reads 'pending', which
                        # IS retryable -- so once the claim ages out something
                        # would push again and duplicate it. Downgrading the row
                        # to 'unknown' is a much smaller write than the one that
                        # just failed and has a real chance of landing; if it does
                        # not, the log line above is the only record.
                        try:
                            await _mark_activity_outcome_unknown(db, row_id)
                        except Exception:
                            logger.error(
                                "Could not mark row %s 'unknown' after a failed outcome write; "
                                "this row may require manual reconciliation.",
                                row_id,
                            )
                    # Report what is DURABLE, not what this request hoped to
                    # write. Answering with the in-memory outcome would tell the
                    # client 'synced' over a row that still says otherwise, and
                    # the client would stop retrying a session nothing recorded.
                    try:
                        outcome = await _read_activity_outcome(db, row_id) or outcome
                    except Exception:
                        logger.error("Could not re-read row %s after a failed outcome write", row_id)
        finally:
            await db.close()

        result = {
            "success": True,
            "id": row_id,
            "session_id": data.session_id,
            "garmin_status": outcome.garmin_status,
            "garmin_activity_id": outcome.garmin_activity_id,
            "garmin_sets_status": outcome.garmin_sets_status,
        }
        if existing is None:
            result["person_id"] = person_id
            result["start_time_utc"] = incoming["start_time_utc"]
            result["duration_min"] = data.duration_min
        else:
            response.status_code = 200
            result["deduplicated"] = True
            if conflicts:
                result["conflict"] = True
                result["conflict_fields"] = conflicts
        # 'unknown' carries an error too: it is the only signal telling the client
        # why the session is neither confirmed nor retryable, and that a re-POST
        # reconciles rather than duplicates.
        safe_garmin_error = bounded_garmin_error(outcome.garmin_error)
        if outcome.garmin_status in ("failed", "unknown") and safe_garmin_error:
            result["garmin_error"] = safe_garmin_error
        return result

    @app.get("/p/{slug}/api/activity/{session_id}")
    async def get_activity(session_id: str, person_id: int = Depends(require_person("view"))):
        """One stored session by its client-generated id.

        person_id is in the WHERE clause, not just in the dependency: the
        dependency authorizes the caller for this person, and this predicate is
        what stops one person's session_id being read through another person's
        slug. 404, never 403, for a session that exists under someone else.
        """
        db = await get_db()
        try:
            row = await (
                await db.execute(
                    f"SELECT {_STRENGTH_SESSION_COLUMNS} FROM strength_sessions "
                    "WHERE person_id = ? AND session_id = ?",
                    (person_id, session_id),
                )
            ).fetchone()
        finally:
            await db.close()

        if row is None:
            raise HTTPException(status_code=404, detail="Strength session not found")
        return _serialize_strength_session(row)

    @app.get("/p/{slug}/api/strength-sessions")
    async def list_strength_sessions(
        since: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        person_id: int = Depends(require_person("view")),
    ):
        """This person's stored sessions, newest first.

        Named /strength-sessions rather than /activities because that path is
        already the dashboard's FIT-import list route over a different table, and
        the two concepts are deliberately separate. There is no ?person= --
        person addressing is by path segment only.
        """
        sql = (
            f"SELECT {_STRENGTH_SESSION_COLUMNS} FROM strength_sessions WHERE person_id = ?"
        )
        params: list = [person_id]
        if since is not None:
            # Validated rather than passed straight into the comparison:
            # start_time_utc is TEXT and SQLite compares it as a string, so a
            # malformed `since` does not error, it silently returns the wrong
            # window -- "yesterday" for `since=2026-9-1`, everything for
            # `since=banana`. A 422 says so instead.
            sql += " AND start_time_utc >= ?"
            params.append(_normalise_since(since))
        sql += " ORDER BY start_time_utc DESC LIMIT ?"
        params.append(limit)

        db = await get_db()
        try:
            rows = await (await db.execute(sql, tuple(params))).fetchall()
        finally:
            await db.close()

        sessions = [_serialize_strength_session(row) for row in rows]
        return {"count": len(sessions), "sessions": sessions}
