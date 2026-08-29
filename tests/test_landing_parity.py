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


@pytest.mark.parametrize("service", SERVICES)
def test_landing_denies_an_unrecognised_grant_value(service):
    """The last place the two rules disagreed.

    require_person denies an access value outside the three via
    `_ACCESS_ORDER.get(granted, -1)`. Both `_reachable_persons` joined
    person_grants without inspecting `access` at all, so such a grant made
    GET / redirect to a /p/{slug}/ that then 404s -- a dead end with no way
    out. person_grants' CHECK constraint makes the value unreachable today,
    but CHECK constraints are exactly what table rebuilds relax, and both
    docstrings claimed to mirror require_person while not doing so.
    """
    src = _reachable_persons_source(service)
    assert "access IN ('view', 'manage', 'own')" in src, (
        f"{service}: _reachable_persons accepts any person_grants.access value, while "
        "require_person denies anything outside the three levels. The landing rule would "
        "redirect to a person the dependency then refuses."
    )


def test_both_services_agree_on_the_landing_status_codes():
    """302 to the person, 400 when ambiguous, 200 HTML when there is nothing
    to show. A new account awaiting a grant is not a client error, and a
    non-2xx there would invite a service worker cache fallback."""
    for service in SERVICES:
        src = (REPO / service).read_text()
        assert "status_code=302" in src, f"{service}: landing redirect is not a 302"
        assert "status_code=400" in src, f"{service}: no ambiguous-landing 400"
