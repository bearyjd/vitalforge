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
from shared.auth import _bearer_token_valid, check_credentials


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


def test_check_credentials_non_ascii_password_returns_false_not_typeerror(monkeypatch):
    monkeypatch.setattr(shared_auth, "_USER", "admin")
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    assert check_credentials("admin", "tökén") is False


def test_check_credentials_valid_pair_accepted(monkeypatch):
    monkeypatch.setattr(shared_auth, "_USER", "admin")
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    assert check_credentials("admin", "correct-pass") is True


def test_check_credentials_rejects_wrong_user_and_wrong_pass(monkeypatch):
    monkeypatch.setattr(shared_auth, "_USER", "admin")
    monkeypatch.setattr(shared_auth, "_PASS", "correct-pass")
    assert check_credentials("wrong-user", "correct-pass") is False
    assert check_credentials("admin", "wrong-pass") is False


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
