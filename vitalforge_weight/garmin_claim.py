"""The Garmin push claim, shared by weight_log and strength_sessions.

Both domains take a claim inside their own BEGIN IMMEDIATE and mean the same thing
by it, so the helper is defined once here.
"""

import logging
from datetime import datetime, timedelta

# get_current_identity, not require_account_identity: the latter 401s
# whenever `user_id is None`, which includes the open-access `anonymous`
# sentinel, and GET / below must keep working in the empty-users-table mode
# CLAUDE.md documents.

logger = logging.getLogger(__name__)


# How long a Garmin push claim stays authoritative. Past this the claimant is
# presumed dead and the row may be re-claimed: a possible duplicate after a
# crash beats a weigh-in stranded unpushable forever.
#
# This value is only safe while the push is SYNCHRONOUS. A claim going stale
# underneath a still-running push would let a retry re-claim and duplicate --
# the exact failure the claim exists to prevent. That cannot happen today
# because push_weight blocks the event loop for its whole duration, so while a
# push is in flight nothing else runs to re-claim anything. That is a property
# of the current deployment, NOT something this code enforces, and it is the
# same class of reasoning that made the original double push look impossible.
# If push_weight ever moves to a thread or worker pool, this constant MUST be
# bounded by a hard push timeout -- garminconnect sets none of its own.
#
# Ten minutes is also short enough that a crashed process cannot strand a row
# or session unpushable for a meaningful time. The claim is a mutual-exclusion
# hint with an expiry, NOT a lock: for strength_sessions the correctness
# backstop against double STORAGE is still UNIQUE (person_id, session_id); the
# claim only guards the push.
#
# Shared by both push paths (weight_log and strength_sessions). They were
# written separately on two branches and folded into one definition here. The
# duplicate is worth a note because nothing would have caught it: git
# auto-merged the two definitions cleanly (they landed in different regions of
# the file), and ruff's F811 does not fire on a name that is USED between its
# two definitions -- which this one was. The second would simply have shadowed
# the first, silently, with both bodies identical so nothing behaved oddly.
_GARMIN_CLAIM_TIMEOUT_SECONDS = 600


def _garmin_claim_is_live(claimed_at: str | None, now: datetime) -> bool:
    """Whether another request is currently pushing this row to Garmin.

    An unreadable or naive claim timestamp is treated as STALE rather than
    live. The claim exists to stop a double push; a value this cannot read is
    no evidence that a push is happening, and believing it would strand the
    row unpushable until it aged out. Erring toward "not claimed" risks the
    duplicate this guards against only where the row was already corrupt,
    which the operator notices.
    """
    if claimed_at is None:
        return False
    try:
        claimed = datetime.fromisoformat(claimed_at)
    except ValueError:
        logger.warning("Unreadable garmin_claimed_at %r; treating the claim as stale", claimed_at)
        return False
    if claimed.tzinfo is None:
        logger.warning("Naive garmin_claimed_at %r; treating the claim as stale", claimed_at)
        return False
    return (now - claimed) < timedelta(seconds=_GARMIN_CLAIM_TIMEOUT_SECONDS)
