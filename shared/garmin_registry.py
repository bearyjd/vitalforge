"""The asynchronous, database-aware boundary for Garmin operations.

``shared.garmin_client`` intentionally knows nothing about SQLite or link
state.  Code that touches Garmin must instead call :func:`call`; that makes a
durable rate admission, a fresh link read, and a stable person lock one
operation rather than conventions individual routes can forget.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
import time  # noqa: F401 - public registry clock/monkeypatch seam
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from garminconnect import Garmin

from shared import garmin_client
from shared.database import get_db
from shared.garmin_registry_errors import (
    _ERROR_CODES,  # noqa: F401 - runtime helpers read it through this facade
    GarminAuthenticationError,
    GarminLink,
    GarminLinkAttemptRateLimited,  # noqa: F401 - public facade export
    GarminLinkConflict,
    GarminLinkInputError,
    GarminNotLinked,
    GarminOperationError,
    GarminRateLimited,
    GarminRegistryError,
    GarminSessionExpired,
)
from shared.garmin_registry_locks import (
    legacy_store_flock,  # noqa: F401 - public facade export
    person_flock,
)
from shared.garmin_registry_runtime import (
    _error_code,
    _load_link,
    _record_auth_failure,
    _record_auth_success,
    _token_store_has_content,
    _wait_for_call_permit,
    actor_has_effective_manage,
    bootstrap_legacy_token_store,  # noqa: F401 - public facade export
    check_link_attempt_quota,
    reserve_call_permit,
    reserve_link_attempt,
    resolve_token_dir,
)

logger = logging.getLogger(__name__)

GARTH_TOKEN_DIR = Path(os.getenv("GARTH_TOKEN_DIR", "/app/data/.garth"))

_T = TypeVar("_T")
# 'legacy_bound' / 'legacy_disabled' are retired: boot-time adoption now moves
# the flat store under person-<id>/generation-1/ and publishes 'linked'.  The
# DDL still tolerates the old values (SQLite cannot alter a CHECK), so a row
# carrying one is simply not usable until it is re-linked.
_VALID_LINK_STATES = frozenset({"linked"})
_LINK_ATTEMPT_LIMIT = 3
_LINK_ATTEMPT_WINDOW_SECONDS = 15 * 60
# The longest call() may sleep for a permit while holding a person flock.
_MAX_INTERACTIVE_WAIT_SECONDS = 30.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _call_interval_seconds() -> float:
    """Read the deployment interval defensively, clamped to one minute."""
    try:
        configured = float(os.getenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2"))
    except ValueError:
        configured = 2.0
    return min(60.0, max(1.0, configured))


def _ensure_token_root() -> Path:
    """Make the root private even when it existed before this release."""
    GARTH_TOKEN_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    GARTH_TOKEN_DIR.chmod(0o700)
    return GARTH_TOKEN_DIR


def _canonical_email(email: str) -> str:
    """Canonicalize account identity without attempting email validation."""
    if not isinstance(email, str):
        raise GarminLinkInputError()
    canonical = email.strip().casefold()
    if not canonical:
        raise GarminLinkInputError()
    return canonical


def _staging_token_dir(person_id: int, generation: int) -> Path:
    root = _ensure_token_root()
    path = root / f".person-{person_id}-generation-{generation}-{uuid.uuid4().hex}.staging"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _person_token_root(person_id: int) -> Path:
    return _ensure_token_root() / f"person-{person_id}"


def _generation_token_dir(person_id: int, generation: int) -> Path:
    return _person_token_root(person_id) / f"generation-{generation}"


def _remove_token_dir(path: Path) -> None:
    """Remove a registry-owned stage or generation directory, never GARTH root."""
    root = _ensure_token_root()
    is_stage = path.parent == root and path.name.startswith(".person-")
    is_generation = (
        path.parent.parent == root
        and path.parent.name.startswith("person-")
        and path.name.startswith("generation-")
    )
    if not (is_stage or is_generation):
        raise RuntimeError("refusing unsafe Garmin token cleanup")
    if path.exists():
        shutil.rmtree(path)


def _install_staged_token_dir(staging: Path, target: Path) -> None:
    """Atomically publish a new immutable generation directory.

    It deliberately never replaces an old generation: until the database
    transaction points at ``target``, the durable link still resolves its old
    directory.  A crash can leave an unused new directory, never a link to a
    different account.
    """
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.parent.chmod(0o700)
    if target.exists():
        raise RuntimeError("generation token directory already exists")
    os.replace(staging, target)
    target.chmod(0o700)


def _remove_person_token_root(person_id: int) -> None:
    if person_id < 1:
        raise RuntimeError("refusing unsafe Garmin token cleanup")
    root = _ensure_token_root()
    path = _person_token_root(person_id)
    if path.parent != root or path.name != f"person-{person_id}":
        raise RuntimeError("refusing unsafe Garmin token cleanup")
    if path.exists():
        shutil.rmtree(path)


def _sweep_stale_staging_dirs(person_id: int) -> None:
    """Remove this person's staging residue; the caller holds the person flock.

    A staging directory outlives its link attempt only after SIGKILL or a
    cancelled login whose worker thread dumped tokens after the attempt's own
    cleanup ran.  Under the flock no attempt for this person is in flight, so
    every match is residue.
    """
    root = _ensure_token_root()
    for child in root.glob(f".person-{int(person_id)}-generation-*.staging"):
        if child.is_dir() and not child.is_symlink():
            _remove_token_dir(child)


async def forget_and_remove_person_token_store(person_id: int) -> None:
    """Evict a person's cached clients and remove its owned token directories.

    The caller must already hold :func:`person_flock`.  Keeping the cache
    eviction before the filesystem operation means a cleanup failure can leave
    an inert artifact but never a usable in-process credential.  The flat
    legacy root is never touched: once adopted, its store lives under
    ``person-<id>/`` like any other, and the ``auth_migrations`` marker keeps
    a later copy at the root from being adopted again.
    """
    person_id = int(person_id)
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    garmin_client.forget(person_id)
    await asyncio.to_thread(_remove_person_token_root, person_id)
    await asyncio.to_thread(_sweep_stale_staging_dirs, person_id)


def _sweep_unreferenced_generation_dirs(person_id: int, durable_generation: int | None) -> None:
    """Delete only obsolete immutable generations while caller owns the flock.

    Staging directories live beside ``person-<id>`` rather than beneath it,
    and are intentionally outside this sweep.  A durable generation is never
    removed, even if its contents are incomplete; deleting it would turn a
    recoverable token-store failure into a wrong-account fallback.
    """
    person_root = _person_token_root(person_id)
    if not person_root.is_dir():
        return
    for child in person_root.iterdir():
        if not child.is_dir() or not child.name.startswith("generation-"):
            continue
        try:
            generation = int(child.name.removeprefix("generation-"))
        except ValueError:
            continue
        if generation < 1 or generation == durable_generation:
            continue
        _remove_token_dir(child)


async def _next_generation(db, person_id: int) -> int:
    ledger = await (
        await db.execute(
            "SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,)
        )
    ).fetchone()
    link = await (
        await db.execute("SELECT generation FROM garmin_links WHERE person_id = ?", (person_id,))
    ).fetchone()
    return max(ledger["generation"] if ledger is not None else 0, link["generation"] if link is not None else 0) + 1


async def _reserve_generation(person_id: int) -> int:
    """Durably allocate the next generation before any credential login.

    The ledger is a monotonic allocator: a cancelled or failed attempt simply
    burns its number.  A login thread that outlives its cancelled request can
    therefore only ever cache ``(person, N)`` or dump tokens into
    ``generation-N`` for an N no published link will resolve.
    """
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        generation = await _next_generation(db, person_id)
        await db.execute(
            """
            INSERT INTO garmin_link_generations (person_id, generation) VALUES (?, ?)
            ON CONFLICT(person_id) DO UPDATE SET generation = excluded.generation
            """,
            (person_id, generation),
        )
        await db.commit()
        return generation
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
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

        now = _utc_now()
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


async def link(
    person_id: int,
    actor_id: int,
    session_version: int,
    email: str,
    password: str,
) -> GarminLink:
    """Link or re-link a person using one transient credential submission.

    The password is passed directly to the synchronous adapter and never
    logged, persisted, or returned.  A failed re-link leaves the existing DB
    row and final token directory untouched because authentication uses a
    private staging directory first.
    """
    person_id = int(person_id)
    actor_id = int(actor_id)
    session_version = int(session_version)
    if person_id < 1 or actor_id < 1 or session_version < 1 or not isinstance(password, str) or not password:
        raise GarminLinkInputError()
    canonical_email = _canonical_email(email)

    try:
        # A known exhausted attempt window must not consume the deployment-wide
        # Garmin budget. The final durable reservation below remains the
        # concurrency authority before any credential reaches Garmin.
        await check_link_attempt_quota(actor_id)
        # Reserve before taking the lifecycle flock.  BEGIN IMMEDIATE must
        # never wait behind a flock holder that will itself need SQLite; this
        # order keeps the two cross-process authorities deadlock-free.
        await reserve_call_permit()
        async with person_flock(person_id):
            return await _link_locked(person_id, actor_id, session_version, canonical_email, password)
    except GarminRegistryError:
        raise
    except Exception as exc:
        logger.warning("Garmin link for person %s failed outside its bounded errors (%s)", person_id, type(exc).__name__)
        raise GarminOperationError("unknown") from None


async def _link_locked(
    person_id: int, actor_id: int, session_version: int, canonical_email: str, password: str
) -> GarminLink:
    """The lifecycle body; the caller holds :func:`person_flock`."""
    existing = await _load_link(person_id)
    await asyncio.to_thread(
        _sweep_unreferenced_generation_dirs,
        person_id,
        int(existing["generation"]) if existing is not None else None,
    )
    await asyncio.to_thread(_sweep_stale_staging_dirs, person_id)
    # This durable user-scoped throttle must succeed before any credential
    # reaches Garmin.  It remains inside the flock so a re-link cannot race
    # this person's generation/token swap.
    await reserve_link_attempt(actor_id)
    await _validate_link_target(person_id, actor_id, session_version)
    generation = await _reserve_generation(person_id)
    target = _generation_token_dir(person_id, generation)
    # The ledger reservation makes a reachable ``target`` impossible for a
    # fresh number; keep the removal as a cheap guard against a directory
    # created outside the registry.
    if target.exists():
        await asyncio.to_thread(_remove_token_dir, target)
    staging = _staging_token_dir(person_id, generation)
    try:
        await _login_into_staging(person_id, generation, staging, canonical_email, password)
        published = await _install_and_publish(
            person_id, actor_id, session_version, canonical_email, generation, staging, target
        )
        if existing is not None:
            await _remove_superseded_generation(person_id, int(existing["generation"]))
        return published
    finally:
        if staging.exists():
            try:
                await asyncio.to_thread(_remove_token_dir, staging)
            except Exception as exc:
                logger.warning(
                    "Garmin staging cleanup for person %s did not complete (%s)", person_id, type(exc).__name__
                )


async def _login_into_staging(
    person_id: int, generation: int, staging: Path, canonical_email: str, password: str
) -> None:
    """Run the credential login off the loop; abandon it safely if cancelled.

    The worker thread cannot be interrupted.  If the request is cancelled
    mid-login the thread will still cache ``(person, generation)`` and may
    dump tokens into ``staging`` later; ``generation`` is already burned in
    the ledger, so neither can collide with a later link, and the completion
    callback discards both (mirroring :func:`_close_acquired_handle_when_done`).
    """
    login = asyncio.create_task(
        asyncio.to_thread(garmin_client.authenticate, person_id, generation, staging, canonical_email, password)
    )
    try:
        await asyncio.shield(login)
    except asyncio.CancelledError:
        _discard_abandoned_login(login, person_id, generation, staging)
        raise
    except Exception as exc:
        code = _error_code(exc)
        garmin_client.forget(person_id, generation)
        raise GarminAuthenticationError(code) from None
    if not _token_store_has_content(staging):
        logger.warning("Garmin login for person %s left no token store to publish", person_id)
        garmin_client.forget(person_id, generation)
        raise GarminOperationError("unknown")


def _discard_abandoned_login(login: asyncio.Task, person_id: int, generation: int, staging: Path) -> None:
    """Evict whatever a cancelled credential login eventually produces."""

    def discard(completed: asyncio.Task) -> None:
        if not completed.cancelled():
            try:
                completed.result()
            except Exception as exc:
                logger.info("Abandoned Garmin login for person %s ended with %s", person_id, type(exc).__name__)
        garmin_client.forget(person_id, generation)
        try:
            _remove_token_dir(staging)
        except Exception as exc:
            logger.warning(
                "Abandoned Garmin login cleanup for person %s did not complete (%s)",
                person_id,
                type(exc).__name__,
            )

    login.add_done_callback(discard)


async def _install_and_publish(
    person_id: int,
    actor_id: int,
    session_version: int,
    canonical_email: str,
    generation: int,
    staging: Path,
    target: Path,
) -> GarminLink:
    """Publish the staged store durably, then drop the link-time client.

    The eviction is unconditional (``finally``): the link-time client's SDK
    persistence path is the staging directory that gets renamed away, so a
    cached copy would dump a refreshed token into a path no durable link
    resolves.  That holds on every exit -- success, a refused publication,
    or a request cancelled at the publication's post-commit ``db.close()``.
    Every generation goes, and the first call() cold-loads from ``target``:
    one extra login, never a silently lost token.
    """
    try:
        try:
            await asyncio.to_thread(_install_staged_token_dir, staging, target)
        except Exception as exc:
            logger.warning(
                "Garmin token store for person %s could not be installed (%s)", person_id, type(exc).__name__
            )
            raise GarminOperationError("unknown") from None
        try:
            return await _publish_link(person_id, actor_id, session_version, canonical_email, generation)
        except Exception:
            try:
                await asyncio.to_thread(_remove_token_dir, target)
            except Exception as exc:
                logger.warning(
                    "Unpublished Garmin generation cleanup for person %s did not complete (%s)",
                    person_id,
                    type(exc).__name__,
                )
            raise
    finally:
        garmin_client.forget(person_id)


async def _remove_superseded_generation(person_id: int, generation: int) -> None:
    """Best effort: the new generation is already durable, so a directory
    that will not go is residue for the next call() to sweep, not an error
    to answer the successful link with."""
    try:
        await asyncio.to_thread(_remove_token_dir, _generation_token_dir(person_id, generation))
    except Exception as exc:
        logger.warning(
            "Superseded Garmin generation cleanup for person %s did not complete (%s)",
            person_id,
            type(exc).__name__,
        )


async def relink(
    person_id: int, actor_id: int, session_version: int, email: str, password: str
) -> GarminLink:
    """Explicit alias for route handlers that distinguish first-link wording."""
    return await link(person_id, actor_id, session_version, email, password)


async def unlink(person_id: int, actor_id: int, session_version: int) -> bool:
    """Remove a person's link, its cached clients, and its token directories.

    The database change commits before cache eviction and filesystem cleanup;
    an interrupted cleanup can leave only an inert credential artifact, never
    a usable durable link.
    """
    person_id = int(person_id)
    actor_id = int(actor_id)
    session_version = int(session_version)
    if person_id < 1 or actor_id < 1 or session_version < 1:
        raise GarminLinkInputError()
    try:
        async with person_flock(person_id):
            deleted = await _delete_link_row(person_id, actor_id, session_version)
            if deleted:
                garmin_client.forget(person_id)
            try:
                if deleted:
                    await asyncio.to_thread(_remove_person_token_root, person_id)
                # Staging residue belongs to no link row, so it is swept even
                # when there was nothing to unlink.
                await asyncio.to_thread(_sweep_stale_staging_dirs, person_id)
            except Exception as exc:
                logger.warning(
                    "Garmin token cleanup for person %s did not complete (%s)", person_id, type(exc).__name__
                )
                raise GarminOperationError("unknown") from None
            return deleted
    except GarminRegistryError:
        raise
    except Exception as exc:
        logger.warning("Garmin unlink for person %s failed outside its bounded errors (%s)", person_id, type(exc).__name__)
        raise GarminOperationError("unknown") from None


async def _delete_link_row(person_id: int, actor_id: int, session_version: int) -> bool:
    """Atomically re-check the actor and delete the link; False if none existed."""
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        if not await actor_has_effective_manage(db, actor_id, session_version, person_id):
            await db.rollback()
            raise GarminSessionExpired()
        row = await (
            await db.execute("SELECT generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
        if row is None:
            await db.rollback()
            return False
        # No filesystem work inside BEGIN IMMEDIATE: unlink() removes the
        # whole person root once this has committed.
        await db.execute("DELETE FROM garmin_links WHERE person_id = ?", (person_id,))
        await db.commit()
        return True
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def call(
    person_id: int,
    op: Callable[[Garmin], _T | Awaitable[_T]],
    *,
    max_wait_seconds: float = 0.0,
) -> _T:
    """Run one complete Garmin operation for an explicitly selected person.

    After any wait for the stable per-person flock, the link is read again;
    unlink/re-link therefore invalidates a stale cache before it can issue a
    call. A cache miss performs two Garmin interactions (token-store login
    and ``op``), so it consumes two separately spaced permits; a cache hit
    consumes only the operation permit. ``op`` receives the exact client
    instead of an unlocked global singleton and can be synchronous (the
    normal garminconnect case) or async for small adapter tests.

    ``max_wait_seconds`` bounds how long the first permit reservation may
    sleep behind the deployment-wide interval.  The default ``0`` keeps the
    fail-fast behaviour interactive callers translate into a retry response;
    a small positive budget lets a user-facing push absorb the interval
    instead of failing on every other tap.  That sleep happens while holding
    the person flock, so same-person lifecycle routes and the scheduled sync
    wait behind it; the budget is therefore clamped to
    :data:`_MAX_INTERACTIVE_WAIT_SECONDS` whatever the caller asks for.
    """
    person_id = int(person_id)
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    max_wait_seconds = float(max_wait_seconds)
    if not max_wait_seconds >= 0.0:  # also rejects NaN
        raise ValueError("max_wait_seconds must be a non-negative number")
    max_wait_seconds = min(max_wait_seconds, _MAX_INTERACTIVE_WAIT_SECONDS)
    try:
        async with person_flock(person_id):
            generation, email = await _usable_link(person_id)
            await asyncio.to_thread(_sweep_unreferenced_generation_dirs, person_id, generation)
            garmin_client.forget_stale_generations(person_id, generation)
            # This must happen after the flock wait and durable link check:
            # otherwise a queued or unlinked request could consume a global
            # slot without being the next logical operation allowed to reach
            # Garmin.
            if max_wait_seconds > 0.0:
                await _wait_for_call_permit(deadline_seconds=max_wait_seconds)
            else:
                await reserve_call_permit()
            if garmin_client.is_authenticated(person_id, generation):
                client = garmin_client.get_client(person_id, generation)
            else:
                client = await _resume_link(person_id, generation, email)
            return await _run_operation(person_id, generation, client, op)
    except GarminRegistryError:
        raise
    except Exception as exc:
        # Token-root chmod/mkdir and flock setup can carry absolute paths in
        # their OS errors.  They cross the same public boundary as a Garmin
        # failure, so callers receive a bounded, path-free error.
        logger.warning("Garmin call for person %s failed outside its bounded errors (%s)", person_id, type(exc).__name__)
        raise GarminOperationError("unknown") from None


async def _usable_link(person_id: int) -> tuple[int, str]:
    """Read the durable link under the flock, evicting any cache for a dead one."""
    link = await _load_link(person_id)
    if link is None or link["state"] not in _VALID_LINK_STATES:
        garmin_client.forget(person_id)
        raise GarminNotLinked(person_id)
    email = link["garmin_email"]
    if not isinstance(email, str):  # Schema protects this; avoid an unsafe fallback if it drifts.
        garmin_client.forget(person_id)
        raise GarminNotLinked(person_id)
    return int(link["generation"]), email


async def _resume_link(person_id: int, generation: int, email: str) -> Garmin:
    """Cold-load a durable generation's token store and take its second permit."""
    token_dir = resolve_token_dir(person_id, generation)
    try:
        # Only the login moves off the event loop.  The operation itself runs
        # synchronously in _run_operation, so two Garmin writes for one
        # person cannot interleave inside one call() -- the write-race
        # reasoning in the weight and activity routes relies on that.
        client = await asyncio.to_thread(garmin_client.authenticate, person_id, generation, token_dir, email, None)
    except Exception as exc:
        code = _error_code(exc)
        garmin_client.forget(person_id, generation)
        await _record_auth_failure(person_id, generation, code)
        raise GarminAuthenticationError(code) from None
    await _record_auth_success(person_id, generation)
    # login(tokenstore=...) can issue a profile/network request, so it
    # consumed the first permit.  The following operation is a distinct
    # Garmin call and obtains a second permit while the same person lock
    # remains held.  It waits for that permit: rejecting the operation
    # immediately after a successful cold login would make one logical
    # request unable to complete at the configured global call rate.
    await _wait_for_call_permit()
    return client


async def _run_operation(
    person_id: int, generation: int, client: Garmin, op: Callable[[Garmin], _T | Awaitable[_T]]
) -> _T:
    try:
        result = op(client)
        if inspect.isawaitable(result):
            return await result
        return result
    except GarminRegistryError:
        raise
    except Exception as exc:
        code = _error_code(exc)
        if code == "auth_failed":
            # A credential rejection (a 401, or the library's own
            # authentication error) from a normal operation means this cached
            # session is no longer safe to reuse.  Persist only the bounded
            # code; the next operation will resume under this same lock
            # protocol.  A 403 is a transient block and is left alone.
            garmin_client.forget(person_id, generation)
            await _record_auth_failure(person_id, generation, code)
        raise GarminOperationError(code) from None


async def call_paced(person_id: int, op: Callable[[Garmin], _T | Awaitable[_T]]) -> _T:
    """Run a deliberate batch operation, waiting between globally limited calls.

    Dashboard sync makes several sequential Garmin reads by design.  This
    helper never skips or shares a permit: each attempt calls the ordinary
    :func:`call`, and only waits for its bounded retry interval when another
    call owns the global slot. Interactive calls remain fail-fast through
    :func:`call` so clients can receive their normal retry response.
    """
    while True:
        try:
            return await call(person_id, op)
        except GarminRateLimited as exc:
            await asyncio.sleep(exc.retry_after)
