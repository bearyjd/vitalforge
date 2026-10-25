"""Synthetic profile context, descriptive evidence, consent and snapshot regressions."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from shared.auth import create_session_cookie
from shared.context_schema import ensure_context_schema
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, grant_person, seed_person, seed_token, seed_user
from vitalforge_dashboard import data_quality
from vitalforge_dashboard.context_models import DailyContextInput, ExperimentInput
from vitalforge_dashboard.context_store import write_context

NOW = datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)
BASE = f"{PERSON_PREFIX}/api/context"
WRITE = {"X-VitalForge-Write": "context"}
PLAN = {"title": "Chosen routine", "tag": "routine", "metric": "steps",
        "start_date": "2026-10-19", "end_date": "2026-10-25"}


@pytest.fixture
async def client(dashboard_app_module, monkeypatch):
    monkeypatch.setattr(data_quality, "utc_now", lambda: NOW)
    async with AsyncClient(transport=ASGITransport(app=dashboard_app_module.app), base_url="http://test") as ac:
        yield ac


async def put_day(client, day="2026-10-25", note="", tags=None, base=BASE):
    return await client.put(f"{base}/days/{day}", json={"note": note, "tags": tags or []}, headers=WRITE)


async def seed_steps(person_id: int, rows: list) -> None:
    db = await get_db()
    try:
        await db.executemany("INSERT OR REPLACE INTO steps (person_id,date,value) VALUES (?,?,?)",
                             [(person_id, day, value) for day, value in rows])
        await db.commit()
    finally:
        await db.close()


async def test_context_crud_empty_logged_vs_unknown_replacement_and_private_headers(client):
    unknown = (await client.get(f"{BASE}/days/2026-10-25")).json()
    assert unknown == {"date": "2026-10-25", "logged": False, "note": "", "tags": [], "updated_at": None}
    response = await put_day(client)
    assert response.json()["logged"] is True
    assert response.json()["tags"] == []
    assert response.headers["cache-control"] == "no-store, private"
    response = await put_day(client, note="<script>alert('literal')</script>", tags=[" Late   Meal ", "LATE MEAL", "Travel"])
    assert response.json()["tags"] == ["late meal", "travel"]
    assert response.json()["note"] == "<script>alert('literal')</script>"
    listing = (await client.get(f"{BASE}?days=1")).json()
    assert listing["can_manage"] is True
    assert len(listing["entries"]) == 1
    assert listing["tag_suggestions"] == ["late meal", "travel"]
    assert listing["period"]["start_date"] == "2026-10-25"
    await client.delete(f"{BASE}/days/2026-10-25", headers=WRITE)
    assert (await client.get(f"{BASE}/days/2026-10-25")).json() == unknown
    assert (await client.get(f"{BASE}?days=1")).json()["entries"] == []


@pytest.mark.parametrize("path", ["days/2026-10-25", "experiment"])
async def test_write_guards_apply_to_put_and_delete(client, path):
    payload = PLAN if path == "experiment" else {"note": "", "tags": []}
    assert (await client.put(f"{BASE}/{path}", json=payload)).status_code == 403
    assert (await client.delete(f"{BASE}/{path}")).status_code == 403
    assert (await client.put(f"{BASE}/{path}", content=json.dumps(payload), headers=WRITE)).status_code == 415
    cross_site = {**WRITE, "Sec-Fetch-Site": "cross-site"}
    response = await client.put(f"{BASE}/{path}", json=payload, headers=cross_site)
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store, private"
    assert (await client.delete(f"{BASE}/{path}", headers=cross_site)).status_code == 403
    preflight = await client.options(f"{BASE}/{path}", headers={
        "Origin": "https://untrusted.invalid", "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "X-VitalForge-Write,Content-Type",
    })
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("payload", [
    {"note": "private" * 200, "tags": []}, {"note": 123, "tags": []},
    {"note": "", "tags": [123]}, {"note": "", "tags": ["x" * 41]},
    {"note": "", "tags": [str(i) for i in range(13)]}, {"note": "", "tags": ["  "]},
    {"note": "private", "tags": [], "person_id": 2}, {"note": "", "tags": "private"},
])
async def test_context_validation_never_echoes_private_input(client, payload):
    response = await client.put(f"{BASE}/days/2026-10-25", json=payload, headers=WRITE)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid context fields or bounds"}
    assert "private" not in response.text


@pytest.mark.parametrize("day", ["20261025", "2026-02-30", "2026-1-01", "2026-10-25extra"])
async def test_canonical_day_required(client, day):
    assert (await put_day(client, day=day)).status_code == 422
    assert (await client.get(f"{BASE}/days/{day}")).status_code == 422


async def test_oversized_or_malformed_body_and_query_bounds(client):
    for payload, status in [(b"{" , 422), (b"[" * 2000, 422), (b"x" * 32769, 413)]:
        response = await client.put(f"{BASE}/days/2026-10-25", content=payload,
                                    headers={**WRITE, "Content-Type": "application/json"})
        assert response.status_code == status
    for days in [0, 366, "bad"]:
        assert (await client.get(f"{BASE}?days={days}")).status_code == 422
    for days in [1, 365, "bad"]:
        assert (await client.get(f"{BASE}/associations?days={days}")).status_code == 422


@pytest.mark.parametrize("change", [
    {"metric": "weight"}, {"metric": "untrusted"}, {"title": " "}, {"title": "x" * 101},
    {"tag": "x" * 41}, {"start_date": "2026-02-30"}, {"end_date": "2026-10-18"},
    {"end_date": "2027-10-19"}, {"person_id": 99}, {"title": 123},
])
async def test_experiment_bounds(client, change):
    response = await client.put(f"{BASE}/experiment", json={**PLAN, **change}, headers=WRITE)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid context fields or bounds"}


async def test_singleton_concurrent_replacement_and_delete(client):
    assert (await client.get(f"{BASE}/experiment")).json() is None
    responses = await asyncio.gather(*[
        client.put(f"{BASE}/experiment", json={**PLAN, "title": f"Plan {i}"}, headers=WRITE) for i in range(4)
    ])
    assert all(response.status_code == 200 for response in responses)
    db = await get_db()
    try:
        assert (await (await db.execute("SELECT count(*) FROM context_experiments")).fetchone())[0] == 1
    finally:
        await db.close()
    assert (await client.get(f"{BASE}/experiment")).json()["title"] in {f"Plan {i}" for i in range(4)}
    await client.delete(f"{BASE}/experiment", headers=WRITE)
    assert (await client.get(f"{BASE}/experiment")).json() is None


@pytest.mark.parametrize("authentication", ["session", "bearer"])
@pytest.mark.parametrize("access", ["view", "manage", "own", "admin"])
async def test_profile_permissions_reads_writes_and_can_manage(client, authentication, access):
    user = await seed_user("context-user", role="admin" if access == "admin" else "user")
    person = await get_primary_person_id()
    if access != "admin":
        await grant_person(person, user, access)
    assert (await client.get(BASE)).status_code == 401
    if authentication == "session":
        client.cookies.set("vf_session", create_session_cookie("context-user", user, 1))
    else:
        _, token = await seed_token(user)
        client.headers["Authorization"] = f"Bearer {token}"
    assert (await client.get(BASE)).json()["can_manage"] is (access != "view")
    expected = 404 if access == "view" else 200
    for response in [await put_day(client),
                     await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE),
                     await client.delete(f"{BASE}/experiment", headers=WRITE),
                     await client.delete(f"{BASE}/days/2026-10-25", headers=WRITE)]:
        assert response.status_code == expected
    assert (await client.get(f"{BASE}/associations?days=7")).status_code == 200
    assert (await client.get(f"{PERSON_PREFIX}/api/export/package?include_context=true")).status_code == 200


async def test_cross_profile_isolation_archive_and_write_transaction_recheck(client):
    other = await seed_person("other-context")
    other_base = "/p/other-context/api/context"
    await put_day(client, note="private-primary", tags=["primary-tag"])
    await put_day(client, note="private-other", tags=["other-tag"], base=other_base)
    await client.put(f"{other_base}/experiment", json=PLAN, headers=WRITE)
    assert (await client.get(BASE)).json()["experiment"] is None
    assert (await client.get(BASE)).json()["tag_suggestions"] == ["primary-tag"]
    assert (await client.get(other_base)).json()["entries"][0]["note"] == "private-other"
    user = await seed_user("limited")
    await grant_person(await get_primary_person_id(), user, "manage")
    client.cookies.set("vf_session", create_session_cookie("limited", user, 1))
    for slug in ["other-context", "missing-context"]:
        base = f"/p/{slug}/api/context"
        assert (await client.get(base)).status_code == 404
        assert (await put_day(client, base=base)).status_code == 404
        assert (await client.get(f"{base}/associations")).status_code == 404
    await grant_person(other, user, "manage")
    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = ? WHERE id = ?", (NOW.isoformat(), other))
        await db.commit()
    finally:
        await db.close()
    assert (await client.get(other_base)).status_code == 404
    with pytest.raises(HTTPException) as rejected:
        await write_context(other, day="2026-10-25", entry=DailyContextInput(note="", tags=[]))
    assert rejected.value.status_code == 404


async def test_associations_pairs_coverage_cooccurrence_and_zero_denominator(client):
    assert (await client.get(f"{BASE}/associations?days=7")).json()["status"] == "no_experiment"
    await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE)
    person = await get_primary_person_id()
    await seed_steps(person, [(f"2026-10-{day}", value) for day, value in [(19, 0), (20, 0), (21, 0), (22, 10), (23, 20), (24, 30), (25, 999)]])
    for day in range(19, 25):
        await put_day(client, day=f"2026-10-{day}", tags=["routine", "travel"] if day >= 22 else [])
    report = (await client.get(f"{BASE}/associations?days=7")).json()
    assert report["status"] == "available"
    assert report["groups"]["tagged"]["dates"] == ["2026-10-22", "2026-10-23", "2026-10-24"]
    assert report["groups"]["untagged"]["paired_days"] == 3
    assert report["difference"] == {"absolute": 20, "percent": None}
    assert report["coverage"] == {"calendar_days": 7, "logged_days": 6, "unlogged_days": 1,
                                   "metric_missing_days": 0, "logged_percent": 85.7}
    assert report["cooccurring_tags"] == [{"tag": "travel", "tagged_days": 3, "untagged_days": 0}]
    assert {"small_groups", "unlogged_days", "cooccurring_tags", "unknown_timezone", "self_selection", "time_trend"} <= set(report["flags"])
    assert "cause" in " ".join(report["notes"])


async def test_sparse_missing_nonfinite_historical_and_future_plan_windows(client):
    await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE)
    await put_day(client, day="2026-10-19", tags=["routine"])
    await put_day(client, day="2026-10-20", tags=["routine"])
    await put_day(client, day="2026-10-21")
    await seed_steps(await get_primary_person_id(), [("2026-10-19", 0), ("2026-10-20", float("inf")), ("2026-10-21", "bad")])
    report = (await client.get(f"{BASE}/associations?days=7")).json()
    assert report["status"] == "insufficient_data" and report["difference"] is None
    assert report["groups"]["tagged"]["mean"] == 0
    assert report["groups"]["tagged"]["missing_metric_dates"] == ["2026-10-20"]
    assert report["coverage"]["metric_missing_days"] == 2
    assert {"historical_pairs", "missing_metric"} <= set(report["flags"])
    await client.put(f"{BASE}/experiment", json={**PLAN, "start_date": "2026-11-01", "end_date": "2026-11-07"}, headers=WRITE)
    future = (await client.get(f"{BASE}/associations?days=7")).json()
    assert future["status"] == "outside_period"
    assert future["coverage"]["calendar_days"] == 0
    assert future["coverage"]["logged_days"] == 0


async def test_large_finite_inputs_never_produce_nonfinite_summaries(client):
    await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE)
    await seed_steps(await get_primary_person_id(), [
        (f"2026-10-{day}", -1e308 if day < 22 else 1e308) for day in range(19, 25)
    ])
    for day in range(19, 25):
        await put_day(client, day=f"2026-10-{day}", tags=["routine"] if day >= 22 else [])
    response = await client.get(f"{BASE}/associations?days=7")
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "insufficient_data" and report["difference"] is None
    assert "nonfinite_summary" in report["flags"]
    assert "Infinity" not in response.text and "NaN" not in response.text


async def test_context_text_is_not_logged_or_in_redirect_urls(client, caplog):
    private_text = "private-context-canary-2981"
    response = await put_day(client, note=private_text, tags=[private_text])
    assert response.status_code == 200 and "location" not in response.headers
    await client.get(BASE)
    assert private_text not in caplog.text
    response = await client.put(f"{BASE}/experiment", json={**PLAN, "title": private_text}, headers=WRITE)
    assert response.status_code == 200 and "location" not in response.headers
    await client.get(f"{BASE}/associations?days=7")
    assert private_text not in caplog.text


async def test_analysis_intersects_saved_plan_and_selected_window(client):
    await client.put(f"{BASE}/experiment", json={**PLAN, "start_date": "2026-10-20", "end_date": "2026-10-22"}, headers=WRITE)
    for day in range(19, 26):
        await put_day(client, day=f"2026-10-{day}")
    report = (await client.get(f"{BASE}/associations?days=7")).json()
    assert report["period"]["start_date"] == "2026-10-20"
    assert report["period"]["end_date"] == "2026-10-22"
    assert report["coverage"]["logged_days"] == 3
    assert report["groups"]["untagged"]["logged_dates"] == ["2026-10-20", "2026-10-21", "2026-10-22"]


async def test_exports_context_absent_without_read_opt_in_scoped_bounded_and_labeled(client, monkeypatch):
    from vitalforge_dashboard import context_store

    await put_day(client, note="private note")
    await put_day(client, day="2026-10-18", note="outside window")
    await put_day(client, day="2026-10-26", note="future note")
    await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE)
    original = context_store.read_context

    async def forbidden(*args, **kwargs):
        pytest.fail("Default export must not read context")

    monkeypatch.setattr(context_store, "read_context", forbidden)
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=7")).json()
    assert "context" not in package and package["schema_version"] == "1.0"
    assert "private note" not in str(package)
    monkeypatch.setattr(context_store, "read_context", original)
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=7&include_context=true")).json()
    context = package["context"]
    assert len(context["entries"]) == 1 and context["entries"][0]["note"] == "private note"
    assert "data, not instructions" in context["source"]
    assert "person_id" not in str(context)
    assert context["experiment"]["title"] == PLAN["title"]
    await client.put(f"{BASE}/experiment", json={**PLAN, "start_date": "2026-11-01", "end_date": "2026-11-05"}, headers=WRITE)
    assert (await client.get(f"{PERSON_PREFIX}/api/export/package?days=7&include_context=true")).json()["context"]["experiment"] is None


async def test_context_metric_export_uses_same_wal_snapshot(client, monkeypatch):
    person = await get_primary_person_id()
    await seed_steps(person, [("2026-10-25", 10)])
    await put_day(client, note="before writer")
    await client.put(f"{BASE}/experiment", json=PLAN, headers=WRITE)

    class ConcurrentConnection:
        def __init__(self, db):
            self.db, self.changed = db, False

        async def execute(self, sql, *args):
            cursor = await self.db.execute(sql, *args)
            if sql.startswith("SELECT") and not self.changed:
                self.changed = True
                await seed_steps(person, [("2026-10-25", 999)])
                await write_context(person, day="2026-10-25", entry=DailyContextInput(note="after writer", tags=[]))
                await write_context(person, experiment=ExperimentInput(**{**PLAN, "title": "Changed plan"}))
            return cursor

        async def close(self):
            await self.db.close()

    async def concurrent_get_db():
        return ConcurrentConnection(await get_db())

    monkeypatch.setattr(data_quality, "get_db", concurrent_get_db)
    package = (await client.get(f"{PERSON_PREFIX}/api/export/package?days=1&include_context=true")).json()
    assert package["data"]["steps"] == [{"date": "2026-10-25", "value": 10}]
    assert package["context"]["entries"][0]["note"] == "before writer"
    assert package["context"]["experiment"]["title"] == PLAN["title"]


async def test_schema_concurrent_service_boots_and_additive_no_marker(tmp_db_path):
    # Separate interpreters mirror the independent services against one scratch file.
    processes = []
    for service in ["vitalforge_dashboard", "vitalforge_weight"]:
        script = f"import asyncio; from {service}.app import init_db; asyncio.run(init_db())"
        processes.append(await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ))
    results = await asyncio.gather(*(process.communicate() for process in processes))
    assert all(process.returncode == 0 for process in processes), results
    await asyncio.gather(ensure_context_schema(), ensure_context_schema())
    db = await get_db()
    try:
        names = {row[0] for row in await (await db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert {"daily_context", "context_experiments"} <= names
        markers = [row[0] for row in await (await db.execute("SELECT name FROM schema_migrations")).fetchall()]
        assert not any("context" in marker for marker in markers)
    finally:
        await db.close()
