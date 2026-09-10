"""FIT-file activity import and the two activity read routes.

Named for the FIT concept on purpose. The `activities` table is FIT-only by its own
CHECK (source_format IN ('fit')) and is a DIFFERENT concept from the weight service's
strength_sessions, which vitalforge_weight/activity_routes.py serves. CLAUDE.md is
explicit that the two must not be unified; the module names should not blur that.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import Depends, File, HTTPException, Query, UploadFile

from shared.auth import (
    require_person,
)
from shared.database import (
    get_db,
)
from vitalforge_dashboard import fit_import

logger = logging.getLogger(__name__)


# How close two uploads' (sport, start_time_utc) must be to be treated as
# the same activity re-imported (e.g. the same watch export processed
# twice, landing a few seconds apart in wall-clock terms even though the
# file bytes differ slightly). This is the second dedup stage -- the first
# is the exact `file_sha256` match below.
ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS = 120


_ACTIVITY_COLUMNS = (
    "id", "start_time_utc", "sport", "duration_seconds", "distance_m", "calories",
    "avg_hr", "max_hr", "elevation_gain_m", "source_format", "file_sha256", "imported_at",
)


async def _read_upload_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an UploadFile in bounded chunks, rejecting anything over
    `max_bytes` before it's fully buffered in memory -- trusting
    `Content-Length` alone isn't enough since a client can omit or lie
    about it."""
    chunk_size = 1024 * 1024
    chunks = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"file exceeds {max_bytes} byte upload limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _activity_row_to_dict(row) -> dict:
    return {col: row[col] for col in _ACTIVITY_COLUMNS}


def add_fit_activity_routes(app):
    """Register these routes on a FastAPI app.

    Mirrors shared/persons_admin.py's add_person_routes, and the weight
    service's add_weight_routes / add_activity_routes."""

    @app.post("/p/{slug}/api/import/activity")
    async def import_activity(
        file: UploadFile = File(...),
        person_id: int = Depends(require_person("manage")),
    ):
        """Import a local FIT activity file. FIT-only for this first slice --
        TCX/GPX are explicitly deferred. Dedup is two-stage and race-free: an
        exact `file_sha256` match, then a (sport, start_time_utc) time-window
        match for near-duplicates, both performed inside one `BEGIN IMMEDIATE`
        transaction so two concurrent uploads of the same file can never both
        pass the check before either commits -- mirrors the fix already applied
        to `vitalforge_weight/app.py`'s weight_log dedup (see that file's
        `post_weight` for the full rationale)."""
        data = await _read_upload_capped(file, fit_import.MAX_UPLOAD_BYTES)

        try:
            record = fit_import.parse_fit_bytes(data)
        except fit_import.FitImportError as e:
            raise HTTPException(status_code=400, detail=str(e))

        file_hash = fit_import.compute_file_hash(data)
        imported_at = datetime.now(timezone.utc).isoformat()
        raw_summary_json = json.dumps(record.raw_summary, default=str)

        columns_sql = ", ".join(_ACTIVITY_COLUMNS)

        db = await get_db()
        try:
            # Atomic: the exact-hash check, the near-duplicate check, and the
            # insert (if neither matches) all happen inside one transaction, so
            # two concurrent uploads of the same file can never both observe
            # "no duplicate" and both insert.
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                f"SELECT {columns_sql} FROM activities WHERE person_id = ? AND file_sha256 = ?",
                (person_id, file_hash),
            )
            existing = await cursor.fetchone()
            duplicate_reason = "exact_duplicate" if existing is not None else None

            if existing is None:
                cursor = await db.execute(
                    f"SELECT {columns_sql} FROM activities "
                    "WHERE person_id = ? "
                    "AND sport IS ? "
                    "AND julianday(start_time_utc) >= julianday(?, ?) "
                    "AND julianday(start_time_utc) <= julianday(?, ?) "
                    "ORDER BY start_time_utc DESC LIMIT 1",
                    (
                        person_id,
                        record.sport,
                        record.start_time_utc,
                        f"-{ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS} seconds",
                        record.start_time_utc,
                        f"+{ACTIVITY_NEAR_DUPLICATE_WINDOW_SECONDS} seconds",
                    ),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    duplicate_reason = "near_duplicate"

            if existing is not None:
                # Nothing to write -- commit() here is a no-op against the DB
                # but still releases the IMMEDIATE lock, mirroring
                # vitalforge_weight/app.py's post_weight, which also commits
                # unconditionally after its dedup check-then-insert regardless
                # of which branch ran.
                await db.commit()
                row = existing
            else:
                insert_cursor = await db.execute(
                    "INSERT INTO activities (person_id, start_time_utc, sport, duration_seconds, distance_m, calories, "
                    "avg_hr, max_hr, elevation_gain_m, source_format, file_sha256, imported_at, raw_summary_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        person_id,
                        record.start_time_utc,
                        record.sport,
                        record.duration_seconds,
                        record.distance_m,
                        record.calories,
                        record.avg_hr,
                        record.max_hr,
                        record.elevation_gain_m,
                        record.source_format,
                        file_hash,
                        imported_at,
                        raw_summary_json,
                    ),
                )
                row_id = insert_cursor.lastrowid
                await db.commit()
                cursor = await db.execute(
                    f"SELECT {columns_sql} FROM activities WHERE id = ? AND person_id = ?",
                    (row_id, person_id),
                )
                row = await cursor.fetchone()
        finally:
            await db.close()

        result = _activity_row_to_dict(row)
        if duplicate_reason is not None:
            result["duplicate"] = True
            result["duplicate_reason"] = duplicate_reason
        return result

    @app.get("/p/{slug}/api/activities")
    async def list_activities(
        limit: int = Query(default=50, ge=1, le=200),
        person_id: int = Depends(require_person("view")),
    ):
        """List imported activities, most recent first."""
        columns_sql = ", ".join(_ACTIVITY_COLUMNS)
        db = await get_db()
        try:
            cursor = await db.execute(
                f"SELECT {columns_sql} FROM activities "
                "WHERE person_id = ? ORDER BY start_time_utc DESC LIMIT ?",
                (person_id, limit),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        return {"count": len(rows), "activities": [_activity_row_to_dict(row) for row in rows]}

    @app.get("/p/{slug}/api/activities/{activity_id}")
    async def get_activity(activity_id: int, person_id: int = Depends(require_person("view"))):
        """A single imported activity, including its full raw FIT session
        summary."""
        columns_sql = ", ".join(_ACTIVITY_COLUMNS)
        db = await get_db()
        try:
            cursor = await db.execute(
                f"SELECT {columns_sql}, raw_summary_json FROM activities "
                "WHERE id = ? AND person_id = ?",
                (activity_id, person_id),
            )
            row = await cursor.fetchone()
        finally:
            await db.close()

        if row is None:
            raise HTTPException(status_code=404, detail="activity not found")

        result = _activity_row_to_dict(row)
        result["raw_summary"] = json.loads(row["raw_summary_json"]) if row["raw_summary_json"] else None
        return result
