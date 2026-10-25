"""Profile-scoped context storage. Unlogged dates are deliberately absent."""

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from shared.database import get_db
from vitalforge_dashboard.context_models import DailyContextInput, ExperimentInput
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS


def entry_out(row) -> dict:
    return {"date": row["date"], "note": row["note"], "tags": json.loads(row["tags_json"]),
            "updated_at": row["updated_at"]}


async def read_context(db, person_id: int, start: str, end: str) -> dict:
    """Use the caller's connection/transaction to keep exports and evidence atomic."""
    cursor = await db.execute(
        "SELECT date, note, tags_json, updated_at FROM daily_context "
        "WHERE person_id = ? AND date >= ? AND date <= ? ORDER BY date", (person_id, start, end),
    )
    entries = [entry_out(row) for row in await cursor.fetchall()]
    await cursor.close()
    cursor = await db.execute(
        "SELECT title, tag, metric, start_date, end_date, updated_at FROM context_experiments WHERE person_id = ?",
        (person_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    experiment = dict(row) if row else None
    return {"entries": entries, "experiment": experiment}


async def context_window(person_id: int, start: str, end: str) -> dict:
    db = await get_db()
    try:
        await db.execute("BEGIN")
        return await read_context(db, person_id, start, end)
    finally:
        await db.close()


async def write_context(person_id: int, *, day: str | None = None,
                        entry: DailyContextInput | None = None,
                        experiment: ExperimentInput | None = None) -> dict | None:
    """Replace/delete one row atomically, rechecking archive state under the write lock.

    day=None targets the singleton experiment; entry/experiment=None means delete.
    No historical experiment versions are retained or implied.
    """
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT id FROM persons WHERE id = ? AND archived_at IS NULL", (person_id,))
        active = await cursor.fetchone()
        await cursor.close()
        if active is None:
            raise HTTPException(404, "Not found", headers=PRIVATE_HEADERS)
        updated_at = datetime.now(timezone.utc).isoformat()
        result = None
        if day is not None:
            if entry is None:
                await db.execute("DELETE FROM daily_context WHERE person_id = ? AND date = ?", (person_id, day))
            else:
                await db.execute(
                    "INSERT INTO daily_context (person_id,date,note,tags_json,updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(person_id,date) DO UPDATE SET note=excluded.note, tags_json=excluded.tags_json, "
                    "updated_at=excluded.updated_at",
                    (person_id, day, entry.note, json.dumps(entry.tags), updated_at),
                )
                result = {"date": day, "logged": True, **entry.model_dump(), "updated_at": updated_at}
        elif experiment is None:
            await db.execute("DELETE FROM context_experiments WHERE person_id = ?", (person_id,))
        else:
            values = experiment.model_dump()
            await db.execute(
                "INSERT INTO context_experiments (person_id,title,tag,metric,start_date,end_date,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET title=excluded.title, "
                "tag=excluded.tag, metric=excluded.metric, start_date=excluded.start_date, "
                "end_date=excluded.end_date, updated_at=excluded.updated_at",
                (person_id, values["title"], values["tag"], values["metric"], values["start_date"],
                 values["end_date"], updated_at),
            )
            result = {**values, "updated_at": updated_at}
        await db.commit()
        return result
    except BaseException:
        await db.rollback()
        raise
    finally:
        await db.close()
