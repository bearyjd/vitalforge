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


def test_canonical_email_strips_and_casefolds():
    assert garmin_registry_common.canonical_email("  Foo@Example.COM ") == "foo@example.com"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("0.1", 1.0), ("500", 60.0), ("abc", 2.0), (None, 2.0)],
    ids=["clamped-up", "clamped-down", "unparseable", "unset"],
)
def test_call_interval_seconds_clamps_and_defaults(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", configured)
    assert garmin_registry_common.call_interval_seconds() == expected


def test_utc_now_is_a_second_precision_zulu_timestamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", garmin_registry_common.utc_now())
