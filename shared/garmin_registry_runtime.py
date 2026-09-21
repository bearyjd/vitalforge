"""Durable registry admission, auth stamps, and link-row publication.

The public API remains in :mod:`shared.garmin_registry`, which imports these
helpers at the top and re-exports the ones tests and routes reach for.  This
module sits beneath it: it reads its limits and clock from
:mod:`shared.garmin_registry_common` and its error types from
:mod:`shared.garmin_registry_errors`, and never imports the facade.  The
``time`` and ``asyncio`` it calls are the same module objects the facade
exposes as monkeypatch seams, so a test that freezes the clock with
``setattr(garmin_registry.time, "time", ...)`` -- the stdlib module's
attribute, not the ``time`` name on the facade -- freezes the permit clock
here too.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import token_file_path
from garminconnect.exceptions import GarminConnectNotFoundError

from shared import garmin_registry_common
from shared.database import get_db
from shared.garmin_registry_errors import (
    REGISTRY_ERROR_CODES,
    GarminLink,
    GarminLinkAttemptRateLimited,
    GarminLinkConflict,
    GarminNotLinked,
    GarminOperationError,
    GarminRateLimited,
    GarminSessionExpired,
)

logger = logging.getLogger(__name__)

# garminconnect only carries an HTTP status in its message text ("API Error
# 401 - ...", "Mobile login: HTTP 403 (...)", "Widget embed returned 429").
# Parsing that text is not logging it.
_HTTP_STATUS_IN_MESSAGE_RE = re.compile(r"(?:HTTP|API Error|returned)\s+(\d{3})\b")


def _error_code(exc: Exception) -> str:
    """Classify without logging or returning third-party exception content.

    garminconnect's own types come first: they are the only reliable signal
    the library offers, since its HTTP status lives in message text rather
    than an attribute.  The generic fallbacks remain for transport errors
    raised beneath the library (httpx/requests/OS).
    """
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "rate_limited"
    if isinstance(exc, GarminConnectAuthenticationError):
        return _wrapped_auth_error_code(exc)
    if isinstance(exc, GarminConnectNotFoundError):
        # A missing resource is neither a connectivity nor a credential
        # problem; "network" would send an operator chasing the wrong fault.
        return "unknown"
    if isinstance(exc, GarminConnectConnectionError):
        return _http_status_code(_http_status(exc) or _http_status_from_message(exc), default="network")
    return _http_status_code(_http_status(exc), default=_error_code_from_type(exc))


def _wrapped_auth_error_code(exc: GarminConnectAuthenticationError) -> str:
    """The library's authentication error is a credential verdict only when it stands alone.

    ``Garmin._load_profile_and_settings`` re-raises whatever broke its
    profile fetch as ``GarminConnectAuthenticationError(...) from e`` -- a
    Cloudflare 403, a 503, a dropped connection -- so a cold token-store
    resume on a flaky network would otherwise stop a sync, answer 401 and
    ask for a re-link over something a retry fixes.  ``Garmin.login()``
    chains the same type from a cause it judged by message text alone
    ("unauthorized", "login failed"); that judgement stands unless the cause
    carries a verdict of its own (an HTTP status, a throttle or not-found
    type, a transport error); a chain made only of authentication errors
    is no cause at all.
    """
    cause = _first_foreign_cause(exc)
    if cause is None or not _carries_own_verdict(cause):
        return "auth_failed"
    return _error_code(cause)


def _first_foreign_cause(exc: BaseException) -> Exception | None:
    """The nearest explicit ``__cause__`` that is not another authentication error.

    Only ``__cause__`` is followed: ``__context__`` would let an unrelated
    exception that happened to be in flight decide a credential verdict.
    """
    seen: set[int] = set()
    current: BaseException | None = exc.__cause__
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if not isinstance(current, GarminConnectAuthenticationError):
            return current if isinstance(current, Exception) else None
        current = current.__cause__
    return None


def _carries_own_verdict(cause: Exception) -> bool:
    """Whether :func:`_error_code` would classify ``cause`` on evidence, not on a default."""
    if isinstance(cause, (GarminConnectTooManyRequestsError, GarminConnectNotFoundError)):
        return True
    if isinstance(cause, GarminConnectConnectionError):
        return (_http_status(cause) or _http_status_from_message(cause)) is not None
    return _http_status(cause) is not None or isinstance(cause, (ConnectionError, TimeoutError, OSError))


def _http_status_code(status: int | None, *, default: str) -> str:
    """Only a 401 is a credential verdict.

    Garmin answers a Cloudflare bot challenge or an IP-reputation block with
    a 403 (garminconnect's own "HTTP 403 (Cloudflare bot challenge)"); that
    is transient and clears with a retry, so it must not evict a session or
    stamp the link as needing a re-link.
    """
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "auth_failed"
    if status == 403:
        return "network"
    return default


def _http_status(exc: Exception) -> int | None:
    """An integer HTTP status from the exception or its attached response."""
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def _http_status_from_message(exc: Exception) -> int | None:
    match = _HTTP_STATUS_IN_MESSAGE_RE.search(str(exc))
    return int(match.group(1)) if match is not None else None


def _error_code_from_type(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "auth" in name or "credential" in name or "login" in name:
        return "auth_failed"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or "network" in name or "timeout" in name:
        return "network"
    return "unknown"


async def reserve_call_permit() -> None:
    """Consume one durable global leaky-bucket slot or raise retry-after."""
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT OR IGNORE INTO garmin_call_budget (singleton, next_allowed_at) VALUES (1, 0)"
        )
        row = await (
            await db.execute("SELECT next_allowed_at FROM garmin_call_budget WHERE singleton = 1")
        ).fetchone()
        if row is None:
            raise RuntimeError("Garmin call budget is unavailable")
        now = time.time()
        interval = garmin_registry_common.call_interval_seconds()
        next_allowed_at = float(row["next_allowed_at"])
        if next_allowed_at > now + interval:
            # A reservation only ever writes ``now + interval``, so a slot
            # further ahead than that is a clock regression (or a shortened
            # interval).  Honouring it would freeze every Garmin call until
            # the wall clock caught up; treat the slot as free instead.
            next_allowed_at = now
        if next_allowed_at > now:
            await db.rollback()
            raise GarminRateLimited(min(60, max(1, math.ceil(next_allowed_at - now))))
        await db.execute(
            "UPDATE garmin_call_budget SET next_allowed_at = ? WHERE singleton = 1",
            (now + interval,),
        )
        await db.commit()
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def _wait_for_call_permit(deadline_seconds: float | None = None) -> None:
    """Wait for a shared permit, sleeping ``retry_after`` between attempts.

    ``deadline_seconds`` bounds the total time spent sleeping: when the next
    sleep would exceed it, the last ``GarminRateLimited`` is raised so an
    interactive caller can answer with its usual retry response.  ``None``
    waits indefinitely, which is only appropriate for a logical operation
    already in flight (a cold call's post-login operation).
    """
    slept = 0.0
    while True:
        try:
            await reserve_call_permit()
            return
        except GarminRateLimited as exc:
            if deadline_seconds is not None and slept + exc.retry_after > deadline_seconds:
                raise
            slept += exc.retry_after
            await asyncio.sleep(exc.retry_after)


async def reserve_link_attempt(user_id: int) -> None:
    """Reserve one of a user's three credential-link attempts per 15 minutes."""
    user_id = int(user_id)
    if user_id < 1:
        raise ValueError("user_id must be a positive integer")
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = await (
            await db.execute(
                """
                SELECT attempted_at_1, attempted_at_2, attempted_at_3
                FROM garmin_link_attempts WHERE user_id = ?
                """,
                (user_id,),
            )
        ).fetchone()
        if row is None:
            await db.execute(
                """
                INSERT INTO garmin_link_attempts
                    (user_id, window_started_at, attempt_count, attempted_at_1)
                VALUES (?, ?, 1, ?)
                """,
                (user_id, now, now),
            )
        else:
            retained = sorted(
                float(value)
                for value in (row["attempted_at_1"], row["attempted_at_2"], row["attempted_at_3"])
                if value is not None and float(value) >= now - garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS
            )
            if len(retained) >= garmin_registry_common.LINK_ATTEMPT_LIMIT:
                await db.rollback()
                raise GarminLinkAttemptRateLimited(
                    min(
                        garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS,
                        max(1, math.ceil(retained[0] + garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS - now)),
                    )
                )
            retained.append(now)
            retained.sort()
            slots = retained + [None] * (garmin_registry_common.LINK_ATTEMPT_LIMIT - len(retained))
            await db.execute(
                """
                UPDATE garmin_link_attempts
                SET window_started_at = ?, attempt_count = ?,
                    attempted_at_1 = ?, attempted_at_2 = ?, attempted_at_3 = ?
                WHERE user_id = ?
                """,
                (retained[0], len(retained), *slots, user_id),
            )
        await db.commit()
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def check_link_attempt_quota(user_id: int) -> None:
    """Reject an already-exhausted window before spending a Garmin permit.

    Reservation remains the authority immediately before credential use; this
    preflight only prevents a known fourth attempt from starving unrelated
    Garmin work while preserving that race-safe final reservation.
    """
    user_id = int(user_id)
    if user_id < 1:
        raise ValueError("user_id must be a positive integer")
    db = await get_db()
    try:
        row = await (
            await db.execute(
                """
                SELECT attempted_at_1, attempted_at_2, attempted_at_3
                FROM garmin_link_attempts WHERE user_id = ?
                """,
                (user_id,),
            )
        ).fetchone()
        if row is None:
            return
        now = time.time()
        retained = [
            float(value)
            for value in (row["attempted_at_1"], row["attempted_at_2"], row["attempted_at_3"])
            if value is not None and float(value) >= now - garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS
        ]
        if len(retained) >= garmin_registry_common.LINK_ATTEMPT_LIMIT:
            oldest = min(retained)
            raise GarminLinkAttemptRateLimited(
                min(
                    garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS,
                    max(1, math.ceil(oldest + garmin_registry_common.LINK_ATTEMPT_WINDOW_SECONDS - now)),
                )
            )
    finally:
        await db.close()


async def actor_has_effective_manage(db, actor_id: int, session_version: int, person_id: int) -> bool:
    """Check the same active admin/manage authority the route used initially."""
    actor = await (
        await db.execute(
            """
            SELECT u.role,
                   EXISTS(
                       SELECT 1 FROM person_grants g
                       WHERE g.person_id = ? AND g.user_id = u.id
                         AND g.access IN ('manage', 'own')
                   ) AS can_manage
            FROM users u WHERE u.id = ? AND u.session_version = ?
            """,
            (person_id, actor_id, session_version),
        )
    ).fetchone()
    return actor is not None and (actor["role"] == "admin" or bool(actor["can_manage"]))


async def _load_link(person_id: int):
    db = await get_db()
    try:
        return await (
            await db.execute(
                "SELECT state, garmin_email, generation FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
    finally:
        await db.close()


async def _record_auth_success(person_id: int, generation: int) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_ok = ?, last_auth_error = NULL, last_auth_error_at = NULL
            WHERE person_id = ? AND generation = ?
            """,
            (garmin_registry_common.utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


async def _record_auth_failure(person_id: int, generation: int, code: str) -> None:
    safe_code = code if code in REGISTRY_ERROR_CODES else "unknown"
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_error = ?, last_auth_error_at = ?
            WHERE person_id = ? AND generation = ?
            """,
            (safe_code, garmin_registry_common.utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


async def _validate_link_target(person_id: int, actor_id: int, session_version: int) -> None:
    """Reject a stale actor or unreachable target before sending credentials.

    This is deliberately repeated by :func:`_publish_link` in its immediate
    transaction.  The first check prevents an already-invalid request from
    reaching Garmin; the second closes the interval while the credential login
    runs without retaining a database transaction across that network call.
    """
    db = await get_db()
    try:
        actor = await (
            await db.execute(
                "SELECT 1 FROM users WHERE id = ? AND session_version = ?", (actor_id, session_version)
            )
        ).fetchone()
        if actor is None:
            raise GarminSessionExpired()
        person = await (
            await db.execute(
                "SELECT 1 FROM persons WHERE id = ? AND archived_at IS NULL", (person_id,)
            )
        ).fetchone()
        if person is None:
            raise GarminNotLinked(person_id)
    finally:
        await db.close()


async def _publish_link(
    person_id: int,
    actor_id: int,
    session_version: int,
    canonical_email: str,
    generation: int,
) -> GarminLink:
    """Atomically verify the actor, allocate generation, and publish a link."""
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        if not await actor_has_effective_manage(db, actor_id, session_version, person_id):
            await db.rollback()
            raise GarminSessionExpired()
        person = await (
            await db.execute(
                "SELECT 1 FROM persons WHERE id = ? AND archived_at IS NULL", (person_id,)
            )
        ).fetchone()
        if person is None:
            await db.rollback()
            raise GarminNotLinked(person_id)
        conflict = await (
            await db.execute(
                "SELECT person_id FROM garmin_links WHERE garmin_email = ?", (canonical_email,)
            )
        ).fetchone()
        if conflict is not None and conflict["person_id"] != person_id:
            await db.rollback()
            raise GarminLinkConflict()

        ledger = await (
            await db.execute(
                "SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
        current = await (
            await db.execute("SELECT generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
        # ``generation`` was reserved in the ledger before the credential
        # login (_reserve_generation).  The person flock keeps that stable
        # across lifecycle callers; refuse, rather than publish a staged
        # client under a surprise generation, if a direct DB writer moved the
        # ledger or published a newer link while the login was running.
        reserved = ledger is not None and int(ledger["generation"]) == generation
        superseded = current is not None and int(current["generation"]) >= generation
        if not reserved or superseded:
            await db.rollback()
            logger.warning(
                "Garmin link publication for person %s refused: generation reservation changed",
                person_id,
            )
            raise GarminOperationError("unknown")

        now = garmin_registry_common.utc_now()
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, linked_by,
                 updated_at, last_auth_ok, last_auth_error, last_auth_error_at)
            VALUES (?, 'linked', ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(person_id) DO UPDATE SET
                state = 'linked', garmin_email = excluded.garmin_email,
                generation = excluded.generation, linked_at = excluded.linked_at,
                linked_by = excluded.linked_by, updated_at = excluded.updated_at,
                last_auth_ok = excluded.last_auth_ok, last_auth_error = NULL,
                last_auth_error_at = NULL
            """,
            (person_id, canonical_email, generation, now, actor_id, now, now),
        )
        await db.execute(
            """
            INSERT INTO garmin_link_generations (person_id, generation) VALUES (?, ?)
            ON CONFLICT(person_id) DO UPDATE SET generation = excluded.generation
            """,
            (person_id, generation),
        )
        await db.commit()
        return GarminLink(person_id, generation, "linked")
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


def _token_store_has_content(path: Path) -> bool:
    """Whether garminconnect left a non-empty token file, without reading it.

    ``login()`` returns normally even when its persistence step was skipped;
    a link published on an empty directory would resume nothing on the next
    cold call.
    """
    try:
        token_path = token_file_path(str(path))
        return token_path.is_file() and not token_path.is_symlink() and token_path.stat().st_size > 0
    except (OSError, ValueError):
        return False
