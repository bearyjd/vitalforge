"""Authorized local-only personal comparison reports."""

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.auth import require_person
from vitalforge_dashboard.comparisons import comparison_report
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS


def add_comparison_routes(app: FastAPI) -> None:
    @app.get("/p/{slug}/api/comparisons")
    async def comparisons(
        days: int = Query(default=30),
        person_id: int = Depends(require_person("view")),
    ) -> JSONResponse:
        if days not in (7, 30, 90):
            raise HTTPException(status_code=422, detail="days must be 7, 30, or 90")
        report = await comparison_report(person_id, days)
        return JSONResponse(report, headers=PRIVATE_HEADERS)
