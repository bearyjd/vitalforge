"""GET /p/{slug}/api/export and its CSV/JSON serialisers.
"""

import csv
import io
import json
import logging

from fastapi import Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from shared.auth import (
    require_person,
)
from shared.database import (
    get_db,
)
from vitalforge_dashboard.metrics import METRIC_TABLES

logger = logging.getLogger(__name__)


async def _export_rows(person_id: int, metrics: list[str], days: int):
    """Yield (metric_name, date, value) tuples for the given metrics.

    Reuses get_metrics()'s exact query pattern (same WHERE/ORDER BY clause,
    same NULL-value filtering, same person_id scoping) against one shared DB
    connection for the whole export, rather than opening/closing a
    connection per metric. `table`/`column` are always looked up from
    METRIC_TABLES (never taken from the raw request), so the f-string
    interpolation into the SQL identifier positions below is safe.

    `person_id` is a parameter rather than something resolved in here: this
    generator is consumed by StreamingResponse after export_data() has
    returned, so there is no request scope left to authorize against.
    export_data() takes it from require_person and threads it down.
    """
    db = await get_db()
    try:
        for metric_name in metrics:
            table, column = METRIC_TABLES[metric_name]
            cursor = await db.execute(
                f"SELECT date, [{column}] as value FROM [{table}] "
                f"WHERE person_id = ? AND date >= date('now', ?) ORDER BY date ASC",
                (person_id, f"-{days} days"),
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row["value"] is not None:
                    yield metric_name, row["date"], row["value"]
    except Exception:
        # The StreamingResponse has already sent a 200 and headers by the time
        # a failure happens here, so the client just sees a truncated
        # download with no indication anything went wrong. Log server-side
        # before re-raising so the failure isn't silently lost.
        logger.exception("Export failed mid-stream (metrics=%s, days=%s)", metrics, days)
        raise
    finally:
        await db.close()


async def _export_csv(person_id: int, metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as CSV text chunks."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["metric", "date", "value"] if include_metric_column else ["date", "value"])
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    async for metric_name, date, value in _export_rows(person_id, metrics, days):
        writer.writerow([metric_name, date, value] if include_metric_column else [date, value])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


async def _export_json(person_id: int, metrics: list[str], days: int, include_metric_column: bool):
    """Stream export rows as a JSON array, one object per row."""
    first = True
    yield "["
    async for metric_name, date, value in _export_rows(person_id, metrics, days):
        if not first:
            yield ","
        first = False
        record = {"date": date, "value": value}
        if include_metric_column:
            record = {"metric": metric_name, **record}
        yield json.dumps(record)
    yield "]"


def add_export_routes(app):
    """Register these routes on a FastAPI app.

    Mirrors shared/persons_admin.py's add_person_routes, and the weight
    service's add_weight_routes / add_activity_routes."""

    @app.get("/p/{slug}/api/export")
    async def export_data(
        metric: str = Query(default="all"),
        days: int = Query(default=30, ge=1, le=365),
        format: str = Query(default="csv"),
        person_id: int = Depends(require_person("view")),
    ):
        """Stream metric data as a CSV or JSON file download.

        `metric=all` streams long/tidy `metric,date,value` rows across every
        known metric; a single metric name streams just `date,value`.
        """
        if metric != "all" and metric not in METRIC_TABLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown metric '{metric}'. Valid: all, {', '.join(sorted(METRIC_TABLES))}",
            )
        if format not in {"csv", "json"}:
            raise HTTPException(status_code=400, detail=f"Unknown format '{format}'. Valid: csv, json")

        metrics_to_export = sorted(METRIC_TABLES) if metric == "all" else [metric]
        include_metric_column = metric == "all"
        filename = f"vitalforge-export-{metric}-{days}d.{format}"

        if format == "csv":
            generator = _export_csv(person_id, metrics_to_export, days, include_metric_column)
            media_type = "text/csv"
        else:
            generator = _export_json(person_id, metrics_to_export, days, include_metric_column)
            media_type = "application/json"

        return StreamingResponse(
            generator,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
