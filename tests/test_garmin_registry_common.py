"""Direct contract tests for the registry's shared leaf helpers.

`canonical_email` and `call_interval_seconds` were private facade helpers whose
guard clauses were only ever reached through `link()` and the permit code; now
that they are a public leaf, their rejection branches and clamps are pinned
here rather than inferred from the lifecycle tests that happen to pass through
them.
"""

import re

import pytest

from shared import garmin_registry_common
from shared.garmin_registry_errors import GarminLinkInputError


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 42], ids=["empty", "spaces", "whitespace", "None", "int"])
def test_canonical_email_rejects_non_string_and_blank_input(bad):
    with pytest.raises(GarminLinkInputError):
        garmin_registry_common.canonical_email(bad)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("  Foo@Example.COM ", "foo@example.com"), ("Straße@Example.test", "strasse@example.test")],
    ids=["ascii", "casefold-not-lower"],
)
def test_canonical_email_strips_and_casefolds(raw, canonical):
    """Pins the canonicalisation the DB identity key (`garmin_links.garmin_email`)
    and the link-conflict lookup currently rely on: `casefold`, which also
    collapses e.g. `ß` -> `ss` where `lower()` would not. Whether the provider
    login should receive this canonical form rather than the user's stripped
    input is tracked in #70; this test takes no position on that."""
    assert garmin_registry_common.canonical_email(raw) == canonical


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("0.1", 1.0),
        ("500", garmin_registry_common.MAX_CALL_INTERVAL_SECONDS),
        ("abc", 2.0),
        (None, 2.0),
    ],
    ids=["clamped-up", "clamped-down", "unparseable", "unset"],
)
def test_call_interval_seconds_clamps_and_defaults(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", configured)
    assert garmin_registry_common.call_interval_seconds() == expected


def test_the_clamp_honours_the_exported_call_interval_ceiling(monkeypatch):
    """`reserve_call_permit`'s clock-regression guard treats a slot beyond
    `MAX_CALL_INTERVAL_SECONDS` as skew, which is only correct while the clamp
    here cannot produce one. Pin the coupling: a clamp re-hardcoded above the
    exported ceiling would make the guard reject slots a peer legitimately
    wrote, with nothing else failing."""
    monkeypatch.setenv(
        "GARMIN_MIN_CALL_INTERVAL_SECONDS", str(garmin_registry_common.MAX_CALL_INTERVAL_SECONDS * 10)
    )
    assert garmin_registry_common.call_interval_seconds() == garmin_registry_common.MAX_CALL_INTERVAL_SECONDS


def test_utc_now_is_a_second_precision_zulu_timestamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", garmin_registry_common.utc_now())
