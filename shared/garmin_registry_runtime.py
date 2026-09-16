"""Durable registry admission and legacy-store helpers.

The public API remains in :mod:`shared.garmin_registry`.  These helpers read
their mutable configuration and collaborators from that facade at call time so
existing startup code and test monkeypatch seams keep their exact behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path

from garminconnect.client import token_file_path

from shared import garmin_client
from shared.database import get_db

logger = logging.getLogger(__name__)


def _registry():
    # Delayed to avoid a facade/import cycle and to retain its public patch
    # points (GARTH_TOKEN_DIR, time, asyncio, and error classes).
    from shared import garmin_registry

    return garmin_registry


async def reserve_call_permit() -> None:
    """Consume one durable global leaky-bucket slot or raise retry-after."""
    registry = _registry()
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT OR IGNORE INTO garmin_call_budget (singleton, next_allowed_at) VALUES (1, 0)"
        )
        row = await (
            await db.execute("SELECT next_allowed_at FROM garmin_call_budget WHERE singleton = 1")
        ).fetchone()
        if row is None:
            raise RuntimeError("Garmin call budget is unavailable")
        now = registry.time.time()
        next_allowed_at = float(row["next_allowed_at"])
        if next_allowed_at > now:
            await db.rollback()
            raise registry.GarminRateLimited(min(60, max(1, math.ceil(next_allowed_at - now))))
        await db.execute(
            "UPDATE garmin_call_budget SET next_allowed_at = ? WHERE singleton = 1",
            (now + registry._call_interval_seconds(),),
        )
        await db.commit()
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def _wait_for_call_permit() -> None:
    """Wait for a shared permit for a logical operation already in flight."""
    registry = _registry()
    while True:
        try:
            await registry.reserve_call_permit()
            return
        except registry.GarminRateLimited as exc:
            await registry.asyncio.sleep(exc.retry_after)


async def reserve_link_attempt(user_id: int) -> None:
    """Reserve one of a user's three credential-link attempts per 15 minutes."""
    registry = _registry()
    user_id = int(user_id)
    if user_id < 1:
        raise ValueError("user_id must be a positive integer")
    db = await get_db(isolation_level=None)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = registry.time.time()
        row = await (
            await db.execute(
                """
                SELECT attempted_at_1, attempted_at_2, attempted_at_3
                FROM garmin_link_attempts WHERE user_id = ?
                """,
                (user_id,),
            )
        ).fetchone()
        if row is None:
            await db.execute(
                """
                INSERT INTO garmin_link_attempts
                    (user_id, window_started_at, attempt_count, attempted_at_1)
                VALUES (?, ?, 1, ?)
                """,
                (user_id, now, now),
            )
        else:
            retained = sorted(
                float(value)
                for value in (row["attempted_at_1"], row["attempted_at_2"], row["attempted_at_3"])
                if value is not None and float(value) >= now - registry._LINK_ATTEMPT_WINDOW_SECONDS
            )
            if len(retained) >= registry._LINK_ATTEMPT_LIMIT:
                await db.rollback()
                raise registry.GarminLinkAttemptRateLimited(
                    min(
                        registry._LINK_ATTEMPT_WINDOW_SECONDS,
                        max(1, math.ceil(retained[0] + registry._LINK_ATTEMPT_WINDOW_SECONDS - now)),
                    )
                )
            retained.append(now)
            retained.sort()
            slots = retained + [None] * (registry._LINK_ATTEMPT_LIMIT - len(retained))
            await db.execute(
                """
                UPDATE garmin_link_attempts
                SET window_started_at = ?, attempt_count = ?,
                    attempted_at_1 = ?, attempted_at_2 = ?, attempted_at_3 = ?
                WHERE user_id = ?
                """,
                (retained[0], len(retained), *slots, user_id),
            )
        await db.commit()
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def _load_link(person_id: int):
    db = await get_db()
    try:
        return await (
            await db.execute(
                "SELECT state, garmin_email, generation FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
    finally:
        await db.close()


async def _record_auth_success(person_id: int, generation: int) -> None:
    registry = _registry()
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_ok = ?, last_auth_error = NULL, last_auth_error_at = NULL
            WHERE person_id = ? AND generation = ?
            """,
            (registry._utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


async def _record_auth_failure(person_id: int, generation: int, code: str) -> None:
    registry = _registry()
    safe_code = code if code in registry._ERROR_CODES else "unknown"
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE garmin_links SET last_auth_error = ?, last_auth_error_at = ?
            WHERE person_id = ? AND generation = ?
            """,
            (safe_code, registry._utc_now(), person_id, generation),
        )
        await db.commit()
    finally:
        await db.close()


async def _is_primary_person(person_id: int) -> bool:
    db = await get_db()
    try:
        row = await (await db.execute("SELECT is_primary FROM persons WHERE id = ?", (person_id,))).fetchone()
        return row is not None and row["is_primary"] == 1
    finally:
        await db.close()


def _looks_like_legacy_token_store(path: Path) -> bool:
    """Check garminconnect's exact legacy token path without reading it."""
    try:
        token_path = token_file_path(str(path))
        return path.is_dir() and token_path.is_file() and not token_path.is_symlink()
    except OSError:
        return False


async def resolve_token_dir(person_id: int, state: str, generation: int | None = None) -> Path:
    registry = _registry()
    root = registry._ensure_token_root()
    if state == "legacy_bound" and await _is_primary_person(person_id) and _looks_like_legacy_token_store(root):
        return root
    if generation is None:
        raise ValueError("generation is required for non-legacy Garmin links")
    return root / f"person-{int(person_id)}" / f"generation-{int(generation)}"


async def bootstrap_legacy_token_store() -> bool:
    """Adopt a verified pre-Phase-3 flat token store for the primary person."""
    registry = _registry()
    try:
        root = registry._ensure_token_root()
    except OSError:
        logger.warning("Legacy Garmin token-store adoption could not prepare its token root")
        return False
    legacy_email = os.getenv("GARMIN_EMAIL")
    if not isinstance(legacy_email, str) or not _looks_like_legacy_token_store(root):
        return False
    try:
        canonical_email = registry._canonical_email(legacy_email)
    except registry.GarminLinkInputError:
        return False
    try:
        async with registry.legacy_store_flock():
            if not _looks_like_legacy_token_store(root):
                return False
            db = await get_db()
            try:
                primary = await (await db.execute("SELECT id FROM persons WHERE is_primary = 1")).fetchone()
                if primary is None:
                    return False
                person_id = int(primary["id"])
                existing = await (await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))).fetchone()
                conflict = await (await db.execute("SELECT 1 FROM garmin_links WHERE garmin_email = ?", (canonical_email,))).fetchone()
                if existing is not None or conflict is not None:
                    return False
            finally:
                await db.close()
            await registry.reserve_call_permit()
            try:
                await asyncio.to_thread(garmin_client.authenticate, person_id, 1, root, canonical_email, None)
            except Exception:
                garmin_client.forget(person_id, 1)
                logger.warning("Legacy Garmin token store could not be verified; leaving it unbound")
                return False
            db = await get_db(isolation_level=None)
            try:
                await db.execute("BEGIN IMMEDIATE")
                primary = await (await db.execute("SELECT id FROM persons WHERE is_primary = 1")).fetchone()
                existing = await (await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))).fetchone()
                conflict = await (await db.execute("SELECT 1 FROM garmin_links WHERE garmin_email = ?", (canonical_email,))).fetchone()
                if primary is None or int(primary["id"]) != person_id or existing is not None or conflict is not None:
                    await db.rollback()
                    garmin_client.forget(person_id, 1)
                    return False
                now = registry._utc_now()
                await db.execute(
                    """INSERT INTO garmin_links
                    (person_id, state, garmin_email, generation, linked_at, updated_at, last_auth_ok)
                    VALUES (?, 'legacy_bound', ?, 1, ?, ?, ?)""",
                    (person_id, canonical_email, now, now, now),
                )
                await db.execute("INSERT INTO garmin_link_generations (person_id, generation) VALUES (?, 1)", (person_id,))
                await db.commit()
                return True
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                garmin_client.forget(person_id, 1)
                raise
            finally:
                await db.close()
    except registry.GarminRateLimited:
        logger.info("Legacy Garmin token-store adoption deferred by the global call limiter")
        return False
    except Exception as exc:
        logger.warning("Legacy Garmin token-store adoption was not completed (%s)", type(exc).__name__)
        return False
