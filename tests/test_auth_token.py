"""Unit tests for DB-backed bearer resolution and password helpers."""

import logging

import pytest
from starlette.requests import Request

from shared import auth as shared_auth
from shared.auth import _hash_password, _resolve_bearer_token, _verify_password, check_credentials
from tests.conftest import seed_token, seed_user


def make_request(headers: list[tuple[str, str]] | None = None) -> Request:
    """A minimal Request built from a hand-written ASGI scope — no app, no
    HTTP round-trip, just enough for `.headers`/`.cookies` to work."""
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])]
    return Request({"type": "http", "headers": raw_headers})


def bearer_request(value: str) -> Request:
    return make_request([("authorization", f"Bearer {value}")])


async def _seed_bearer_owner(raw_token: str = "correct-token", role: str = "user"):
    user_id = await seed_user("token-owner", role=role)
    await seed_token(user_id, raw_token=raw_token)
    return user_id


async def test_bearer_valid_token_resolves_owner(initialized_db):
    user_id = await _seed_bearer_owner()
    identity = await _resolve_bearer_token(bearer_request("correct-token"))
    assert identity is not None
    assert identity.username == "token-owner"
    assert identity.user_id == user_id
    assert identity.role == "user"


async def test_bearer_wrong_token_rejected(initialized_db):
    await _seed_bearer_owner()
    assert await _resolve_bearer_token(bearer_request("wrong-token")) is None


async def test_bearer_empty_value_rejected(initialized_db):
    assert await _resolve_bearer_token(make_request([("authorization", "Bearer ")])) is None


async def test_bearer_whitespace_only_value_rejected(initialized_db):
    assert await _resolve_bearer_token(make_request([("authorization", "Bearer    ")])) is None


async def test_bearer_rejected_when_no_token_rows_exist(initialized_db):
    await seed_user("token-owner")
    assert await _resolve_bearer_token(bearer_request("correct-token")) is None


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
async def test_bearer_scheme_case_insensitive(initialized_db, scheme):
    await _seed_bearer_owner()
    identity = await _resolve_bearer_token(make_request([("authorization", f"{scheme} correct-token")]))
    assert identity is not None


@pytest.mark.parametrize(
    "header_value",
    ["Basic correct-token", "correct-token"],
)
async def test_bearer_wrong_scheme_rejected(initialized_db, header_value):
    await _seed_bearer_owner()
    assert await _resolve_bearer_token(make_request([("authorization", header_value)])) is None


async def test_bearer_non_ascii_token_returns_none_not_typeerror(initialized_db):
    await _seed_bearer_owner()
    # No exception is the assertion; a wrong result would also be a failure
    # since it's compared against a real token that can't match.
    assert await _resolve_bearer_token(bearer_request("tökén")) is None


async def test_bearer_surrounding_whitespace_stripped(initialized_db):
    await _seed_bearer_owner()
    identity = await _resolve_bearer_token(make_request([("authorization", "Bearer   correct-token   ")]))
    assert identity is not None


def test_hash_password_round_trips_via_verify():
    stored = _hash_password("a-real-password")
    assert _verify_password("a-real-password", stored) is True
    assert _verify_password("wrong-password", stored) is False


def test_hash_password_uses_a_different_salt_each_call():
    a = _hash_password("same-password")
    b = _hash_password("same-password")
    assert a != b  # different salt -> different stored value, even for the same password


def test_verify_password_malformed_stored_hash_returns_false_not_raise():
    assert _verify_password("anything", "not-the-right-format-at-all") is False


async def test_check_credentials_non_ascii_password_returns_false_not_typeerror(initialized_db):
    await seed_user("admin", password="correct-pass")
    assert await check_credentials("admin", "tökén") is False


async def test_check_credentials_valid_pair_accepted(initialized_db):
    await seed_user("admin", password="correct-pass")
    assert await check_credentials("admin", "correct-pass") is True


async def test_check_credentials_rejects_wrong_user_and_wrong_pass(initialized_db):
    await seed_user("admin", password="correct-pass")
    assert await check_credentials("wrong-user", "correct-pass") is False
    assert await check_credentials("admin", "wrong-pass") is False


async def test_check_credentials_unknown_username_returns_false(initialized_db):
    assert await check_credentials("nobody", "anything") is False


async def test_check_credentials_pays_the_same_scrypt_cost_for_unknown_usernames(initialized_db, monkeypatch):
    """Fix-review finding (MEDIUM, username-enumeration oracle): an unknown
    username used to return False immediately, skipping the scrypt cost a
    real check pays -- a measurable, exploitable timing signal (reviewer
    measured ~27ms vs ~0.8ms). Asserting on wall-clock time would be flaky
    in CI; asserting hashlib.scrypt was actually invoked either way is the
    deterministic version of the same check."""
    import hashlib as hashlib_module

    calls = []
    real_scrypt = hashlib_module.scrypt

    def counting_scrypt(*args, **kwargs):
        calls.append(1)
        return real_scrypt(*args, **kwargs)

    monkeypatch.setattr(hashlib_module, "scrypt", counting_scrypt)

    await check_credentials("definitely-does-not-exist", "whatever")
    assert len(calls) == 1


def test_resolve_secret_passes_through_configured_value():
    assert shared_auth._resolve_secret("a-real-secret-value") == "a-real-secret-value"


@pytest.mark.parametrize(
    "insecure_value",
    [
        "",
        "   ",
        "default-dev-secret",
        "change-this-to-a-random-string",
        "your-random-secret-here",
    ],
)
def test_resolve_secret_generates_random_for_blank_or_known_placeholder(insecure_value):
    result = shared_auth._resolve_secret(insecure_value)
    assert result != insecure_value
    assert len(result) > 20


def test_resolve_secret_generates_a_different_value_each_call():
    a = shared_auth._resolve_secret("default-dev-secret")
    b = shared_auth._resolve_secret("default-dev-secret")
    assert a != b


def test_resolve_secret_warning_names_the_risk_but_not_the_value(caplog):
    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        result = shared_auth._resolve_secret("default-dev-secret")
    assert "VITALFORGE_SECRET" in caplog.text
    assert result not in caplog.text
