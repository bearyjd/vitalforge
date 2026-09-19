"""Durable registry admission and legacy-store helpers.

The public API remains in :mod:`shared.garmin_registry`.  These helpers read
their mutable configuration and collaborators from that facade at call time so
existing startup code and test monkeypatch seams keep their exact behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from pathlib import Path

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import token_file_path
from garminconnect.exceptions import GarminConnectNotFoundError

from shared import garmin_client
from shared.database import get_db

logger = logging.getLogger(__name__)

# One-time marker in ``auth_migrations`` (the same table
# ``shared.auth.bootstrap_migrated_token`` uses).  ``assert_schema_understood``
# only reads ``schema_migrations``, so an older image ignores this marker
# instead of boot-looping on it.
_LEGACY_ADOPTION_MARKER = "legacy-garth-store-adopted"
_LEGACY_GENERATION = 1
# garminconnect only carries an HTTP status in its message text ("API Error
# 401 - ...", "Mobile login: HTTP 403 (...)", "Widget embed returned 429").
# Parsing that text is not logging it.
_HTTP_STATUS_IN_MESSAGE_RE = re.compile(r"(?:HTTP|API Error|returned)\s+(\d{3})\b")


def _registry():
    # Delayed to avoid a facade/import cycle and to retain its public patch
    # points (GARTH_TOKEN_DIR, time, asyncio, and error classes).
    from shared import garmin_registry

    return garmin_registry


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
        return "auth_failed"
    if isinstance(exc, GarminConnectNotFoundError):
        # A missing resource is neither a connectivity nor a credential
        # problem; "network" would send an operator chasing the wrong fault.
        return "unknown"
    if isinstance(exc, GarminConnectConnectionError):
        return _http_status_code(_http_status(exc) or _http_status_from_message(exc), default="network")
    return _http_status_code(_http_status(exc), default=_error_code_from_type(exc))


def _http_status_code(status: int | None, *, default: str) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth_failed"
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
    registry = _registry()
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
        now = registry.time.time()
        interval = registry._call_interval_seconds()
        next_allowed_at = float(row["next_allowed_at"])
        if next_allowed_at > now + interval:
            # A reservation only ever writes ``now + interval``, so a slot
            # further ahead than that is a clock regression (or a shortened
            # interval).  Honouring it would freeze every Garmin call until
            # the wall clock caught up; treat the slot as free instead.
            next_allowed_at = now
        if next_allowed_at > now:
            await db.rollback()
            raise registry.GarminRateLimited(min(60, max(1, math.ceil(next_allowed_at - now))))
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
    registry = _registry()
    slept = 0.0
    while True:
        try:
            await registry.reserve_call_permit()
            return
        except registry.GarminRateLimited as exc:
            if deadline_seconds is not None and slept + exc.retry_after > deadline_seconds:
                raise
            slept += exc.retry_after
            await registry.asyncio.sleep(exc.retry_after)


async def reserve_link_attempt(user_id: int) -> None:
    """Reserve one of a user's three credential-link attempts per 15 minutes."""
    registry = _registry()
    user_id = int(user_id)
    if user_id < 1:
        raise ValueError("user_id must be a positive integer")
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = registry.time.time()
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
                if value is not None and float(value) >= now - registry._LINK_ATTEMPT_WINDOW_SECONDS
            )
            if len(retained) >= registry._LINK_ATTEMPT_LIMIT:
                await db.rollback()
                raise registry.GarminLinkAttemptRateLimited(
                    min(
                        registry._LINK_ATTEMPT_WINDOW_SECONDS,
                        max(1, math.ceil(retained[0] + registry._LINK_ATTEMPT_WINDOW_SECONDS - now)),
                    )
                )
            retained.append(now)
            retained.sort()
            slots = retained + [None] * (registry._LINK_ATTEMPT_LIMIT - len(retained))
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
    registry = _registry()
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
        now = registry.time.time()
        retained = [
            float(value)
            for value in (row["attempted_at_1"], row["attempted_at_2"], row["attempted_at_3"])
            if value is not None and float(value) >= now - registry._LINK_ATTEMPT_WINDOW_SECONDS
        ]
        if len(retained) >= registry._LINK_ATTEMPT_LIMIT:
            oldest = min(retained)
            raise registry.GarminLinkAttemptRateLimited(
                min(
                    registry._LINK_ATTEMPT_WINDOW_SECONDS,
                    max(1, math.ceil(oldest + registry._LINK_ATTEMPT_WINDOW_SECONDS - now)),
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
    registry = _registry()
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_ok = ?, last_auth_error = NULL, last_auth_error_at = NULL
            WHERE person_id = ? AND generation = ?
            """,
            (registry._utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


async def _record_auth_failure(person_id: int, generation: int, code: str) -> None:
    registry = _registry()
    safe_code = code if code in registry._ERROR_CODES else "unknown"
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_error = ?, last_auth_error_at = ?
            WHERE person_id = ? AND generation = ?
            """,
            (safe_code, registry._utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


def _looks_like_legacy_token_store(path: Path) -> bool:
    """Check garminconnect's exact legacy token path without reading it."""
    try:
        token_path = token_file_path(str(path))
        return path.is_dir() and token_path.is_file() and not token_path.is_symlink()
    except (OSError, ValueError):
        return False


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


def resolve_token_dir(person_id: int, generation: int) -> Path:
    """Every durable link resumes from its own immutable generation directory."""
    return _registry()._generation_token_dir(int(person_id), int(generation))


async def bootstrap_legacy_token_store() -> bool:
    """Adopt a verified pre-Phase-3 flat token store for the primary person, once.

    Adoption moves ``<root>/garmin_tokens.json`` under the primary person's
    ``generation-1`` directory and publishes an ordinary ``linked`` row, then
    records :data:`_LEGACY_ADOPTION_MARKER` in ``auth_migrations`` in the
    same transaction.  The marker is what makes this one-time: it survives
    unlink, re-link, archive, and even a restored backup of the flat store.
    """
    registry = _registry()
    try:
        root = registry._ensure_token_root()
    except OSError:
        logger.warning("Legacy Garmin token-store adoption could not prepare its token root")
        return False
    legacy_email = os.getenv("GARMIN_EMAIL")
    if not isinstance(legacy_email, str):
        return False
    try:
        canonical_email = registry._canonical_email(legacy_email)
    except registry.GarminLinkInputError:
        return False
    try:
        async with registry.legacy_store_flock():
            return await _adopt_legacy_store_locked(root, canonical_email)
    except registry.GarminRateLimited:
        logger.info("Legacy Garmin token-store adoption deferred by the global call limiter")
        return False
    except Exception as exc:
        logger.warning("Legacy Garmin token-store adoption was not completed (%s)", type(exc).__name__)
        return False


async def _adopt_legacy_store_locked(root: Path, canonical_email: str) -> bool:
    """The adoption decision tree; the caller holds ``legacy_store_flock``.

    The moved-store branch exists for a process killed between the file move
    and the database commit: the flat store is already under ``generation-1``
    and only the publication is missing.
    """
    registry = _registry()
    if await _legacy_adoption_recorded():
        return False
    person_id = await _adoptable_primary_person(canonical_email)
    if person_id is None:
        return False
    durable = registry._generation_token_dir(person_id, _LEGACY_GENERATION)
    if _looks_like_legacy_token_store(durable):
        if not await _verify_token_store(person_id, canonical_email, durable):
            return False
        return await _publish_legacy_adoption(person_id, canonical_email)
    if not _looks_like_legacy_token_store(root):
        return False
    return await _adopt_flat_store(person_id, canonical_email, root, durable)


async def _adopt_flat_store(person_id: int, canonical_email: str, root: Path, durable: Path) -> bool:
    """Verify, move, then publish; a failed publication puts the file back."""
    if not await _verify_token_store(person_id, canonical_email, root):
        return False
    source = token_file_path(str(root))
    target = token_file_path(str(durable))
    await asyncio.to_thread(_move_token_file, source, target)
    published = False
    try:
        published = await _publish_legacy_adoption(person_id, canonical_email)
    finally:
        if not published:
            await asyncio.to_thread(_restore_token_file, target, source)
    return published


async def _verify_token_store(person_id: int, canonical_email: str, token_dir: Path) -> bool:
    """Resume a token store once, then drop the client so nothing dumps there later.

    The verification client's SDK persistence path is ``token_dir``; a cached
    copy would silently re-dump refreshed tokens to the flat root after the
    move.  A cold :func:`shared.garmin_registry.call` re-logs-in from the
    durable directory instead.
    """
    registry = _registry()
    await registry.reserve_call_permit()
    try:
        await asyncio.to_thread(
            garmin_client.authenticate, person_id, _LEGACY_GENERATION, token_dir, canonical_email, None
        )
    except Exception as exc:
        logger.warning(
            "Legacy Garmin token store could not be verified (%s); leaving it unbound", type(exc).__name__
        )
        return False
    finally:
        garmin_client.forget(person_id, _LEGACY_GENERATION)
    return True


def _move_token_file(source: Path, target: Path) -> None:
    """Move the flat store into its private generation directory atomically."""
    garmin_client._ensure_token_dir(target.parent)
    os.replace(source, target)


def _restore_token_file(target: Path, source: Path) -> None:
    """Best effort: put a moved flat store back so the next boot can retry."""
    try:
        if target.is_file() and not source.exists():
            os.replace(target, source)
    except OSError as exc:
        logger.warning(
            "Legacy Garmin token store could not be restored after a failed adoption (%s)",
            type(exc).__name__,
        )


async def _legacy_adoption_recorded() -> bool:
    db = await get_db()
    try:
        return await _legacy_adoption_recorded_in(db)
    finally:
        await db.close()


async def _legacy_adoption_recorded_in(db) -> bool:
    row = await (
        await db.execute("SELECT 1 FROM auth_migrations WHERE name = ?", (_LEGACY_ADOPTION_MARKER,))
    ).fetchone()
    return row is not None


async def _adoptable_primary_person(canonical_email: str) -> int | None:
    db = await get_db()
    try:
        return await _adoptable_primary_person_in(db, canonical_email)
    finally:
        await db.close()


async def _adoptable_primary_person_in(db, canonical_email: str) -> int | None:
    """The primary person's id while the new lifecycle has never touched it.

    A link row, a generation-ledger row, or another person owning the address
    all mean the per-person lifecycle already has authority over this person;
    the flat store is then stale residue, never a credential to revive.  The
    ledger check is also what keeps the moved-store recovery branch honest: a
    route-driven link reserves its generation before logging in, so a
    ``generation-1`` directory with no ledger row can only be an interrupted
    adoption.
    """
    primary = await (await db.execute("SELECT id FROM persons WHERE is_primary = 1")).fetchone()
    if primary is None:
        return None
    person_id = int(primary["id"])
    existing = await (
        await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
    ).fetchone()
    ledger = await (
        await db.execute("SELECT 1 FROM garmin_link_generations WHERE person_id = ?", (person_id,))
    ).fetchone()
    conflict = await (
        await db.execute("SELECT 1 FROM garmin_links WHERE garmin_email = ?", (canonical_email,))
    ).fetchone()
    if existing is not None or ledger is not None or conflict is not None:
        return None
    return person_id


async def _publish_legacy_adoption(person_id: int, canonical_email: str) -> bool:
    """Publish the adopted store as a ``linked`` generation 1 with its marker."""
    registry = _registry()
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        if await _legacy_adoption_recorded_in(db) or (
            await _adoptable_primary_person_in(db, canonical_email) != person_id
        ):
            await db.rollback()
            return False
        now = registry._utc_now()
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at, last_auth_ok)
            VALUES (?, 'linked', ?, ?, ?, ?, ?)
            """,
            (person_id, canonical_email, _LEGACY_GENERATION, now, now, now),
        )
        await db.execute(
            "INSERT INTO garmin_link_generations (person_id, generation) VALUES (?, ?)",
            (person_id, _LEGACY_GENERATION),
        )
        await db.execute(
            "INSERT INTO auth_migrations (name, completed_at) VALUES (?, ?)",
            (_LEGACY_ADOPTION_MARKER, now),
        )
        await db.commit()
        return True
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()
