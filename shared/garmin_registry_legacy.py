"""One-time adoption of the pre-Phase-3 flat ``.garth`` token store.

Both service lifespans call :func:`bootstrap_legacy_token_store` last, after
the schema and the first admin exist.  This module sits above the facade: it
takes everything it needs from the registry -- token directories, the flocks,
the permit wait -- as attributes of :mod:`shared.garmin_registry` read at call
time (``GARTH_TOKEN_DIR`` is a facade global), and the facade never imports it
back.  Nothing else in the registry knows the flat store exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from garminconnect.client import token_file_path

from shared import garmin_client, garmin_registry, garmin_registry_common
from shared.database import get_db
from shared.garmin_registry_errors import GarminLinkInputError, GarminRateLimited

logger = logging.getLogger(__name__)

# One-time marker in ``auth_migrations`` (the same table
# ``shared.auth.bootstrap_migrated_token`` uses).  ``assert_schema_understood``
# only reads ``schema_migrations``, so an older image ignores this marker
# instead of boot-looping on it.
_LEGACY_ADOPTION_MARKER = "legacy-garth-store-adopted"
_LEGACY_GENERATION = 1


def _looks_like_legacy_token_store(path: Path) -> bool:
    """Check garminconnect's exact legacy token path without reading it."""
    try:
        token_path = token_file_path(str(path))
        return path.is_dir() and token_path.is_file() and not token_path.is_symlink()
    except (OSError, ValueError):
        return False


async def bootstrap_legacy_token_store() -> bool:
    """Adopt a verified pre-Phase-3 flat token store for the primary person, once.

    Adoption moves ``<root>/garmin_tokens.json`` under the primary person's
    ``generation-1`` directory and publishes an ordinary ``linked`` row, then
    records :data:`_LEGACY_ADOPTION_MARKER` in ``auth_migrations`` in the
    same transaction.  The marker is what makes this one-time: it survives
    unlink, re-link, archive, and even a restored backup of the flat store.
    """
    try:
        root = garmin_registry._ensure_token_root()
    except OSError:
        logger.warning("Legacy Garmin token-store adoption could not prepare its token root")
        return False
    legacy_email = os.getenv("GARMIN_EMAIL")
    if not isinstance(legacy_email, str):
        logger.info("Legacy Garmin token-store adoption skipped: GARMIN_EMAIL is not set")
        return False
    try:
        canonical_email = garmin_registry_common.canonical_email(legacy_email)
    except GarminLinkInputError:
        return False
    try:
        async with garmin_registry.legacy_store_flock():
            return await _adopt_legacy_store_locked(root, canonical_email)
    except GarminRateLimited:
        logger.info(
            "Legacy Garmin token-store adoption could not get a Garmin call permit; "
            "it is retried at the next boot"
        )
        return False
    except Exception as exc:
        logger.warning("Legacy Garmin token-store adoption was not completed (%s)", type(exc).__name__)
        return False


async def _adopt_legacy_store_locked(root: Path, canonical_email: str) -> bool:
    """The adoption decision tree; the caller holds ``legacy_store_flock``.

    Lock order is ``legacy_store_flock`` -> ``person_flock``, and nothing
    takes them the other way round: ``link()``, ``unlink()``, ``call()`` and
    the archive route hold only ``person_flock``.  The person flock is what
    keeps a route-driven link for the same person from publishing its own
    ``generation-1`` while the flat store is being verified and moved; the
    primary is therefore re-checked once the flock is held, since a link may
    have reserved its generation in the meantime.
    """
    if await _legacy_adoption_recorded():
        logger.info("Legacy Garmin token-store adoption skipped: marker already recorded")
        return False
    person_id = await _adoptable_primary_person(canonical_email)
    if person_id is None:
        logger.info(
            "Legacy Garmin token-store adoption skipped: no primary person is adoptable "
            "(no primary, or the primary already has lifecycle state / an email conflict)"
        )
        return False
    async with garmin_registry.person_flock(person_id):
        if await _adoptable_primary_person(canonical_email) != person_id:
            logger.info(
                "Legacy Garmin token-store adoption skipped for person %s: it gained "
                "lifecycle state while its lock was being acquired",
                person_id,
            )
            return False
        return await _adopt_for_person_locked(person_id, canonical_email, root)


async def _adopt_for_person_locked(person_id: int, canonical_email: str, root: Path) -> bool:
    """Both flocks are held.  The moved-store branch exists for a process
    killed between the file move and the database commit: the flat store is
    already under ``generation-1`` and only the publication is missing."""
    durable = garmin_registry.resolve_token_dir(person_id, _LEGACY_GENERATION)
    if _looks_like_legacy_token_store(durable):
        if not await _verify_token_store(person_id, canonical_email, durable):
            return False
        published = await _publish_legacy_adoption(person_id, canonical_email)
        if published:
            logger.info(
                "Adopted the legacy Garmin token store for person %s as generation 1", person_id
            )
        return published
    await _warn_about_orphaned_moved_stores(root, person_id)
    if not _looks_like_legacy_token_store(root):
        logger.info("Legacy Garmin token-store adoption skipped: no flat token store is present")
        return False
    return await _adopt_flat_store(person_id, canonical_email, root, durable)


def _moved_store_person_ids(root: Path) -> list[int]:
    """Person ids owning a ``generation-1`` token file, from names alone."""
    person_ids: list[int] = []
    for token_path in root.glob("person-*/generation-1/garmin_tokens.json"):
        suffix = token_path.parent.parent.name.removeprefix("person-")
        if suffix.isdigit():
            person_ids.append(int(suffix))
    return person_ids


async def _warn_about_orphaned_moved_stores(root: Path, primary_id: int) -> None:
    """Log-only: name a moved store that a change of primary orphaned.

    A crash between the move and the commit leaves the flat store under the
    then-primary's ``generation-1`` with no ledger row.  If a different person
    is primary by the next boot, the recovery branch never looks there again;
    an operator has to decide what that file is, so it is named, never read
    or deleted.
    """
    candidates = [pid for pid in await asyncio.to_thread(_moved_store_person_ids, root) if pid != primary_id]
    if not candidates:
        return
    db = await get_db()
    try:
        for person_id in candidates:
            ledger = await (
                await db.execute("SELECT 1 FROM garmin_link_generations WHERE person_id = ?", (person_id,))
            ).fetchone()
            if ledger is None:
                logger.warning(
                    "Legacy Garmin token store moved for person %s was never published; leaving it in place",
                    person_id,
                )
    finally:
        await db.close()


async def _adopt_flat_store(person_id: int, canonical_email: str, root: Path, durable: Path) -> bool:
    """Verify, move, then publish; a failed publication puts the file back.

    A cancellation never does: after the commit the ``linked`` row expects
    the file under ``generation-1``, and before it the moved file is exactly
    what the interrupted-adoption branch of :func:`_adopt_for_person_locked`
    completes at the next boot.
    """
    if not await _verify_token_store(person_id, canonical_email, root):
        return False
    source = token_file_path(str(root))
    target = token_file_path(str(durable))
    await asyncio.to_thread(_move_token_file, source, target)
    try:
        published = await _publish_legacy_adoption(person_id, canonical_email)
    except Exception:
        await asyncio.to_thread(_restore_token_file, target, source)
        raise
    if not published:
        await asyncio.to_thread(_restore_token_file, target, source)
    else:
        logger.info("Adopted the legacy Garmin token store for person %s as generation 1", person_id)
    return published


async def _verify_token_store(person_id: int, canonical_email: str, token_dir: Path) -> bool:
    """Resume a token store once, then drop the client so nothing dumps there later.

    The verification client's SDK persistence path is ``token_dir``; a cached
    copy would silently re-dump refreshed tokens to the flat root after the
    move.  A cold :func:`shared.garmin_registry.call` re-logs-in from the
    durable directory instead.
    """
    # A busy permit is worth a short wait rather than a whole boot cycle.
    # This sleeps while holding both the legacy-store and the primary's
    # person flock, so the bound is deliberately a few intervals, not open.
    await garmin_registry._wait_for_call_permit(deadline_seconds=3 * garmin_registry_common.call_interval_seconds())
    try:
        await asyncio.to_thread(
            garmin_client.authenticate, person_id, _LEGACY_GENERATION, token_dir, canonical_email, None
        )
    except Exception as exc:
        logger.warning(
            "Legacy Garmin token store could not be verified (%s); leaving it unbound", type(exc).__name__
        )
        return False
    finally:
        garmin_client.forget(person_id, _LEGACY_GENERATION)
    return True


def _move_token_file(source: Path, target: Path) -> None:
    """Move the flat store into its private generation directory atomically.

    Like :func:`shared.garmin_registry._install_staged_token_dir` it never
    replaces an existing file: a token file already at ``target`` belongs to
    a published generation, and the flat store is then residue.
    """
    garmin_client._ensure_token_dir(target.parent)
    target.parent.parent.chmod(0o700)  # person-<id>/ is registry-owned; tighten it even if it pre-existed
    if target.exists() or target.is_symlink():
        raise FileExistsError("generation token file already exists")
    os.replace(source, target)


def _restore_token_file(target: Path, source: Path) -> None:
    """Best effort: put a moved flat store back so the next boot can retry."""
    try:
        if target.is_file() and not source.exists():
            os.replace(target, source)
    except OSError as exc:
        logger.warning(
            "Legacy Garmin token store could not be restored after a failed adoption (%s)",
            type(exc).__name__,
        )


async def _legacy_adoption_recorded() -> bool:
    db = await get_db()
    try:
        return await _legacy_adoption_recorded_in(db)
    finally:
        await db.close()


async def _legacy_adoption_recorded_in(db) -> bool:
    row = await (
        await db.execute("SELECT 1 FROM auth_migrations WHERE name = ?", (_LEGACY_ADOPTION_MARKER,))
    ).fetchone()
    return row is not None


async def _adoptable_primary_person(canonical_email: str) -> int | None:
    db = await get_db()
    try:
        return await _adoptable_primary_person_in(db, canonical_email)
    finally:
        await db.close()


async def _adoptable_primary_person_in(db, canonical_email: str) -> int | None:
    """The primary person's id while the new lifecycle has never touched it.

    A link row, a generation-ledger row, or another person owning the address
    all mean the per-person lifecycle already has authority over this person;
    the flat store is then stale residue, never a credential to revive.  The
    ledger check is also what keeps the moved-store recovery branch honest: a
    route-driven link reserves its generation before logging in, so a
    ``generation-1`` directory with no ledger row can only be an interrupted
    adoption.
    """
    primary = await (await db.execute("SELECT id FROM persons WHERE is_primary = 1")).fetchone()
    if primary is None:
        return None
    person_id = int(primary["id"])
    existing = await (
        await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
    ).fetchone()
    ledger = await (
        await db.execute("SELECT 1 FROM garmin_link_generations WHERE person_id = ?", (person_id,))
    ).fetchone()
    conflict = await (
        await db.execute("SELECT 1 FROM garmin_links WHERE garmin_email = ?", (canonical_email,))
    ).fetchone()
    if existing is not None or ledger is not None or conflict is not None:
        return None
    return person_id


async def _publish_legacy_adoption(person_id: int, canonical_email: str) -> bool:
    """Publish the adopted store as a ``linked`` generation 1 with its marker."""
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        if await _legacy_adoption_recorded_in(db) or (
            await _adoptable_primary_person_in(db, canonical_email) != person_id
        ):
            await db.rollback()
            return False
        now = garmin_registry_common.utc_now()
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at, last_auth_ok)
            VALUES (?, 'linked', ?, ?, ?, ?, ?)
            """,
            (person_id, canonical_email, _LEGACY_GENERATION, now, now, now),
        )
        await db.execute(
            "INSERT INTO garmin_link_generations (person_id, generation) VALUES (?, ?)",
            (person_id, _LEGACY_GENERATION),
        )
        await db.execute(
            "INSERT INTO auth_migrations (name, completed_at) VALUES (?, ?)",
            (_LEGACY_ADOPTION_MARKER, now),
        )
        await db.commit()
        return True
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()
