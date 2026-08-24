"""Unit tests for the A1 auth helper: `_bearer_token_valid` and the
`check_credentials` non-ASCII fix (D3). Pure functions, no wiring — the
bearer check isn't called from `get_current_user`/the middleware yet (A2).

`_API_TOKEN`/`_PASS` are read at import time (see conftest.py's own note on
`DB_PATH`), so tests vary them via `monkeypatch.setattr(shared.auth, ...)`,
never `monkeypatch.setenv`.
"""

import logging

import pytest
from starlette.requests import Request

from shared import auth as shared_auth
from shared.auth import _bearer_token_valid, _hash_password, _verify_password, check_credentials
from tests.conftest import seed_user


def make_request(headers: list[tuple[str, str]] | None = None) -> Request:
    """A minimal Request built from a hand-written ASGI scope — no app, no
    HTTP round-trip, just enough for `.headers`/`.cookies` to work."""
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])]
    return Request({"type": "http", "headers": raw_headers})


def bearer_request(value: str) -> Request:
    return make_request([("authorization", f"Bearer {value}")])


@pytest.fixture
def set_token(monkeypatch):
    def _set(token: str):
        monkeypatch.setattr(shared_auth, "_API_TOKEN", token)

    return _set


def test_bearer_valid_token_accepted(set_token):
    set_token("correct-token")
    assert _bearer_token_valid(bearer_request("correct-token")) is True


def test_bearer_wrong_token_rejected(set_token):
    set_token("correct-token")
    assert _bearer_token_valid(bearer_request("wrong-token")) is False


def test_bearer_empty_value_rejected(set_token):
    set_token("correct-token")
    assert _bearer_token_valid(make_request([("authorization", "Bearer ")])) is False


def test_bearer_whitespace_only_value_rejected(set_token):
    set_token("correct-token")
    assert _bearer_token_valid(make_request([("authorization", "Bearer    ")])) is False


def test_bearer_rejected_when_token_unconfigured(set_token):
    set_token("")
    assert _bearer_token_valid(bearer_request("correct-token")) is False


def test_bearer_rejected_when_token_configured_whitespace_only(set_token):
    set_token("   ")
    assert _bearer_token_valid(bearer_request("   ")) is False


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_bearer_scheme_case_insensitive(set_token, scheme):
    set_token("correct-token")
    assert _bearer_token_valid(make_request([("authorization", f"{scheme} correct-token")])) is True


@pytest.mark.parametrize(
    "header_value",
    ["Basic correct-token", "correct-token"],
)
def test_bearer_wrong_scheme_rejected(set_token, header_value):
    set_token("correct-token")
    assert _bearer_token_valid(make_request([("authorization", header_value)])) is False


def test_bearer_non_ascii_token_returns_false_not_typeerror(set_token):
    set_token("correct-token")
    # No exception is the assertion; a wrong result would also be a failure
    # since it's compared against a real token that can't match.
    assert _bearer_token_valid(bearer_request("tökén")) is False


def test_bearer_surrounding_whitespace_stripped(set_token):
    set_token("correct-token")
    assert _bearer_token_valid(make_request([("authorization", "Bearer   correct-token   ")])) is True


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


def test_startup_warns_when_token_set_and_pass_empty(monkeypatch, caplog):
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "sometoken")
    monkeypatch.setattr(shared_auth, "_PASS", "")
    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        shared_auth._warn_if_misconfigured()
    assert "VITALFORGE_API_TOKEN is set but VITALFORGE_PASS is empty" in caplog.text


@pytest.mark.parametrize(
    "token,password",
    [
        ("sometoken", "somepass"),  # both set
        ("", ""),  # neither set
    ],
)
def test_startup_silent_in_the_other_three_configs(monkeypatch, caplog, token, password):
    monkeypatch.setattr(shared_auth, "_API_TOKEN", token)
    monkeypatch.setattr(shared_auth, "_PASS", password)
    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        shared_auth._warn_if_misconfigured()
    assert caplog.text == ""


def test_startup_warning_contains_no_token_value(monkeypatch, caplog):
    monkeypatch.setattr(shared_auth, "_API_TOKEN", "super-secret-token-value")
    monkeypatch.setattr(shared_auth, "_PASS", "")
    with caplog.at_level(logging.WARNING, logger=shared_auth.__name__):
        shared_auth._warn_if_misconfigured()
    assert "super-secret-token-value" not in caplog.text


def test_resolve_secret_passes_through_configured_value():
    assert shared_auth._resolve_secret("a-real-secret-value") == "a-real-secret-value"


def test_resolve_secret_generates_random_when_still_default():
    result = shared_auth._resolve_secret("default-dev-secret")
    assert result != "default-dev-secret"
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
