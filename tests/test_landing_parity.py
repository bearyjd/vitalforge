"""The two services must land the same person in the same place.

Both apps grew a `GET /` that redirects to `/p/{slug}/`, written independently
during the Phase 2 sweep. They diverged: the weight service applied
require_person's admin bypass when computing the reachable set, the dashboard
did not. An admin holding one grant in a three-person household therefore
landed on their person via port 8086 and got a 400 "ambiguous" via 8085 --
same login, same database, same click, different answer depending on which
port was open.

Neither service's tests could see it. Each was internally consistent; only
comparing them shows the bug. Hence this file: the parity IS the requirement,
so it gets a test that fails when the two drift apart rather than a comment in
each asking the reader to remember the other.

Landing is about PREFERENCE, not capability. require_person's admin bypass is
about reaching a person addressed explicitly; using it to widen the landing
set makes the home page 400 for the common admin case. Spec f.2 gives
default_person_id this redirect "and nothing else".
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVICES = ["vitalforge-dashboard/app.py", "vitalforge-weight/app.py"]


def _reachable_persons_source(service: str) -> str:
    tree = ast.parse((REPO / service).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if node.name == "_reachable_persons":
                return ast.get_source_segment((REPO / service).read_text(), node) or ""
    pytest.fail(f"{service} has no _reachable_persons -- the landing rule moved or was renamed")


@pytest.mark.parametrize("service", SERVICES)
def test_landing_set_is_grant_scoped_for_admins(service):
    """The specific divergence that occurred, pinned.

    An admin bypass here reads as consistency with require_person and is not:
    it turns the home page into a 400 for an admin with one grant among
    several persons.
    """
    src = _reachable_persons_source(service)
    tree = ast.parse(ast.unparse(ast.parse(src)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        rendered = ast.unparse(node)
        if "admin" in rendered:
            pytest.fail(
                f"{service}: _reachable_persons branches on {rendered!r}. Landing is scoped "
                "by GRANT for every account-bound caller, admins included -- see this "
                "module's docstring. An admin can still open /p/{slug}/ directly."
            )


@pytest.mark.parametrize("service", SERVICES)
def test_landing_excludes_archived_persons(service):
    """A redirect must never point somewhere require_person will 404.
    require_person excludes archived persons by construction, so a landing
    rule that included them would make / a permanent dead end."""
    src = _reachable_persons_source(service)
    assert "archived_at IS NULL" in src, (
        f"{service}: _reachable_persons does not exclude archived persons; / could redirect "
        "to a slug require_person refuses, with no way out"
    )


@pytest.mark.parametrize("service", SERVICES)
def test_landing_serves_open_access_mode(service):
    """Empty users table is this project's primary dev path (`docker compose
    up` on a fresh volume). The anonymous sentinel has user_id None and must
    reach every active person, since there are no grants to consult."""
    src = _reachable_persons_source(service)
    assert "user_id is None" in src, (
        f"{service}: _reachable_persons has no anonymous branch; open-access mode would land "
        "on an empty set and show the no-persons page on a fresh volume"
    )


@pytest.mark.parametrize("service_module", ["vitalforge-weight.app", "vitalforge-dashboard.app"])
async def test_landing_denies_an_unrecognised_grant_value(
    initialized_db, fake_garmin_client, monkeypatch, service_module
):
    """The last place the two rules disagreed, pinned BEHAVIOURALLY.

    require_person denies an access value outside the three via
    `_ACCESS_ORDER.get(granted, -1)`. Both `_reachable_persons` joined
    person_grants without inspecting `access` at all, so such a grant made
    GET / redirect to a /p/{slug}/ that then 404s -- a dead end with no way
    out. person_grants' CHECK constraint makes the value unreachable today,
    but CHECK constraints are exactly what a future table rebuild relaxes, and
    both docstrings claimed to mirror require_person while not doing so.

    Asserted by driving the two routes rather than by grepping the SQL: a
    source check here reads the query text INCLUDING its comments, so deleting
    the predicate while leaving the words in a comment above it passed. A guard
    a comment can satisfy is not a guard.
    """
    from httpx import ASGITransport, AsyncClient

    from shared.auth import create_session_cookie
    from shared.database import get_db
    from tests.conftest import import_service_module, seed_person, seed_user

    module = import_service_module(service_module)
    monkeypatch.setattr(module, "authenticate", lambda: None)
    if hasattr(module, "scheduled_sync"):

        async def _noop(lock, registry):
            return None

        monkeypatch.setattr(module, "scheduled_sync", _noop)

    person_id = await seed_person("bryn")
    user_id = await seed_user("bob")
    db = await get_db()
    try:
        # The CHECK constraint is the reason this value is unreachable through
        # any route; suspending it is how the test reaches the state a future
        # table rebuild could reintroduce.
        await db.execute("PRAGMA ignore_check_constraints = ON")
        await db.execute(
            "INSERT INTO person_grants (person_id, user_id, access, granted_at) "
            "VALUES (?, ?, 'superuser', '2026-01-01T00:00:00+00:00')",
            (person_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()

    cookies = {"vf_session": create_session_cookie("bob", user_id, 1)}
    transport = ASGITransport(app=module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        landing = await ac.get("/", cookies=cookies, follow_redirects=False)
        # require_person's answer, for comparison: the grant does not
        # authorize. GET /p/{slug}/ is the one person-scoped route both
        # services carry.
        scoped = await ac.get("/p/bryn/", cookies=cookies)

    assert scoped.status_code == 404, (
        f"{service_module}: require_person accepted an unrecognised grant value; this test's "
        "premise is gone and the assertion below proves nothing"
    )
    assert landing.headers.get("location") != "/p/bryn/", (
        f"{service_module}: GET / redirected to a person require_person then refuses -- a dead "
        "end the user cannot clear. _reachable_persons must deny the same grant values "
        "require_person denies."
    )


def test_both_services_agree_on_the_landing_status_codes():
    """302 to the person, 400 when ambiguous, 200 HTML when there is nothing
    to show. A new account awaiting a grant is not a client error, and a
    non-2xx there would invite a service worker cache fallback."""
    for service in SERVICES:
        src = (REPO / service).read_text()
        assert "status_code=302" in src, f"{service}: landing redirect is not a 302"
        assert "status_code=400" in src, f"{service}: no ambiguous-landing 400"
