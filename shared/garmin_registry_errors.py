"""Bounded error types and the non-secret link record of the Garmin registry.

Every message here is deliberately fixed text: a registry error crosses the
route boundary and must never carry third-party exception detail, a token
path, or an email address.
"""

from __future__ import annotations

from dataclasses import dataclass

REGISTRY_ERROR_CODES = frozenset({"auth_failed", "rate_limited", "network", "unknown"})

LEGACY_GARMIN_TARGET_RETIRED_ERROR = "legacy_target_retired"

# Every value strength_sessions.garmin_error may hold.  APPEND-ONLY: migration
# 004 rewrites any historic value outside this tuple to 'unknown' on databases
# that have not run it yet, so removing an entry would redact live rows.
STRENGTH_GARMIN_ERROR_CODES: tuple[str, ...] = (
    "auth_failed",
    "link_required",
    "rate_limited",
    "network",
    "unknown",
    LEGACY_GARMIN_TARGET_RETIRED_ERROR,
    "activity_preparation_failed",
    "activity_push_failed",
    "activity_push_outcome_unknown",
    "activity_sets_upload_failed",
    "activity_reconciliation_failed",
    "activity_reconciliation_pending",
    "activity_outcome_record_failed",
)


class GarminRegistryError(RuntimeError):
    """Base error whose text intentionally contains no third-party detail."""


class GarminNotLinked(GarminRegistryError):
    """The requested person has no usable durable Garmin link."""

    def __init__(self, person_id: int):
        self.person_id = person_id
        super().__init__("Garmin is not linked for this person")


class GarminRateLimited(GarminRegistryError):
    """The durable deployment-wide call budget has not yet refilled."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Garmin is temporarily rate limited")


class GarminLinkAttemptRateLimited(GarminRegistryError):
    """The authenticated user has exhausted their link-attempt window."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Too many Garmin link attempts")


class GarminAuthenticationError(GarminRegistryError):
    """A link could not resume its token store without exposing why."""

    def __init__(self, code: str):
        self.code = code if code in REGISTRY_ERROR_CODES else "unknown"
        super().__init__("Garmin authentication failed")


class GarminOperationError(GarminRegistryError):
    """A Garmin operation failed without surfacing its raw exception text."""

    def __init__(self, code: str):
        self.code = code if code in REGISTRY_ERROR_CODES else "unknown"
        super().__init__("Garmin operation failed")


class GarminLinkConflict(GarminRegistryError):
    """Another person already owns the requested canonical Garmin account."""

    def __init__(self):
        super().__init__("Garmin account is already linked")


class GarminSessionExpired(GarminRegistryError):
    """The actor's step-up session changed while a credential login ran."""

    def __init__(self):
        super().__init__("Your session is no longer current")


class GarminLinkInputError(GarminRegistryError):
    """Credential metadata could not be accepted without echoing it back."""

    def __init__(self):
        super().__init__("Garmin link details are invalid")


@dataclass(frozen=True)
class GarminLink:
    """Non-secret durable link metadata returned to future route handlers."""

    person_id: int
    generation: int
    state: str
