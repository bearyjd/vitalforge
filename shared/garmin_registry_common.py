"""Constants and pure helpers every Garmin registry module shares.

A leaf: it imports only the standard library and the registry's bounded error
types, so :mod:`shared.garmin_registry_runtime` and the facade can both read
their limits and clock formatting from here without runtime having to reach
back into the facade for them.  The token-root normalizer lives here too: it is
pure path logic the facade runs once, at import.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from shared.garmin_registry_errors import GarminLinkInputError

# 'legacy_bound' / 'legacy_disabled' are retired: boot-time adoption now moves
# the flat store under person-<id>/generation-1/ and publishes 'linked'.  The
# DDL still tolerates the old values (SQLite cannot alter a CHECK), so a row
# carrying one is simply not usable until it is re-linked.
VALID_LINK_STATES = frozenset({"linked"})
LINK_ATTEMPT_LIMIT = 3
LINK_ATTEMPT_WINDOW_SECONDS = 15 * 60
# The longest call() may sleep for ANY ONE permit while holding a person
# flock.  A cold call takes two (before the credential login and after it),
# each bounded by this, with an untimed provider login between them -- so the
# flock-hold ceiling is two of these plus that login, not one.
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


# garminconnect refuses a ``~name`` token path (its private _OTHER_USER_HOME_RE);
# the same pattern, owned here, because expanduser() would erase the ``~name``.
_OTHER_USER_HOME = re.compile(r"^~[^/\\]")


class TokenRootRefused(ValueError):
    """A configured token root refused for a fixed reason; the message never
    carries the path, so it is safe to log."""


def _trusted_symlink_owner(uid: int) -> bool:
    """Who may own a symlink the token root passes through: root or this process."""
    return uid in (0, os.geteuid())


def _stat_if_present(path: Path, *, follow_symlinks: bool) -> os.stat_result | None:
    """None for a path not created yet (a first boot); a loop is a refusal."""
    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TokenRootRefused("a symlink loop") from None
        raise


def normalize_token_root(raw: str | os.PathLike[str]) -> Path:
    """The directory garth itself will address for a configured token root.

    garminconnect's ``token_file_path`` expands ``~``, refuses ``~name`` and
    refuses a path with any symlinked ancestor, so the literal value split the
    registry's paths from garth's (#73).  The facade calls this once, at
    import; the result is used as given from then on.

    Resolving would also launder a symlink another user planted at or above
    the root.  Handed the literal path, garminconnect's guard refused both;
    the kernel's ``protected_symlinks`` refuses only a trailing follow, so
    it also refused one AT the root, never one above it.  That posture is
    kept: every symlink in the configured path's own chain, walked top-down,
    must be owned by root or this process -- as the legitimate ones are:
    macOS ``/tmp`` and ``/var``, Silverblue ``/home``, an operator's own
    volume link.  ``~name``, a ``..`` component and a symlink loop are
    refused outright.  Limits: a symlink inside a trusted symlink's TARGET
    is not checked, and the check and the realpath are a boot-time TOCTOU
    pair.  Everything below the root is left to garminconnect's refusal on
    garth's own I/O.  ``os.path.realpath``, not ``Path.resolve()``: on 3.12
    the latter raises RuntimeError, path in the message, on a symlink loop;
    a loop is refused here by name instead.  A bare ``~`` with no resolvable
    home raises RuntimeError to the caller.
    """
    text = os.fspath(raw)
    if _OTHER_USER_HOME.match(text):
        raise TokenRootRefused("another user's home")
    configured = Path(text).expanduser().absolute()
    # realpath collapses '..' lexically; the lstat walk cannot follow it past a missing component.
    if ".." in configured.parts:
        raise TokenRootRefused("a '..' component")
    for component in (*reversed(configured.parents), configured):
        found = _stat_if_present(component, follow_symlinks=False)
        if found is not None and stat.S_ISLNK(found.st_mode) and not _trusted_symlink_owner(found.st_uid):
            raise TokenRootRefused("a symlink owned by another user")
    root = Path(os.path.realpath(configured))
    _stat_if_present(root, follow_symlinks=True)  # non-strict realpath leaves a loop in place
    return root
