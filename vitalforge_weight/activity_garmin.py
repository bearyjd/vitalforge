"""Garmin-side machinery for strength sessions: the push, its error classification,
and recording the outcome.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.
from shared.garmin_client import (
    STRENGTH_ACTIVITY_TYPE_KEY,
    authenticate,
    build_exercise_sets_payload,
    extract_activity_id,
    find_activities_by_date,
    push_activity,
    push_activity_sets,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityPushOutcome:
    """What one Garmin attempt did. Frozen: the route threads it into the
    response and the post-commit UPDATE, and neither may edit it in place."""

    garmin_status: str
    garmin_activity_id: str | None
    garmin_error: str | None
    garmin_sets_status: str


# garmin_error is echoed to the client AND stored, so it is the one place a
# raw exception string from garminconnect crosses a trust boundary. Those
# strings have been observed to carry the account email (login failures) and
# request URLs with tokens in them.
_ERROR_EMAIL_RE = re.compile(r"[^\s@,;<>()\[\]]+@[^\s@,;<>()\[\]]+\.[^\s@,;<>()\[\]]+")


_ERROR_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")


_GARMIN_ERROR_MAX_CHARS = 300


# Exception class names that mean the request may ALREADY HAVE REACHED Garmin
# when it failed. Matched by name across the exception's MRO and its cause
# chain rather than by isinstance, because garminconnect layers requests,
# urllib3 and curl_cffi and which one surfaces is a detail of its transport
# that a version bump can change.
_AMBIGUOUS_TRANSPORT_ERRORS = frozenset({
    "ChunkedEncodingError",
    "IncompleteRead",
    "ProtocolError",
    "ReadTimeout",
    "ReadTimeoutError",
    "RemoteDisconnected",
    "Timeout",
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "BrokenPipeError",
    "CurlError",
})


# never established, so nothing was sent and a retry is safe. ConnectTimeout
# subclasses both Timeout and ConnectionError in requests, which is exactly
# why the precedence has to be explicit.
_PRE_SEND_ERRORS = frozenset({
    "ConnectTimeout",
    "ConnectTimeoutError",
    "ConnectionRefusedError",
    "NameResolutionError",
    "gaierror",
    "SSLError",
    "GarminConnectAuthenticationError",
    "GarminConnectTooManyRequestsError",
})


def _sanitise_error(message: str) -> str:
    """Strip credentials out of an exception string and bound its length.

    garminconnect's errors quote the request it was making, which on a login
    failure includes the account email and on a data call can include a URL
    carrying a token. That string is stored on the row and returned to the
    client, so it is sanitised once, here, at the boundary.
    """
    cleaned = _ERROR_EMAIL_RE.sub("[redacted]", message)
    cleaned = _ERROR_TOKEN_RE.sub("[redacted]", cleaned)
    if len(cleaned) > _GARMIN_ERROR_MAX_CHARS:
        cleaned = cleaned[: _GARMIN_ERROR_MAX_CHARS - 1].rstrip() + "…"
    return cleaned


def _exception_names(error: BaseException) -> set[str]:
    """Every class name in the exception's MRO, plus its cause/context chain.

    requests wraps urllib3 wraps http.client, so the interesting name is
    routinely two levels down in __cause__ rather than on the exception the
    caller sees.
    """
    names: set[str] = set()
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.update(klass.__name__ for klass in type(current).__mro__)
        current = current.__cause__ or current.__context__
    return names


def _push_outcome_is_ambiguous(error: BaseException) -> bool:
    """Whether a failed push may nonetheless have created the activity.

    The asymmetry is deliberate. Calling an ambiguous failure 'failed' invites
    a retry that files a SECOND activity for one session, permanently, because
    this service has no delete path. Calling a genuine failure 'unknown' costs
    one reconciliation lookup on the next re-POST. The second is much cheaper,
    but it is not free -- an 'unknown' row is never retried automatically --
    so the pre-send names are checked first and win.
    """
    names = _exception_names(error)
    if names & _PRE_SEND_ERRORS:
        return False
    return bool(names & _AMBIGUOUS_TRANSPORT_ERRORS)


def _exercise_sets_enabled() -> bool:
    """Whether to attempt the per-exercise exerciseSets upload.

    Read on EVERY call, not captured at import: this module is imported once
    per test session (importlib, via tests/conftest.py's weight_app_module),
    so a module-level constant would freeze whatever the environment held
    during the first test that imported it.

    Parsed as a string, never `bool(os.environ.get(...))` -- that is True for
    the literal "0", which is exactly the value this ships with.
    """
    return os.environ.get("VITALFORGE_GARMIN_EXERCISE_SETS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _garmin_time_zone() -> str:
    """The IANA zone name create_manual_activity's wall clock belongs to.

    Read per call, for the same reason as _exercise_sets_enabled. An unset or
    empty TZ falls back to UTC and SAYS SO -- guessing the host zone would
    silently misfile every activity by the offset amount, and a silent
    fallback makes that invisible. This service's own TZ is authoritative
    for the conversion; Cadence always sends UTC with an explicit offset and
    nobody adjusts it on the client side.
    """
    time_zone = os.environ.get("TZ") or ""
    if not time_zone.strip():
        logger.warning(
            "TZ is unset; filing this Garmin activity against UTC. Set TZ to the "
            "deployment's IANA zone or activities land at the wrong local time."
        )
        return "UTC"
    return time_zone.strip()


# How many trailing characters of session_id go into the Garmin activity
# title. Enough that two sessions the same person completed on one day cannot
# realistically share one, short enough to stay readable in an activity list.
_SESSION_MARKER_CHARS = 6


def _activity_name(session_label: str | None, display_name: str | None, session_id: str) -> str:
    """The Garmin activity title. Exact strings, D-015.

    display_name is non-None only on the cross-person override, and is read
    from persons.display_name for the TARGET person -- never from the request
    body, which cannot be trusted to name the person the path addressed.
    The dash is U+2014 with one space either side.

    The trailing session marker is what makes reconciliation EXACT. Garmin
    offers no idempotency key, so an ambiguous push can only be resolved by
    looking activities up and matching a name -- and "Cadence — Lower A" is
    not unique: the same label twice in one day (a morning and an evening
    session, or a redo) would collide, and reconciliation would adopt the
    wrong activity's id or skip a push that never happened. The last 6
    characters of the client-generated session_id disambiguate that at the
    cost of a short suffix in the title. Short deliberately: it is a
    disambiguator, not an identifier, and the whole session_id (up to 128
    chars) in a Garmin activity title would be unreadable.

    It is a SUFFIX so the human-meaningful part of the name still leads in
    Garmin's activity list, where long titles are truncated from the right.
    """
    label = session_label or "Strength"
    marker = session_id[-_SESSION_MARKER_CHARS:]
    if display_name is not None:
        return f"Cadence ({display_name}) — {label} [{marker}]"
    return f"Cadence — {label} [{marker}]"


def _push_activity(
    *,
    start_time_utc: str,
    duration_min: int,
    activity_name: str,
    exercises_json: str,
) -> ActivityPushOutcome:
    """Push one session to Garmin. NEVER RAISES -- mirrors _push_composition.

    The UTC-to-local-wall-clock conversion lives here rather than in
    shared/garmin_client.push_activity because tests patch that name in this
    module's namespace (tests/conftest.py); a conversion inside the patched
    function would be replaced by the fake, and the tests that pin it would
    be asserting on nothing.

    Takes the stored start time and exercises as RAW STRINGS rather than
    parsed values, so that parsing them happens inside this function's try
    and not the caller's. On a retry both come back out of SQLite, and a row
    whose start_time_utc or exercises_json Python cannot parse would
    otherwise raise on the request path AFTER the row was committed -- a 500
    over durable data, telling the client the whole request failed when it
    did not, and sending it into a retry that could create a second Garmin
    activity. post_weight hit exactly this on its own stored timestamp and
    parses defensively for the same reason. Unreachable through this route
    today, because every row is written by its own isoformat()/json.dumps();
    that was equally true of the weight path when the bug was found there.

    A row whose exercises_json cannot be read degrades to 'failed' rather
    than pushing an activity with no exercises: filing a session on Garmin
    while unable to read what was actually done is worse than reporting the
    problem and letting the client retry.

    ZoneInfo() construction is inside the try for the same reason: a typo'd
    TZ is an operator error that must degrade to garmin_status='failed' with
    the message stored.
    """
    # Split from the push below on purpose: everything in this block happens
    # BEFORE any request goes out, so its failures are unambiguously 'failed'
    # and safe to retry. Keeping them in one try with the push would force the
    # transport classifier to reason about our own json/ZoneInfo errors too.
    try:
        exercises = json.loads(exercises_json)
        authenticate()
        time_zone = _garmin_time_zone()
        start_local = datetime.fromisoformat(start_time_utc).astimezone(ZoneInfo(time_zone))
    except Exception as e:
        logger.error("Could not prepare the Garmin activity push: %s", e)
        return ActivityPushOutcome("failed", None, _sanitise_error(str(e)), "not_attempted")

    try:
        # LOCAL wall clock, no offset, plus the zone name alongside -- the
        # library's documented contract. A UTC string sent with a local zone
        # name, or a string carrying an offset, silently shifts the activity
        # by the offset amount and nobody notices for weeks.
        response = push_activity(
            start_datetime=start_local.strftime("%Y-%m-%dT%H:%M:%S.000"),
            time_zone=time_zone,
            # Passed explicitly rather than defaulted inside push_activity so
            # every argument Garmin receives is visible at the one call site,
            # and so a test can assert on the type key -- it is confirmed only
            # as a strength WORKOUT sportTypeKey, not as an ACTIVITY typeKey,
            # and JD's live get_activity_types() probe may yet rename it.
            type_key=STRENGTH_ACTIVITY_TYPE_KEY,
            distance_km=0.0,
            duration_min=duration_min,
            activity_name=activity_name,
        )
    except Exception as e:
        if _push_outcome_is_ambiguous(e):
            # The request may already have been on the wire. Retrying blind
            # would file a second activity for one session, and this service
            # has no delete path, so the duplicate would be permanent.
            logger.error(
                "Garmin activity push failed ambiguously (%s: %s); marking the session "
                "'unknown' -- a re-POST reconciles it by lookup before pushing again.",
                type(e).__name__, e,
            )
            return ActivityPushOutcome(
                "unknown", None,
                _sanitise_error(f"push outcome unknown ({type(e).__name__}): {e}"),
                "not_attempted",
            )
        logger.error("Failed to push activity to Garmin: %s", e)
        return ActivityPushOutcome("failed", None, _sanitise_error(str(e)), "not_attempted")

    activity_id = extract_activity_id(response)
    if activity_id is None:
        # The activity really is on Garmin -- 'synced' is the honest status.
        # There is simply no id to address it by, so the sets step cannot run.
        logger.warning(
            "create_manual_activity returned no usable activity id (%r); "
            "the activity was created but exercise sets cannot be attached.",
            response,
        )
        return ActivityPushOutcome("synced", None, None, "not_attempted")

    if not _exercise_sets_enabled():
        return ActivityPushOutcome("synced", activity_id, None, "not_attempted")

    try:
        payload = build_exercise_sets_payload(exercises, start_local)
        if not payload["exerciseSets"]:
            # Every exercise lacked a garmin_category. Nothing to send, and
            # nothing failed.
            return ActivityPushOutcome("synced", activity_id, None, "not_attempted")
        push_activity_sets(activity_id, payload)
    except Exception as e:
        # Only garmin_sets_status degrades. garmin_status stays 'synced'
        # because the activity itself genuinely exists on Garmin -- flipping
        # it to 'failed' would make the client re-POST and create a SECOND
        # activity for the same session.
        logger.error("Failed to push exercise sets for activity %s: %s", activity_id, e)
        return ActivityPushOutcome("synced", activity_id, None, "failed")

    return ActivityPushOutcome("synced", activity_id, None, "synced")


def _reconcile_activity(*, start_time_utc: str, activity_name: str) -> ActivityPushOutcome | None:
    """Ask Garmin whether an ambiguous push actually landed. NEVER RAISES.

    Returns a resolved outcome, or None meaning "Garmin answered and the
    activity is genuinely not there, so pushing again is safe".

    The three answers are deliberately distinct. A lookup that FAILS must not
    collapse into "not found" -- that is precisely the mistake that turns one
    session into two activities, so it returns 'unknown' again and the row
    stays un-retryable until someone asks once more.

    Matching is on the exact activity_name we would have sent, within the
    session's LOCAL date plus or minus a day. The window is one day wide on
    each side because the name is composed from a local wall clock and Garmin
    files by its own account-local date; a session near midnight can land on
    the neighbouring day. The name carries the session label and, for a D-015
    push, the person's display name, which makes a same-day collision between
    two genuinely different sessions unlikely but not impossible -- the honest
    limit of reconciling without an idempotency key Garmin does not offer.
    """
    try:
        time_zone = _garmin_time_zone()
        local_date = datetime.fromisoformat(start_time_utc).astimezone(ZoneInfo(time_zone)).date()
        activities = find_activities_by_date(
            (local_date - timedelta(days=1)).isoformat(),
            (local_date + timedelta(days=1)).isoformat(),
        )
    except Exception as e:
        logger.error("Could not reconcile an ambiguous Garmin push (%s: %s)", type(e).__name__, e)
        return ActivityPushOutcome(
            "unknown", None,
            _sanitise_error(f"reconciliation pending, Garmin lookup failed ({type(e).__name__}): {e}"),
            "not_attempted",
        )

    for activity in activities or []:
        if not isinstance(activity, dict) or activity.get("activityName") != activity_name:
            continue
        activity_id = activity.get("activityId")
        if activity_id is None:
            continue
        logger.warning(
            "Reconciled an ambiguous Garmin push: activity %s named %r already exists; "
            "not pushing again.", activity_id, activity_name,
        )
        # garmin_sets_status stays 'not_attempted': the activity exists, but
        # whether the exercise-sets call ever ran for it is not knowable from
        # this lookup, and claiming 'synced' would be a guess.
        return ActivityPushOutcome("synced", str(activity_id), None, "not_attempted")

    logger.info("Reconciliation found no activity named %r; the push is safe to repeat.", activity_name)
    return None


async def _record_activity_garmin_outcome(db, row_id: int, outcome: ActivityPushOutcome) -> None:
    """Persist one Garmin attempt's result against an already-committed row.

    A module-level function, not an inline statement, so a test can
    monkeypatch it to raise and prove the route still answers 202 rather than
    turning durable data into a 500.

    Also releases the row's Garmin claim, so the next retry (if this attempt
    failed) is free to take it. A claim left behind because this write itself
    failed ages out after _GARMIN_CLAIM_TIMEOUT_SECONDS.

    Unlike post_weight's equivalent, this does NOT depend on the
    single-worker assumption for its double-push safety: garmin_claimed_at is
    taken inside the route's BEGIN IMMEDIATE, so two requests cannot both
    decide to push regardless of how the pushes interleave.
    """
    await db.execute(
        "UPDATE strength_sessions SET garmin_status = ?, garmin_activity_id = ?, garmin_error = ?, "
        "garmin_sets_status = ?, garmin_claimed_at = NULL, updated_at = ? WHERE id = ?",
        (
            outcome.garmin_status,
            outcome.garmin_activity_id,
            outcome.garmin_error,
            outcome.garmin_sets_status,
            datetime.now(timezone.utc).isoformat(),
            row_id,
        ),
    )
    await db.commit()


async def _mark_activity_outcome_unknown(db, row_id: int, reason: str) -> None:
    """Last-resort downgrade when recording a real outcome failed.

    Deliberately the smallest possible write: no activity id, no sets status,
    just the status that stops an automatic retry, plus the claim cleared so
    a DELIBERATE re-POST can reconcile immediately rather than waiting out the
    claim timeout. Its whole purpose is to have a better chance of landing
    than the fuller UPDATE that just failed.
    """
    await db.execute(
        "UPDATE strength_sessions SET garmin_status = 'unknown', garmin_error = ?, "
        "garmin_claimed_at = NULL, updated_at = ? WHERE id = ?",
        (
            _sanitise_error(f"push outcome could not be recorded: {reason}"),
            datetime.now(timezone.utc).isoformat(),
            row_id,
        ),
    )
    await db.commit()


async def _read_activity_outcome(db, row_id: int) -> ActivityPushOutcome | None:
    """The row's DURABLE Garmin state, for answering after a write failed."""
    row = await (
        await db.execute(
            "SELECT garmin_status, garmin_activity_id, garmin_error, garmin_sets_status "
            "FROM strength_sessions WHERE id = ?",
            (row_id,),
        )
    ).fetchone()
    if row is None:
        return None
    return ActivityPushOutcome(
        row["garmin_status"], row["garmin_activity_id"], row["garmin_error"], row["garmin_sets_status"]
    )
