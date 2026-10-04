"""The process-local lock key namespace, pinned.

`flock_scope` keys its in-process lock on a tuple the caller supplies. The two
callers must never share a key: `person_flock(0)` entered while the same task
holds `legacy_store_flock()` used to wait forever on the legacy store's own
non-reentrant `asyncio.Lock` (key `0` was both), and only the `< 1` guard
inside the lock-path lambda -- reached after the key was taken -- stood
between them. These tests hold the key contract and the refuse-before-locking
order directly, so a future caller that picks a colliding key or moves the
guard fails here rather than hanging a boot.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import garmin_registry, garmin_registry_locks


def _keys_for(loop: asyncio.AbstractEventLoop) -> set:
    return {key for (owner, key) in garmin_registry_locks._process_local_locks if owner is loop}


@pytest.mark.parametrize("person_id", [0, -1])
async def test_person_flock_refuses_a_non_positive_id_before_taking_any_lock(tmp_db_path, person_id):
    before = dict(garmin_registry_locks._process_local_locks)
    with pytest.raises(ValueError):
        garmin_registry.person_flock(person_id)
    assert garmin_registry_locks._process_local_locks == before, "a refused id must not create a lock entry"


async def test_person_flock_zero_inside_the_legacy_store_flock_raises_instead_of_hanging(tmp_db_path):
    """The Red Team scenario, pinned from both sides: the legacy key and
    person 0 are different locks (so this cannot wait on the lock the task
    already holds), and the refusal comes before `flock_scope` runs at all --
    with the guard back inside `lock_path()`, `flock_scope` would create the
    `("person", 0)` entry first and only then raise."""
    loop = asyncio.get_running_loop()
    async with garmin_registry.legacy_store_flock():
        async with asyncio.timeout(2):
            with pytest.raises(ValueError):
                async with garmin_registry.person_flock(0):
                    pytest.fail("must not enter")
    assert (loop, garmin_registry_locks.person_lock_key(0)) not in garmin_registry_locks._process_local_locks, (
        "the refusal must happen before flock_scope takes (and so creates) a process-local lock"
    )


async def test_legacy_store_and_person_flocks_nest_on_distinct_keys(tmp_db_path):
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(2):
        async with garmin_registry.legacy_store_flock():
            async with garmin_registry.person_flock(1):
                keys = _keys_for(loop)
    assert garmin_registry_locks.LEGACY_STORE_LOCK_KEY in keys
    assert garmin_registry_locks.person_lock_key(1) in keys


def test_lock_keys_are_distinct_per_person_and_from_the_legacy_store():
    """Distinct per person, and the legacy key is its own KIND -- not a person
    key with a special id, which is exactly the collision the tuples exist to
    rule out."""
    assert garmin_registry_locks.person_lock_key(1) != garmin_registry_locks.person_lock_key(2)
    assert garmin_registry_locks.person_lock_key(1) != garmin_registry_locks.LEGACY_STORE_LOCK_KEY
    assert garmin_registry_locks.person_lock_key(2) != garmin_registry_locks.LEGACY_STORE_LOCK_KEY
    assert garmin_registry_locks.LEGACY_STORE_LOCK_KEY[0] != garmin_registry_locks.person_lock_key(1)[0]
    assert garmin_registry_locks.person_lock_key(0) != garmin_registry_locks.LEGACY_STORE_LOCK_KEY


async def test_flock_scope_releases_the_local_lock_when_the_lock_path_raises(tmp_db_path, monkeypatch):
    """`lock_path()` runs under the process-local lock; if it raises, that
    lock must be released on the way out or the next entrant for the same
    key waits forever in-process. The seam is pinned as having fired: with a
    schema-less database, `call()` would fail with the same bounded error
    even if the patched lock path were never consulted."""
    fired: list[int] = []

    def cannot_prepare_lock(person_id):
        fired.append(person_id)
        raise OSError("lock path unavailable")

    # Scoped so the seam alone is undone; the fixture's GARTH_TOKEN_DIR patch stays.
    with monkeypatch.context() as scoped:
        scoped.setattr(garmin_registry, "_person_lock_path", cannot_prepare_lock)
        with pytest.raises(garmin_registry.GarminOperationError):
            await garmin_registry.call(1, lambda _client: pytest.fail("must not run"))
        assert fired == [1], "the patched lock path was never consulted; the failure came from somewhere else"
        # _acquire_lock is the only thing that creates this file: its absence proves
        # lock_path() raised before the flock was opened.
        assert not (garmin_registry.GARTH_TOKEN_DIR / ".person-1.lock").exists()

    async with asyncio.timeout(2):
        async with garmin_registry.person_flock(1):
            pass


async def test_flock_scope_prepares_its_lock_path_off_the_event_loop(tmp_path):
    """`lock_path()` may mkdir/chmod the token root; that runs on a worker
    thread, still under the process-local lock."""
    loop_thread = threading.get_ident()
    local_lock_held: list[bool] = []
    ran_on: list[int] = []
    key = garmin_registry_locks.person_lock_key(99)
    local_lock = garmin_registry_locks._process_local_lock(key)

    def lock_path():
        ran_on.append(threading.get_ident())
        local_lock_held.append(local_lock.locked())
        return tmp_path / "probe.lock"

    async with asyncio.timeout(2):
        async with garmin_registry_locks.flock_scope(key, lock_path):
            pass
    assert ran_on and ran_on[0] != loop_thread, "lock_path() ran on the event loop thread"
    assert local_lock_held == [True], "lock_path() must still run under the process-local lock"


async def test_flock_scope_acquires_and_releases_when_default_executor_is_busy(tmp_path):
    """A default-executor starvation must not pin a held flock (#95).

    The one available default worker is deliberately blocked.  Entering and
    leaving the scope still completes because path preparation/acquisition use
    the dedicated bounded flock executor and descriptor close is local.
    """
    loop = asyncio.get_running_loop()
    prior_default_executor = loop._default_executor
    default_executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(default_executor)
    blocker_started = asyncio.Event()
    unblock_default_executor = threading.Event()

    def occupy_default_executor():
        loop.call_soon_threadsafe(blocker_started.set)
        unblock_default_executor.wait()

    blocker = loop.run_in_executor(None, occupy_default_executor)
    try:
        await asyncio.wait_for(blocker_started.wait(), timeout=2)
        key = garmin_registry_locks.person_lock_key(100)
        async with asyncio.timeout(2):
            async with garmin_registry_locks.flock_scope(key, lambda: tmp_path / "probe.lock"):
                pass
    finally:
        unblock_default_executor.set()
        await blocker
        loop._default_executor = prior_default_executor
        default_executor.shutdown(wait=True)


async def test_cancelling_after_flock_worker_dispatch_closes_the_late_handle(monkeypatch):
    """Cancellation after dispatch cannot leak a handle acquired later (#95)."""
    loop = asyncio.get_running_loop()
    dispatched_worker = loop.create_future()
    acquisition_dispatched = asyncio.Event()
    release_calls: list[object] = []

    class FakeHandle:
        def close(self):
            release_calls.append(self)

    original_run_in_executor = loop.run_in_executor

    acquisition_submissions = 0

    def run_executor_inline(executor, callback, *args):
        nonlocal acquisition_submissions
        if executor is garmin_registry_locks._FLOCK_EXECUTOR:
            acquisition_submissions += 1
            if acquisition_submissions == 1:
                completed = loop.create_future()
                completed.set_result(callback(*args))
                return completed
            assert acquisition_submissions == 2, "flock_scope submitted more than path preparation and acquisition"
            acquisition_dispatched.set()
            return dispatched_worker
        if executor is garmin_registry_locks._FLOCK_RELEASE_EXECUTOR:
            callback(*args)
        else:
            return original_run_in_executor(executor, callback, *args)
        completed = loop.create_future()
        completed.set_result(None)
        return completed

    monkeypatch.setattr(loop, "run_in_executor", run_executor_inline)

    async def wait_for_flock():
        async with garmin_registry_locks.flock_scope(
            garmin_registry_locks.person_lock_key(101), lambda: object()
        ):
            pytest.fail("a cancelled acquisition must not enter the flock")

    waiter = asyncio.create_task(wait_for_flock())
    await asyncio.wait_for(acquisition_dispatched.wait(), timeout=2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    handle = FakeHandle()
    dispatched_worker.set_result(handle)
    await asyncio.sleep(0)
    assert acquisition_submissions == 2
    assert release_calls == [handle]
