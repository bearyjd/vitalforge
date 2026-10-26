"""Authorized, printable clinician report: a private server-rendered page, never a shareable link."""

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from shared.auth import require_person
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS
from vitalforge_dashboard.report_data import ALLOWED_DAYS, build_report

REPORT_HEADERS = {**PRIVATE_HEADERS, "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


def add_report_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.get("/p/{slug}/report", response_class=HTMLResponse)
    async def report_page(
        request: Request,
        slug: str,
        days: int = Query(default=30),
        include_identity: bool = Query(default=False),
        include_context: bool = Query(default=False),
        person_id: int = Depends(require_person("view")),
    ) -> HTMLResponse:
        if days not in ALLOWED_DAYS:
            raise HTTPException(status_code=422, detail="days must be 7, 30, or 90",
                                headers=PRIVATE_HEADERS)
        report = await build_report(
            person_id, days, include_identity=include_identity, include_context=include_context)
        return templates.TemplateResponse(
            request, "report.html", {"report": report, "person_slug": slug}, headers=REPORT_HEADERS)
