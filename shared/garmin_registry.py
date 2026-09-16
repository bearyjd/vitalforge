"""The asynchronous, database-aware boundary for Garmin operations.

``shared.garmin_client`` intentionally knows nothing about SQLite or link
state.  Code that touches Garmin must instead call :func:`call`; that makes a
durable rate admission, a fresh link read, and a stable person lock one
operation rather than conventions individual routes can forget.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import shutil
import time  # noqa: F401 - public registry clock/monkeypatch seam
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from garminconnect import Garmin

from shared import garmin_client
from shared.database import get_db
from shared.garmin_registry_runtime import (
    _load_link,
    _record_auth_failure,
    _record_auth_success,
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
_VALID_LINK_STATES = frozenset({"linked", "legacy_bound"})
_ERROR_CODES = frozenset({"auth_failed", "rate_limited", "network", "unknown"})
_LINK_ATTEMPT_LIMIT = 3
_LINK_ATTEMPT_WINDOW_SECONDS = 15 * 60
_process_person_locks: dict[tuple[asyncio.AbstractEventLoop, int], asyncio.Lock] = {}


class GarminRegistryError(RuntimeError):
    """Base error whose text intentionally contains no third-party detail."""


class GarminNotLinked(GarminRegistryError):
    """The requested person has no usable durable Garmin link."""

    def __init__(self, person_id: int):
        self.person_id = person_id
        super().__init__("Garmin is not linked for this person")


class GarminRateLimited(GarminRegistryError):
    """The durable deployment-wide call budget has not yet refilled."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Garmin is temporarily rate limited")


class GarminLinkAttemptRateLimited(GarminRegistryError):
    """The authenticated user has exhausted their link-attempt window."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Too many Garmin link attempts")


class GarminAuthenticationError(GarminRegistryError):
    """A link could not resume its token store without exposing why."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "unknown"
        super().__init__("Garmin authentication failed")


class GarminOperationError(GarminRegistryError):
    """A Garmin operation failed without surfacing its raw exception text."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "unknown"
        super().__init__("Garmin operation failed")


class GarminLinkConflict(GarminRegistryError):
    """Another person already owns the requested canonical Garmin account."""

    def __init__(self):
        super().__init__("Garmin account is already linked")


class GarminSessionExpired(GarminRegistryError):
    """The actor's step-up session changed while a credential login ran."""

    def __init__(self):
        super().__init__("Your session is no longer current")


class GarminLinkInputError(GarminRegistryError):
    """Credential metadata could not be accepted without echoing it back."""

    def __init__(self):
        super().__init__("Garmin link details are invalid")


@dataclass(frozen=True)
class GarminLink:
    """Non-secret durable link metadata returned to future route handlers."""

    person_id: int
    generation: int
    state: str


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


def _person_lock_path(person_id: int) -> Path:
    """A stable lock survives a token-directory replacement on re-link."""
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    return _ensure_token_root() / f".person-{person_id}.lock"


def _acquire_lock(lock_path: Path):
    handle = open(lock_path, "a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        handle.close()
        raise
    return handle


def _process_person_lock(person_id: int) -> asyncio.Lock:
    """Return this process's companion lock for the durable flock.

    ``flock`` coordinates the two service processes, but its semantics do not
    reliably make two independently-opened descriptors in the same process
    wait for each other. The tiny process-local layer closes that gap while
    retaining flock as the crash-safe, cross-process authority.
    """
    # pytest-asyncio (and application reloads) can create a new event loop in
    # the same interpreter. asyncio.Lock is loop-bound once contended, so the
    # cache must be scoped to both loop and person rather than leaking a lock
    # from a completed loop into the next one.
    key = (asyncio.get_running_loop(), person_id)
    lock = _process_person_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _process_person_locks[key] = lock
    return lock


@asynccontextmanager
async def person_flock(person_id: int):
    """Serialize all operations for a person across both service processes.

    Lifecycle routes use the same public context manager, so unlink/re-link
    cannot swap a token directory while :func:`call` is authenticating or
    using it.  flock is released if a process dies; the file is intentionally
    retained as lock infrastructure, not a sentinel.
    """
    person_id = int(person_id)
    local_lock = _process_person_lock(person_id)
    async with local_lock:
        lock_path = _person_lock_path(person_id)
        acquire_task = asyncio.create_task(asyncio.to_thread(_acquire_lock, lock_path))
        try:
            handle = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            # shield() leaves the worker alive.  Do not await it here: a
            # second cancellation can interrupt that await and orphan the
            # descriptor after flock() eventually succeeds.  The completion
            # callback owns that late handle instead.
            _close_acquired_handle_when_done(acquire_task)
            raise
        try:
            yield
        finally:
            await _close_lock_handle(handle)


@asynccontextmanager
async def legacy_store_flock():
    """Serialize the one-time flat-store adoption across both services.

    The lock belongs beside the historic flat store, not inside any person's
    directory: before adoption there is deliberately no person-owned path.
    """
    local_lock = _process_person_lock(0)
    async with local_lock:
        lock_path = _ensure_token_root() / ".legacy-bootstrap.lock"
        acquire_task = asyncio.create_task(asyncio.to_thread(_acquire_lock, lock_path))
        try:
            handle = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            _close_acquired_handle_when_done(acquire_task)
            raise
        try:
            yield
        finally:
            await _close_lock_handle(handle)


def _close_acquired_handle_when_done(task: asyncio.Task) -> None:
    """Arrange cleanup for a cancelled flock acquisition without leaking it."""
    def close_handle(completed: asyncio.Task) -> None:
        if completed.cancelled():
            return
        try:
            handle = completed.result()
        except BaseException:
            return
        handle.close()

    task.add_done_callback(close_handle)


async def _close_lock_handle(handle) -> None:
    """Close a flock descriptor even if cleanup itself is cancelled twice."""
    close_task = asyncio.create_task(asyncio.to_thread(handle.close))
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        # The shielded worker still owns ``handle`` and will close it. Keep a
        # callback solely to consume any unexpected worker exception.
        close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        raise


def _error_code(exc: Exception) -> str:
    """Classify without logging or returning third-party exception content."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    status = status if status is not None else getattr(response, "status_code", None)
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth_failed"
    name = type(exc).__name__.lower()
    if "auth" in name or "credential" in name or "login" in name:
        return "auth_failed"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or "network" in name or "timeout" in name:
        return "network"
    return "unknown"


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


async def forget_and_remove_person_token_store(person_id: int) -> None:
    """Evict a person's cached clients and remove its owned token directories.

    The caller must already hold :func:`person_flock`.  Keeping the cache
    eviction before the filesystem operation means a cleanup failure can leave
    an inert artifact but never a usable in-process credential.  This helper
    intentionally does not touch the flat legacy store: a ``legacy_disabled``
    tombstone is what prevents that historic shared store from being adopted
    again.
    """
    person_id = int(person_id)
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    garmin_client.forget(person_id)
    await asyncio.to_thread(_remove_person_token_root, person_id)


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


async def _next_generation(person_id: int) -> int:
    db = await get_db()
    try:
        ledger = await (
            await db.execute(
                "SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
        link = await (
            await db.execute("SELECT generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    return max(ledger["generation"] if ledger is not None else 0, link["generation"] if link is not None else 0) + 1


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
        durable_generation = max(
            ledger["generation"] if ledger is not None else 0,
            current["generation"] if current is not None else 0,
        ) + 1
        # The person flock makes this stable across normal lifecycle callers.
        # Refuse, rather than attach the staged client to a surprise generation,
        # if a direct DB writer changed it while credential login was running.
        if durable_generation != generation:
            await db.rollback()
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
            existing = await _load_link(person_id)
            await asyncio.to_thread(
                _sweep_unreferenced_generation_dirs,
                person_id,
                int(existing["generation"]) if existing is not None else None,
            )
            generation = await _next_generation(person_id)
            # This durable user-scoped throttle must succeed before any
            # credential reaches Garmin.  It remains inside the flock so a
            # re-link cannot race this person's generation/token swap.
            await reserve_link_attempt(actor_id)
            await _validate_link_target(person_id, actor_id, session_version)
            target = _generation_token_dir(person_id, generation)
            # If a process died after installing the staged directory but
            # before the database publication, this next generation is not
            # reachable from durable metadata.  Discard it before retrying;
            # otherwise a crash would permanently block this person's next
            # link attempt on an already-existing target directory.
            if target.exists():
                await asyncio.to_thread(_remove_token_dir, target)
            staging = _staging_token_dir(person_id, generation)
            try:
                try:
                    await asyncio.to_thread(
                        garmin_client.authenticate,
                        person_id,
                        generation,
                        staging,
                        canonical_email,
                        password,
                    )
                except Exception as exc:
                    code = _error_code(exc)
                    garmin_client.forget(person_id, generation)
                    if existing is not None:
                        await _record_auth_failure(person_id, int(existing["generation"]), code)
                    raise GarminAuthenticationError(code) from None

                try:
                    await asyncio.to_thread(_install_staged_token_dir, staging, target)
                except Exception:
                    garmin_client.forget(person_id, generation)
                    raise GarminOperationError("unknown") from None
                try:
                    published = await _publish_link(
                        person_id, actor_id, session_version, canonical_email, generation
                    )
                except GarminRegistryError:
                    garmin_client.forget(person_id, generation)
                    try:
                        await asyncio.to_thread(_remove_token_dir, target)
                    except Exception:
                        pass
                    raise
                garmin_client.forget_stale_generations(person_id, generation)
                if existing is not None and existing["state"] == "linked":
                    try:
                        await asyncio.to_thread(
                            _remove_token_dir,
                            _generation_token_dir(person_id, int(existing["generation"])),
                        )
                    except Exception:
                        raise GarminOperationError("unknown") from None
                return published
            finally:
                if staging.exists():
                    try:
                        await asyncio.to_thread(_remove_token_dir, staging)
                    except Exception:
                        pass
    except GarminRegistryError:
        raise
    except Exception:
        raise GarminOperationError("unknown") from None


async def relink(
    person_id: int, actor_id: int, session_version: int, email: str, password: str
) -> GarminLink:
    """Explicit alias for route handlers that distinguish first-link wording."""
    return await link(person_id, actor_id, session_version, email, password)


async def unlink(person_id: int, actor_id: int, session_version: int) -> bool:
    """Remove a normal link or durably tombstone a legacy-bound one.

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
            db = await get_db(isolation_level=None)
            try:
                await db.execute("BEGIN IMMEDIATE")
                if not await actor_has_effective_manage(db, actor_id, session_version, person_id):
                    await db.rollback()
                    raise GarminSessionExpired()
                row = await (
                    await db.execute("SELECT state, generation FROM garmin_links WHERE person_id = ?", (person_id,))
                ).fetchone()
                if row is None:
                    await db.rollback()
                    return False
                await asyncio.to_thread(_sweep_unreferenced_generation_dirs, person_id, int(row["generation"]))
                if row["state"] == "legacy_bound":
                    await db.execute(
                        """
                        UPDATE garmin_links
                        SET state = 'legacy_disabled', garmin_email = NULL,
                            linked_at = NULL, linked_by = NULL, updated_at = ?,
                            last_auth_ok = NULL, last_auth_error = NULL,
                            last_auth_error_at = NULL
                        WHERE person_id = ?
                        """,
                        (_utc_now(), person_id),
                    )
                else:
                    await db.execute("DELETE FROM garmin_links WHERE person_id = ?", (person_id,))
                await db.commit()
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise
            finally:
                await db.close()

            garmin_client.forget(person_id)
            try:
                await asyncio.to_thread(_remove_person_token_root, person_id)
            except Exception:
                raise GarminOperationError("unknown") from None
            return True
    except GarminRegistryError:
        raise
    except Exception:
        raise GarminOperationError("unknown") from None


async def call(person_id: int, op: Callable[[Garmin], _T | Awaitable[_T]]) -> _T:
    """Run one complete Garmin operation for an explicitly selected person.

    After any wait for the stable per-person flock, the link is read again;
    unlink/re-link therefore invalidates a stale cache before it can issue a
    call. A cache miss performs two Garmin interactions (token-store login
    and ``op``), so it consumes two separately spaced permits; a cache hit
    consumes only the operation permit. ``op`` receives the exact client
    instead of an unlocked global singleton and can be synchronous (the
    normal garminconnect case) or async for small adapter tests.
    """
    person_id = int(person_id)
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    try:
        async with person_flock(person_id):
            link = await _load_link(person_id)
            if link is None or link["state"] not in _VALID_LINK_STATES:
                garmin_client.forget(person_id)
                raise GarminNotLinked(person_id)
            generation = int(link["generation"])
            email = link["garmin_email"]
            await asyncio.to_thread(_sweep_unreferenced_generation_dirs, person_id, generation)
            if not isinstance(email, str):  # Schema protects this; avoid an unsafe fallback if it drifts.
                garmin_client.forget(person_id)
                raise GarminNotLinked(person_id)

            garmin_client.forget_stale_generations(person_id, generation)
            # This must happen after the flock wait and durable link check:
            # otherwise a queued or unlinked request could consume a global
            # slot without being the next logical operation allowed to reach
            # Garmin.
            await reserve_call_permit()
            if garmin_client.is_authenticated(person_id, generation):
                client = garmin_client.get_client(person_id, generation)
            else:
                token_dir = await resolve_token_dir(person_id, link["state"], generation)
                try:
                    # Login alone moves off the event loop. Garmin operations
                    # stay synchronous here because existing write-race
                    # reasoning relies on that behavior; this change does not
                    # widen it.
                    client = await asyncio.to_thread(
                        garmin_client.authenticate,
                        person_id,
                        generation,
                        token_dir,
                        email,
                        None,
                    )
                except Exception as exc:
                    code = _error_code(exc)
                    garmin_client.forget(person_id, generation)
                    await _record_auth_failure(person_id, generation, code)
                    raise GarminAuthenticationError(code) from None
                await _record_auth_success(person_id, generation)
                # login(tokenstore=...) can issue a profile/network request,
                # so it consumed the first permit.  The following operation
                # is a distinct Garmin call and obtains a second permit while
                # the same person lock remains held.  It waits for that
                # permit: rejecting the operation immediately after a
                # successful cold login would make one logical request unable
                # to complete at the configured global call rate.
                await _wait_for_call_permit()

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
                    # A 401/403 from a normal operation means this cached
                    # session is no longer safe to reuse.  Persist only the
                    # bounded code; the next operation will resume under this
                    # same lock protocol.
                    garmin_client.forget(person_id, generation)
                    await _record_auth_failure(person_id, generation, code)
                raise GarminOperationError(code) from None
    except GarminRegistryError:
        raise
    except Exception:
        # Token-root chmod/mkdir and flock setup can carry absolute paths in
        # their OS errors.  They cross the same public boundary as a Garmin
        # failure, so callers receive a bounded, path-free error.
        raise GarminOperationError("unknown") from None


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
