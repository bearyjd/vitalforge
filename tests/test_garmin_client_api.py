"""Regression guard for the 2026-08-22 garminconnect 0.2.38 -> 0.3.11 upgrade.

That bump (commit 49fa674, for add_body_composition) silently broke
`shared.garmin_client.authenticate()`: the new garminconnect dropped its
`garth` dependency entirely (no `.garth` attribute, different token file
format), but `authenticate()` still called `.garth.dump(...)`. Every other
test in this suite monkeypatches `authenticate`/`garmin_client._client` to a
`FakeGarminClient` (see conftest.py) that never touches the real
`garminconnect` package, so nothing here would have caught an API mismatch
like that. These tests import the REAL `garminconnect.Garmin` class (no
network I/O -- `Garmin()` construction and `inspect.signature` are both
local) specifically to catch a future version bump that changes this shape
again.
"""

import inspect

from garminconnect import Garmin


def test_garmin_constructor_accepts_email_and_password():
    """authenticate() calls Garmin(email=..., password=...)."""
    sig = inspect.signature(Garmin.__init__)
    assert "email" in sig.parameters
    assert "password" in sig.parameters


def test_garmin_login_accepts_tokenstore_kwarg():
    """authenticate() calls client.login(tokenstore=path)."""
    sig = inspect.signature(Garmin.login)
    assert "tokenstore" in sig.parameters


def test_garmin_client_has_no_garth_attribute():
    """authenticate() must not depend on `.garth` -- that's the garth-era API
    this version of garminconnect no longer has. If this starts failing
    because garminconnect brought `.garth` back, that's fine; it means
    verify the rest of authenticate() still matches before relying on it
    again."""
    client = Garmin(email="test@example.com", password="x")
    assert not hasattr(client, "garth")
