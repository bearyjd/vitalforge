"""Optional private context routes with profile authorization and narrow CSRF guards."""

import json
from datetime import timedelta

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.auth import require_person
from vitalforge_dashboard import data_quality
from vitalforge_dashboard.context_analysis import context_associations
from vitalforge_dashboard.context_models import DailyContextInput, ExperimentInput, canonical_date
from vitalforge_dashboard.context_store import context_window, write_context
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS


def _day(value: str) -> str:
    try:
        return canonical_date(value)
    except ValueError:
        raise HTTPException(422, "Expected an ISO calendar date", headers=PRIVATE_HEADERS) from None


async def context_write_guard(request: Request) -> None:
    # The fixed header forces cross-origin browser requests through CORS preflight.
    # This app does not enable CORS. Do not compare Origin with proxy-derived URLs.
    if request.headers.get("X-VitalForge-Write") != "context" or request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise HTTPException(403, "Context write header required", headers=PRIVATE_HEADERS)
    if request.method == "PUT" and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON body required", headers=PRIVATE_HEADERS)


async def _body(request: Request, model):
    # Parse here so validation errors never echo private note/tag/title contents.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(413, "Context body too large", headers=PRIVATE_HEADERS)
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError, RecursionError):
        raise HTTPException(422, "Invalid context fields or bounds", headers=PRIVATE_HEADERS) from None


def _response(value) -> JSONResponse:
    return JSONResponse(value, headers=PRIVATE_HEADERS)


def add_context_routes(app) -> None:
    view = require_person("view")
    manage = require_person("manage")

    @app.get("/p/{slug}/api/context")
    async def context_list(request: Request, slug: str, days: int = Query(default=30, ge=1, le=365),
                           person_id: int = Depends(view)):
        end = data_quality.utc_now().date()
        start = end - timedelta(days=days - 1)
        context = await context_window(person_id, start.isoformat(), end.isoformat())
        can_manage = True
        try:
            await manage(request, slug)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            can_manage = False
        return _response({
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "days": days,
                       "boundaries": "inclusive", "window_timezone": "UTC",
                       "observation_date_semantics": "User-chosen calendar dates; timezone unknown."},
            **context, "can_manage": can_manage,
            "tag_suggestions": sorted({tag for entry in context["entries"] for tag in entry["tags"]}),
        })

    @app.get("/p/{slug}/api/context/associations")
    async def associations(days: int = Query(default=30), person_id: int = Depends(view)):
        if days not in {7, 30, 90}:
            raise HTTPException(422, "Choose a 7, 30 or 90 day window", headers=PRIVATE_HEADERS)
        quality, data, context = await data_quality.profile_snapshot(person_id, days, include_context=True)
        return _response(context_associations(quality, data, context))

    @app.get("/p/{slug}/api/context/experiment")
    async def get_experiment(person_id: int = Depends(view)):
        context = await context_window(person_id, "0001-01-01", "0001-01-01")
        return _response(context["experiment"])

    @app.put("/p/{slug}/api/context/experiment", dependencies=[Depends(context_write_guard)])
    async def put_experiment(request: Request, person_id: int = Depends(manage)):
        experiment = await _body(request, ExperimentInput)
        return _response(await write_context(person_id, experiment=experiment))

    @app.delete("/p/{slug}/api/context/experiment", dependencies=[Depends(context_write_guard)])
    async def delete_experiment(person_id: int = Depends(manage)):
        await write_context(person_id)
        return _response({"deleted": True})

    @app.get("/p/{slug}/api/context/days/{day}")
    async def get_day(day: str, person_id: int = Depends(view)):
        day = _day(day)
        context = await context_window(person_id, day, day)
        entry = context["entries"][0] if context["entries"] else None
        return _response({**entry, "logged": True} if entry else
                         {"date": day, "logged": False, "note": "", "tags": [], "updated_at": None})

    @app.put("/p/{slug}/api/context/days/{day}", dependencies=[Depends(context_write_guard)])
    async def put_day(day: str, request: Request, person_id: int = Depends(manage)):
        day = _day(day)
        entry = await _body(request, DailyContextInput)
        return _response(await write_context(person_id, day=day, entry=entry))

    @app.delete("/p/{slug}/api/context/days/{day}", dependencies=[Depends(context_write_guard)])
    async def delete_day(day: str, person_id: int = Depends(manage)):
        await write_context(person_id, day=_day(day))
        return _response({"deleted": True})
