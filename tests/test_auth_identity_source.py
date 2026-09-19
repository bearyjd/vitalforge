"""Credential-source and step-up invariants for sensitive browser flows."""

import pytest
from fastapi import HTTPException

from shared import auth as shared_auth
from shared.auth import (
    _get_current_identity,
    _require_step_up,
    create_session_cookie,
    require_cookie_session_identity,
)
from shared.database import get_db
from tests.conftest import seed_token, seed_user
from tests.test_auth_token import make_request


def _cookie_request(cookie: str):
    return make_request([("cookie", f"vf_session={cookie}")])


async def test_cookie_session_identity_is_classified_and_accepted(initialized_db):
    user_id = await seed_user("alice")
    request = _cookie_request(create_session_cookie("alice", user_id, 1))

    identity = await require_cookie_session_identity(request)

    assert identity.username == "alice"
    assert identity.user_id == user_id
    assert identity.source == "cookie"


async def test_cookie_session_dependency_returns_401_when_no_identity(initialized_db):
    await seed_user("alice")

    with pytest.raises(HTTPException, match="Not authenticated") as exc_info:
        await require_cookie_session_identity(make_request())

    assert exc_info.value.status_code == 401


async def test_cookie_session_dependency_hides_person_from_bearer_identity(initialized_db):
    user_id = await seed_user("alice")
    await seed_token(user_id, raw_token="alice-token")
    request = make_request([("authorization", "Bearer alice-token")])

    with pytest.raises(HTTPException, match="Person not found") as exc_info:
        await require_cookie_session_identity(request)

    assert exc_info.value.status_code == 404


async def test_valid_bearer_takes_precedence_over_cookie_for_cookie_only_flow(initialized_db):
    user_id = await seed_user("alice")
    await seed_token(user_id, raw_token="alice-token")
    cookie = create_session_cookie("alice", user_id, 1)
    request = make_request(
        [("authorization", "Bearer alice-token"), ("cookie", f"vf_session={cookie}")]
    )

    identity = await _get_current_identity(request)
    assert identity is not None
    assert identity.source == "bearer"

    with pytest.raises(HTTPException) as exc_info:
        await require_cookie_session_identity(request)
    assert exc_info.value.status_code == 404


async def test_anonymous_development_identity_cannot_use_cookie_only_flow(initialized_db):
    with pytest.raises(HTTPException, match="Person not found") as exc_info:
        await require_cookie_session_identity(make_request())

    assert exc_info.value.status_code == 404


async def test_step_up_rejects_session_version_changed_after_identity_resolution(initialized_db):
    user_id = await seed_user("alice", password="correct-password")
    identity = await _get_current_identity(
        _cookie_request(create_session_cookie("alice", user_id, 1))
    )
    assert identity is not None

    db = await get_db()
    try:
        await db.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,))
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(HTTPException, match="Current password incorrect") as exc_info:
        await _require_step_up(identity, "correct-password")

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("verified", [(11, 3), (10, 4)])
async def test_step_up_requires_the_exact_account_and_session_version(monkeypatch, verified):
    identity = shared_auth.Identity("alice", 10, 3, "user", "cookie")

    async def wrong_session_version(username: str, password: str):
        assert (username, password) == ("alice", "correct-password")
        return verified

    monkeypatch.setattr(shared_auth, "_authenticate_credentials", wrong_session_version)

    with pytest.raises(HTTPException, match="Current password incorrect") as exc_info:
        await _require_step_up(identity, "correct-password")

    assert exc_info.value.status_code == 401


async def test_unknown_identity_source_fails_closed_for_cookie_only_flow(monkeypatch):
    async def unclassified_identity(request):
        return shared_auth.Identity("alice", 10, 3, "user", "unclassified")

    monkeypatch.setattr(shared_auth, "_get_current_identity", unclassified_identity)

    with pytest.raises(HTTPException, match="Person not found") as exc_info:
        await require_cookie_session_identity(make_request())

    assert exc_info.value.status_code == 404


async def test_credentials_reject_password_reset_after_initial_read(monkeypatch):
    """A reset after the credential read cannot authenticate its old password.

    The two fake connections model the exact interleaving: the first lookup
    observes the old row, scrypt verifies that old hash, and a reset commits
    before the helper's required current-row recheck.
    """

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Db:
        def __init__(self, row):
            self.row = row

        async def execute(self, query, parameters):
            return Cursor(self.row)

        async def close(self):
            pass

    old_row = {"id": 10, "password_hash": "old-password-hash", "session_version": 3}
    reset_row = {"id": 10, "password_hash": "new-password-hash", "session_version": 4}
    connections = iter((Db(old_row), Db(reset_row)))

    async def get_db_after_reset():
        return next(connections)

    monkeypatch.setattr(shared_auth, "get_db", get_db_after_reset)
    monkeypatch.setattr(shared_auth, "_verify_password", lambda password, stored_hash: True)

    assert await shared_auth._authenticate_credentials("alice", "old-password") is None
