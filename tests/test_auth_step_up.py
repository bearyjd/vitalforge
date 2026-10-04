import asyncio

import pytest
from fastapi import HTTPException

from shared import auth
from tests.conftest import seed_user

# The step-up failure window is cleared around every test by the autouse
# `_reset_step_up_failures` fixture in tests/conftest.py; no per-file
# fixture needed here.


async def test_sixth_failed_step_up_in_a_window_is_throttled_before_scrypt(initialized_db, monkeypatch):
    user_id = await seed_user("owner", "correct horse")
    identity = auth._Identity("owner", user_id, 1, "admin", "cookie")
    now = 10_000.0
    monkeypatch.setattr(auth.time, "time", lambda: now)
    scrypt_calls = []
    real = auth._authenticate_credentials

    async def counting(username, password):
        scrypt_calls.append(username)
        return await real(username, password)

    monkeypatch.setattr(auth, "_authenticate_credentials", counting)

    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            await auth._require_step_up(identity, "wrong")
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await auth._require_step_up(identity, "correct horse")
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"].isdigit()
    assert len(scrypt_calls) == 5, "the throttled attempt must not reach scrypt"

    now += 15 * 60 + 1
    await auth._require_step_up(identity, "correct horse")  # window expired: success, and it clears the entry
    assert user_id not in auth._step_up_failures


async def test_concurrent_step_ups_reserve_the_failure_limit_before_verifying(initialized_db, monkeypatch):
    user_id = await seed_user("owner", "correct horse")
    identity = auth._Identity("owner", user_id, 1, "admin", "cookie")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_failure(username, password):
        nonlocal calls
        calls += 1
        if calls == auth._STEP_UP_FAILURE_LIMIT:
            entered.set()
        await release.wait()
        return None

    monkeypatch.setattr(auth, "_authenticate_credentials", blocked_failure)

    attempts = [asyncio.create_task(auth._require_step_up(identity, "wrong")) for _ in range(6)]
    await entered.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*attempts, return_exceptions=True)

    statuses = [result.status_code for result in results if isinstance(result, HTTPException)]
    assert calls == auth._STEP_UP_FAILURE_LIMIT
    assert statuses.count(401) == auth._STEP_UP_FAILURE_LIMIT
    assert statuses.count(429) == 1
    assert len(auth._step_up_failures[user_id]) == auth._STEP_UP_FAILURE_LIMIT
    assert user_id not in auth._step_up_pending


async def test_step_up_releases_a_reservation_when_credential_check_errors(initialized_db, monkeypatch):
    user_id = await seed_user("owner", "correct horse")
    identity = auth._Identity("owner", user_id, 1, "admin", "cookie")

    async def unavailable(username, password):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(auth, "_authenticate_credentials", unavailable)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await auth._require_step_up(identity, "correct horse")
    assert user_id not in auth._step_up_pending


async def test_cancelled_step_up_releases_its_reservation_for_a_later_attempt(initialized_db, monkeypatch):
    user_id = await seed_user("owner", "correct horse")
    identity = auth._Identity("owner", user_id, 1, "admin", "cookie")
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_verification(username, password):
        started.set()
        await never.wait()
        return None

    monkeypatch.setattr(auth, "_authenticate_credentials", blocked_verification)
    attempt = asyncio.create_task(auth._require_step_up(identity, "correct horse"))
    await started.wait()
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert user_id not in auth._step_up_pending

    calls = 0

    async def failed_verification(username, password):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(auth, "_authenticate_credentials", failed_verification)
    with pytest.raises(HTTPException) as exc:
        await auth._require_step_up(identity, "wrong")
    assert exc.value.status_code == 401
    assert calls == 1


def test_pruning_every_expired_failure_drops_the_user_entry():
    """Only a successful step-up used to pop the key, so a user who failed
    and never came back kept an empty deque forever."""
    now = 10_000.0
    auth._step_up_failures[7] = auth.deque([now - auth._STEP_UP_WINDOW_SECONDS - 1, now - auth._STEP_UP_WINDOW_SECONDS])

    assert auth._step_up_retry_after(7, now) is None
    assert 7 not in auth._step_up_failures


def test_pruning_keeps_the_entry_while_a_failure_is_still_in_the_window():
    now = 10_000.0
    auth._step_up_failures[7] = auth.deque([now - auth._STEP_UP_WINDOW_SECONDS - 1, now - 1])

    assert auth._step_up_retry_after(7, now) is None
    assert list(auth._step_up_failures[7]) == [now - 1]
