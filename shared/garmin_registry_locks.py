"""Cross-process and in-process serialization for the Garmin registry.

``flock`` is the crash-safe authority shared by both service processes; the
per-loop ``asyncio.Lock`` layer only closes the gap that two descriptors in
one process do not reliably wait for each other.  The public entry points are
re-exported by :mod:`shared.garmin_registry`.
"""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager
from pathlib import Path

_process_person_locks: dict[tuple[asyncio.AbstractEventLoop, int], asyncio.Lock] = {}


def _registry():
    # Delayed to avoid a facade/import cycle and to honour the facade's
    # GARTH_TOKEN_DIR monkeypatch seam.
    from shared import garmin_registry

    return garmin_registry


def _person_lock_path(person_id: int) -> Path:
    """A stable lock survives a token-directory replacement on re-link."""
    if person_id < 1:
        raise ValueError("person_id must be a positive integer")
    return _registry()._ensure_token_root() / f".person-{person_id}.lock"


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
        lock_path = _registry()._ensure_token_root() / ".legacy-bootstrap.lock"
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
