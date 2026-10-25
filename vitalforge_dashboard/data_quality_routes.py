"""Profile-authorized quality reports, using local data only."""

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from shared.auth import require_person
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS, metric_snapshot


def add_data_quality_routes(app: FastAPI) -> None:
    @app.get("/p/{slug}/api/data-quality")
    async def data_quality(
        days: int = Query(default=30, ge=1, le=365),
        person_id: int = Depends(require_person("view")),
    ) -> JSONResponse:
        quality, _ = await metric_snapshot(person_id, days)
        return JSONResponse(quality, headers=PRIVATE_HEADERS)
