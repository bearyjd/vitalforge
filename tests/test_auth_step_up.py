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
