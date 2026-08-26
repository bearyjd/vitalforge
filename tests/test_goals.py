"""Goal / target tracking: ownership matrix over `/api/goals*`, plus unit
coverage of `goals.compute_progress`'s trend-based ETA.

Ownership matrix: owner can CRUD their own goal, a non-owner gets 403, an
admin can override a non-owner's 403, an unauthenticated caller gets 401,
and an unknown id gets 404 -- mirrors test_api_tokens.py's pattern for
`shared/auth.py`'s existing token-ownership routes.

`goals.py` is a sibling module of `vitalforge-dashboard/app.py` and needs
that module's own `sys.path.insert` (for its bare `from recommendations
import ...`) to have already run before it's importable — every test below
therefore depends on `dashboard_app_module` (which imports `app.py`) even
when only exercising `goals.compute_progress` directly, and imports `goals`
lazily inside the test body rather than at module top level.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from shared import auth as shared_auth
from shared.auth import create_session_cookie
from shared.database import get_db
from tests.conftest import seed_user


@pytest.fixture
async def client(dashboard_app_module):
    transport = ASGITransport(app=dashboard_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _cookies_for(username: str) -> dict[str, str]:
    user_id, session_version = await shared_auth._get_user_id_and_session_version(username)
    return {"vf_session": create_session_cookie(username, user_id, session_version)}


async def _goal_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM goals")).fetchone())[0]
    finally:
        await db.close()


async def seed_metric(table: str, column: str, rows: list[tuple[str, float]]):
    db = await get_db()
    try:
        for date, value in rows:
            await db.execute(
                f"INSERT OR REPLACE INTO [{table}] (date, [{column}]) VALUES (?, ?)",
                (date, value),
            )
        await db.commit()
    finally:
        await db.close()


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Ownership matrix
# ---------------------------------------------------------------------------


async def test_owner_can_crud_own_goal(client):
    await seed_user("alice", password="alice-pw")
    cookies = await _cookies_for("alice")

    create_resp = await client.post(
        "/api/goals",
        json={"metric": "steps", "target_value": 10000, "target_date": "2026-12-31"},
        cookies=cookies,
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["metric"] == "steps"
    assert body["target_value"] == 10000
    assert body["target_date"] == "2026-12-31"
    assert body["progress"]["latest_value"] is None  # no synced data yet
    goal_id = body["id"]

    list_resp = await client.get("/api/goals", cookies=cookies)
    assert list_resp.status_code == 200
    assert [g["id"] for g in list_resp.json()] == [goal_id]

    get_resp = await client.get(f"/api/goals/{goal_id}", cookies=cookies)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == goal_id

    patch_resp = await client.patch(f"/api/goals/{goal_id}", json={"target_value": 12000}, cookies=cookies)
    assert patch_resp.status_code == 200
    assert patch_resp.json()["target_value"] == 12000
    # target_date left untouched by the partial update.
    assert patch_resp.json()["target_date"] == "2026-12-31"

    delete_resp = await client.delete(f"/api/goals/{goal_id}", cookies=cookies)
    assert delete_resp.status_code == 200
    assert await _goal_count() == 0


async def test_non_owner_gets_403(client):
    await seed_user("alice", password="alice-pw")
    await seed_user("bob", password="bob-pw")
    create_resp = await client.post(
        "/api/goals",
        json={"metric": "resting_hr", "target_value": 55},
        cookies=await _cookies_for("alice"),
    )
    goal_id = create_resp.json()["id"]

    bob_cookies = await _cookies_for("bob")
    assert (await client.get(f"/api/goals/{goal_id}", cookies=bob_cookies)).status_code == 403
    assert (
        await client.patch(f"/api/goals/{goal_id}", json={"target_value": 50}, cookies=bob_cookies)
    ).status_code == 403
    assert (await client.delete(f"/api/goals/{goal_id}", cookies=bob_cookies)).status_code == 403
    assert await _goal_count() == 1


async def test_admin_can_override(client):
    await seed_user("root", password="root-pw", role="admin")
    await seed_user("bob", password="bob-pw")
    create_resp = await client.post(
        "/api/goals",
        json={"metric": "resting_hr", "target_value": 55},
        cookies=await _cookies_for("bob"),
    )
    goal_id = create_resp.json()["id"]

    admin_cookies = await _cookies_for("root")
    assert (await client.get(f"/api/goals/{goal_id}", cookies=admin_cookies)).status_code == 200

    patch_resp = await client.patch(f"/api/goals/{goal_id}", json={"target_value": 50}, cookies=admin_cookies)
    assert patch_resp.status_code == 200
    assert patch_resp.json()["target_value"] == 50

    delete_resp = await client.delete(f"/api/goals/{goal_id}", cookies=admin_cookies)
    assert delete_resp.status_code == 200
    assert await _goal_count() == 0


async def test_unauthenticated_gets_401(client):
    # Seed a user first so auth is actually enabled -- an empty users table
    # makes every request anonymous instead, which would also 401 but for
    # the wrong reason (see test_dev_mode_no_users_returns_401 below for
    # that separate, expected case).
    await seed_user("alice", password="alice-pw")
    assert (await client.get("/api/goals")).status_code == 401
    assert (await client.post("/api/goals", json={"metric": "steps", "target_value": 1})).status_code == 401
    assert (await client.get("/api/goals/1")).status_code == 401
    assert (await client.patch("/api/goals/1", json={"target_value": 1})).status_code == 401
    assert (await client.delete("/api/goals/1")).status_code == 401


async def test_unknown_id_returns_404(client):
    await seed_user("alice", password="alice-pw")
    resp = await client.get("/api/goals/999999", cookies=await _cookies_for("alice"))
    assert resp.status_code == 404
    assert (
        await client.patch("/api/goals/999999", json={"target_value": 1}, cookies=await _cookies_for("alice"))
    ).status_code == 404
    assert (await client.delete("/api/goals/999999", cookies=await _cookies_for("alice"))).status_code == 404


async def test_dev_mode_no_users_returns_401_for_goals(client):
    # Empty users table -> auth is disabled ("anonymous") for every other
    # dashboard endpoint, but a goal always belongs to a real user_id, so
    # goal endpoints still 401 in this mode. Expected, documented in the PR
    # description, and pinned here so it can't silently regress.
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/goals")).status_code == 401


async def test_create_goal_rejects_unknown_metric(client):
    await seed_user("alice", password="alice-pw")
    resp = await client.post(
        "/api/goals",
        json={"metric": "not-a-real-metric", "target_value": 1},
        cookies=await _cookies_for("alice"),
    )
    assert resp.status_code == 422
    assert await _goal_count() == 0


async def test_patch_rejects_unknown_metric(client):
    await seed_user("alice", password="alice-pw")
    cookies = await _cookies_for("alice")
    create_resp = await client.post("/api/goals", json={"metric": "steps", "target_value": 1000}, cookies=cookies)
    goal_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/goals/{goal_id}", json={"metric": "not-a-real-metric"}, cookies=cookies)
    assert resp.status_code == 422
    # Rejected metric never landed.
    unchanged = await client.get(f"/api/goals/{goal_id}", cookies=cookies)
    assert unchanged.json()["metric"] == "steps"


async def test_users_only_list_their_own_goals(client):
    await seed_user("alice", password="alice-pw")
    await seed_user("bob", password="bob-pw")
    await client.post(
        "/api/goals", json={"metric": "steps", "target_value": 1}, cookies=await _cookies_for("alice")
    )
    await client.post(
        "/api/goals", json={"metric": "resting_hr", "target_value": 2}, cookies=await _cookies_for("bob")
    )

    resp = await client.get("/api/goals", cookies=await _cookies_for("alice"))
    assert resp.status_code == 200
    assert [g["metric"] for g in resp.json()] == ["steps"]


# ---------------------------------------------------------------------------
# compute_progress ETA
# ---------------------------------------------------------------------------


async def test_compute_progress_projects_eta_when_trending_toward_target(dashboard_app_module):
    import goals

    rows = [(days_ago(4 - i), 100.0 * (i + 1)) for i in range(5)]  # 100, 200, ..., 500 ascending
    await seed_metric("steps", "value", rows)

    progress = await goals.compute_progress("steps", "value", target_value=1000, target_date=None)
    assert progress.latest_value == 500.0
    assert progress.trend_slope is not None
    assert progress.trend_slope > 0
    assert progress.eta_date is not None
    assert progress.on_track is True


async def test_compute_progress_no_eta_when_trending_away_from_target(dashboard_app_module):
    import goals

    rows = [(days_ago(4 - i), 500.0 - 100.0 * i) for i in range(5)]  # 500, 400, ..., 100 descending
    await seed_metric("steps", "value", rows)

    progress = await goals.compute_progress("steps", "value", target_value=1000, target_date=None)
    assert progress.trend_slope is not None
    assert progress.trend_slope < 0
    assert progress.eta_date is None
    assert progress.on_track is None


async def test_compute_progress_insufficient_data_returns_none_slope(dashboard_app_module):
    import goals

    await seed_metric("steps", "value", [(days_ago(0), 100.0)])
    progress = await goals.compute_progress("steps", "value", target_value=1000, target_date=None)
    assert progress.latest_value == 100.0
    assert progress.trend_slope is None
    assert progress.eta_date is None
    assert progress.on_track is None


async def test_compute_progress_already_at_target(dashboard_app_module):
    import goals

    rows = [(days_ago(4 - i), 1000.0) for i in range(5)]
    await seed_metric("steps", "value", rows)

    progress = await goals.compute_progress("steps", "value", target_value=1000, target_date=None)
    assert progress.eta_date == days_ago(0)
    assert progress.on_track is True


async def test_compute_progress_on_track_relative_to_target_date(dashboard_app_module):
    import goals

    rows = [(days_ago(4 - i), 100.0 * (i + 1)) for i in range(5)]  # +100/day
    await seed_metric("steps", "value", rows)

    # A generous target_date far in the future should be "on track"; an
    # impossibly close one should not.
    generous = await goals.compute_progress(
        "steps", "value", target_value=1000, target_date=days_ago(-365)
    )
    assert generous.eta_date is not None
    assert generous.on_track is True

    impossible = await goals.compute_progress("steps", "value", target_value=1000, target_date=days_ago(1))
    assert impossible.eta_date is not None
    assert impossible.on_track is False


async def test_compute_progress_no_data_returns_all_none(dashboard_app_module):
    import goals

    progress = await goals.compute_progress("steps", "value", target_value=1000, target_date=None)
    assert progress.latest_value is None
    assert progress.trend_slope is None
    assert progress.eta_date is None
    assert progress.on_track is None
