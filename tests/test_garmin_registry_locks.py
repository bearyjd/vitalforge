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
    """The Red Team scenario: the legacy key and person 0 must be different
    locks, and the refusal must come before any lock is taken."""
    async with garmin_registry.legacy_store_flock():
        async with asyncio.timeout(2):
            with pytest.raises(ValueError):
                async with garmin_registry.person_flock(0):
                    pytest.fail("must not enter")


async def test_legacy_store_and_person_flocks_nest_on_distinct_keys(tmp_db_path):
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(2):
        async with garmin_registry.legacy_store_flock():
            async with garmin_registry.person_flock(1):
                keys = _keys_for(loop)
    assert garmin_registry_locks.LEGACY_STORE_LOCK_KEY in keys
    assert ("person", 1) in keys


def test_lock_keys_are_distinct_per_person_and_from_the_legacy_store():
    assert garmin_registry_locks.person_lock_key(1) != garmin_registry_locks.person_lock_key(2)
    assert garmin_registry_locks.person_lock_key(1) != garmin_registry_locks.LEGACY_STORE_LOCK_KEY
    assert garmin_registry_locks.person_lock_key(2) != garmin_registry_locks.LEGACY_STORE_LOCK_KEY
