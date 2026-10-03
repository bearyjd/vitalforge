"""Constants and pure helpers every Garmin registry module shares.

A leaf: it imports only the standard library and the registry's bounded error
types, so :mod:`shared.garmin_registry_runtime` and the facade can both read
their limits and clock formatting from here without runtime having to reach
back into the facade for them.  The token-root normalizer lives here too: it is
pure path logic the facade runs once, at import.
"""

from __future__ import annotations

import errno
import logging
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
# The durable token layout, <root>/person-<id>/generation-<n>/: the facade
# builds and cleans these paths, and the legacy adoption globs them.
PERSON_DIR_PREFIX = "person-"
GENERATION_DIR_PREFIX = "generation-"


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
    # casefold, deliberately kept.  This string is the garmin_links uniqueness
    # key AND what link() hands to Garmin's login (not the user's own input).
    # casefold also folds non-ASCII (``ß`` -> ``ss``; tests/test_garmin_registry_common.py
    # pins that), which would change the address Garmin sees.  Accepted because
    # Garmin accounts are ASCII in practice, where casefold() equals lower().
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
    refused outright, as is a root that resolves to ``/``, ``$HOME`` or the
    cwd (see :func:`_refuse_a_shared_directory`).  Limits: a symlink inside a
    trusted symlink's TARGET is not checked, and the check and the realpath
    are a boot-time TOCTOU pair.  Everything below the root is left to
    garminconnect's refusal on garth's own I/O.  ``os.path.realpath``, not
    ``Path.resolve()``: on 3.12 the latter raises RuntimeError, path in the
    message, on a symlink loop; a loop is refused here by name instead.  A
    bare ``~`` with no resolvable home raises RuntimeError to the caller.
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
    _refuse_a_shared_directory(root)
    return root


def _refuse_a_shared_directory(root: Path) -> None:
    """The registry chmods its root 0700 and keeps lock files in it, so ``/``,
    $HOME (a bare ``~``) or the cwd (``.``; the image's /app under compose)
    would be mutated.  Compared resolved to resolved; a home or cwd that
    cannot be resolved matches nothing rather than disabling Garmin.  Every
    match is named: in the image HOME and the cwd are both /app."""
    locations = (("the filesystem root", lambda: "/"), ("the working directory", os.getcwd),
                 ("the home directory", Path.home))
    matched = []
    for reason, locate in locations:
        try:
            directory = Path(os.path.realpath(locate()))
        except (OSError, RuntimeError, KeyError):
            continue
        if root == directory:
            matched.append(reason)
    if matched:
        raise TokenRootRefused(" and ".join(matched))


def _is_trusted_sticky(found: os.stat_result) -> bool:
    """A sticky directory owned by root (``/tmp``) or by this process: others
    may write it, but cannot rename or remove an entry they do not own, and
    its owner is already trusted.  One owned by any other user is not."""
    return found.st_uid in (0, os.geteuid()) and bool(found.st_mode & stat.S_ISVTX)


def first_writable_ancestor(root: Path) -> Path | None:
    """The highest existing ancestor of ``root`` (not the root itself, which
    the registry makes 0700) that is group- or other-writable, unless it is a
    sticky directory root or this process owns.  Advisory only: refusing would reject a
    root-owned 0775 ``/srv``.  A missing ancestor ends the walk; never raises."""
    for ancestor in reversed(root.parents):
        try:
            found = os.stat(ancestor)
        except OSError:
            return None
        if found.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not _is_trusted_sticky(found):
            return ancestor
    return None


DEFAULT_TOKEN_ROOT = "/app/data/.garth"


def configured_token_root(logger: logging.Logger) -> Path | None:
    """The environment's root, normalized once: None disables Garmin instead of
    crashing both services at import.  Only the normalizer's own fixed refusals
    are logged by text; an OSError or RuntimeError carries the path.  Logs
    through the caller's ``logger`` (the facade's), where boot errors belong.
    A blank value (compose's ``${GARTH_TOKEN_DIR:-}``) is unset, not the cwd.
    A writable ancestor is only warned about, by its (repr'd) path."""
    configured = os.getenv("GARTH_TOKEN_DIR", "")
    try:
        root = normalize_token_root(configured if configured.strip() else DEFAULT_TOKEN_ROOT)
    except (OSError, RuntimeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, TokenRootRefused) else type(exc).__name__
        logger.error("GARTH_TOKEN_DIR is unusable (%s); Garmin features are disabled", reason)
        return None
    writable = first_writable_ancestor(root)
    if writable is not None:
        logger.warning(
            "GARTH_TOKEN_DIR is below a directory other users can write (%r); "
            "another local user could replace the token root",
            str(writable),
        )
    return root
