"""Constants and pure helpers every Garmin registry module shares.

A leaf: it imports only the standard library and the registry's bounded error
types, so :mod:`shared.garmin_registry_runtime` and the facade can both read
their limits and clock formatting from here without runtime having to reach
back into the facade for them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from shared.garmin_registry_errors import GarminLinkInputError

# 'legacy_bound' / 'legacy_disabled' are retired: boot-time adoption now moves
# the flat store under person-<id>/generation-1/ and publishes 'linked'.  The
# DDL still tolerates the old values (SQLite cannot alter a CHECK), so a row
# carrying one is simply not usable until it is re-linked.
VALID_LINK_STATES = frozenset({"linked"})
LINK_ATTEMPT_LIMIT = 3
LINK_ATTEMPT_WINDOW_SECONDS = 15 * 60
# The longest call() may sleep for a permit while holding a person flock.
MAX_INTERACTIVE_WAIT_SECONDS = 30.0
# The largest interval a deployment can configure, and so the largest slot any
# writer can put in the shared budget.  reserve_call_permit's clock-regression
# guard is only correct while its ceiling is this same value, which is why both
# read it from here rather than repeating the literal.
MAX_CALL_INTERVAL_SECONDS = 60.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def call_interval_seconds() -> float:
    """Read the deployment interval defensively, clamped to one minute."""
    try:
        configured = float(os.getenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2"))
    except ValueError:
        configured = 2.0
    return min(MAX_CALL_INTERVAL_SECONDS, max(1.0, configured))


def canonical_email(email: str) -> str:
    """Canonicalize account identity without attempting email validation."""
    if not isinstance(email, str):
        raise GarminLinkInputError()
    canonical = email.strip().casefold()
    if not canonical:
        raise GarminLinkInputError()
    return canonical
