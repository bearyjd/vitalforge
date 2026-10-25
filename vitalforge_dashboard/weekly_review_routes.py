"""Authorized local-only on-demand weekly review."""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from shared.auth import require_account_identity, require_person
from vitalforge_dashboard.data_quality import PRIVATE_HEADERS
from vitalforge_dashboard.goals import list_goals
from vitalforge_dashboard.weekly_review import weekly_review


async def _account_goals(request: Request) -> list[dict]:
    """Goals belong to the account, not the profile: no account identity means no goals, never a 401."""
    try:
        identity = await require_account_identity(request)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        return []
    if identity.user_id is None:  # require_account_identity 401s first; this narrows the type
        return []
    return await list_goals(identity.user_id)


def add_weekly_review_routes(app: FastAPI) -> None:
    @app.get("/p/{slug}/api/weekly-review")
    async def weekly_review_route(
        request: Request,
        person_id: int = Depends(require_person("view")),
    ) -> JSONResponse:
        report = await weekly_review(person_id, await _account_goals(request))
        return JSONResponse(report, headers=PRIVATE_HEADERS)
