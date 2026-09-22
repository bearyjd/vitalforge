"""Cross-process and in-process serialization for the Garmin registry.

``flock`` is the crash-safe authority shared by both service processes; the
per-loop ``asyncio.Lock`` layer only closes the gap that two descriptors in
one process do not reliably wait for each other.  This module is a leaf that
knows nothing about where lock files live: :func:`flock_scope` takes its lock
path from the caller, and :mod:`shared.garmin_registry` builds
``person_flock`` / ``legacy_store_flock`` on it with paths under its own
``GARTH_TOKEN_DIR``.
"""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

# A process-local lock key names the lock's kind and identity, so callers that
# share ``_process_local_locks`` cannot collide: the legacy-store key is a
# one-tuple and every person key carries its id.
LockKey = tuple[str] | tuple[str, int]
LEGACY_STORE_LOCK_KEY: LockKey = ("legacy-store",)

_process_local_locks: dict[tuple[asyncio.AbstractEventLoop, LockKey], asyncio.Lock] = {}


def person_lock_key(person_id: int) -> LockKey:
    return ("person", person_id)


def _acquire_lock(lock_path: Path):
    handle = open(lock_path, "a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        handle.close()
        raise
    return handle


def _process_local_lock(local_key: LockKey) -> asyncio.Lock:
    """Return this process's companion lock for the durable flock.

    ``flock`` coordinates the two service processes, but its semantics do not
    reliably make two independently-opened descriptors in the same process
    wait for each other. The tiny process-local layer closes that gap while
    retaining flock as the crash-safe, cross-process authority.
    """
    # pytest-asyncio (and application reloads) can create a new event loop in
    # the same interpreter. asyncio.Lock is loop-bound once contended, so the
    # cache must be scoped to both loop and key rather than leaking a lock
    # from a completed loop into the next one.
    key = (asyncio.get_running_loop(), local_key)
    lock = _process_local_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _process_local_locks[key] = lock
    return lock


@asynccontextmanager
async def flock_scope(local_key: LockKey, lock_path: Callable[[], Path]) -> AsyncIterator[None]:
    """Hold this process's lock for ``local_key``, then flock ``lock_path()``.

    The process-local lock comes first because flock alone does not make two
    descriptors in one process wait for each other (see
    :func:`_process_local_lock`); ``lock_path`` is only called once it is
    held, so a path that has to prepare its directory does so serialized per
    key.  flock is released if a process dies; the file is intentionally
    retained as lock infrastructure, not a sentinel.

    ``local_key`` is structural: :data:`LEGACY_STORE_LOCK_KEY` for the
    flat-store adoption and :func:`person_lock_key` for a person share
    ``_process_local_locks`` without any way to collide, since a legacy key
    can never equal a person key.  A new caller gets its own kind.  Keys and
    lock files must still pair one-to-one: a distinct key on a colliding lock
    file would lose the in-process half of the exclusion.
    """
    local_lock = _process_local_lock(local_key)
    async with local_lock:
        path = lock_path()
        acquire_task = asyncio.create_task(asyncio.to_thread(_acquire_lock, path))
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
