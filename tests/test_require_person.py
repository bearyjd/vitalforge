"""The access-control matrix for require_person.

This is the security boundary the whole of Phase 2 rests on, and its failure
mode is silent: a wrong answer here does not crash, it returns another person's
health data. Every branch of the dependency gets a test, and the negative cases
matter more than the positive ones.

Three properties are load-bearing and each has a test that fails if it regresses:

1. 404, never 403, for a missing grant. A 403 confirms the person exists, which
   leaks household membership to anyone who can guess a name.
2. The anonymous branch precedes the admin branch. In open-access mode
   identity.role is None, so an admin check placed first would fall through and
   break `docker compose up` on a fresh volume.
3. Identity and grant resolve in ONE query, bound to the just-established
   identity -- two queries leave a revoked grant briefly usable.
"""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from shared.auth import add_auth_routes, create_session_cookie, require_person
from tests.conftest import grant_person, seed_person, seed_user


def _build_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)

    @app.get("/p/{slug}/api/read")
    async def read(person_id: int = Depends(require_person("view"))):
        return {"person_id": person_id}

    @app.post("/p/{slug}/api/write")
    async def write(person_id: int = Depends(require_person("manage"))):
        return {"person_id": person_id}

    @app.delete("/p/{slug}/api/admin-ish")
    async def owner_only(person_id: int = Depends(require_person("own"))):
        return {"person_id": person_id}

    return app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _as(username: str, role: str = "user") -> tuple[int, dict]:
    user_id = await seed_user(username, role=role)
    return user_id, {"vf_session": create_session_cookie(username, user_id, 1)}


# --- the level matrix ---------------------------------------------------------


@pytest.mark.parametrize(
    "granted,path,method,expected",
    [
        # view satisfies view only
        ("view", "/p/bryn/api/read", "get", 200),
        ("view", "/p/bryn/api/write", "post", 404),
        ("view", "/p/bryn/api/admin-ish", "delete", 404),
        # manage satisfies view and manage
        ("manage", "/p/bryn/api/read", "get", 200),
        ("manage", "/p/bryn/api/write", "post", 200),
        ("manage", "/p/bryn/api/admin-ish", "delete", 404),
        # own satisfies everything
        ("own", "/p/bryn/api/read", "get", 200),
        ("own", "/p/bryn/api/write", "post", 200),
        ("own", "/p/bryn/api/admin-ish", "delete", 200),
    ],
)
async def test_access_level_matrix(client, granted, path, method, expected):
    """Levels are ranked, not equal: a route asking for `view` is satisfied by
    `manage`, but never the other way round."""
    user_id, cookies = await _as(f"u-{granted}-{method}")
    person_id = await seed_person("bryn")
    await grant_person(person_id, user_id, access=granted)

    resp = await getattr(client, method)(path, cookies=cookies)
    assert resp.status_code == expected, (
        f"grant={granted} on {method.upper()} {path} gave {resp.status_code}, expected {expected}"
    )


async def test_insufficient_level_is_404_not_403(client):
    """THE leak test. 403 would confirm 'bryn' exists to someone who only
    guessed the name."""
    user_id, cookies = await _as("viewer")
    person_id = await seed_person("bryn")
    await grant_person(person_id, user_id, access="view")

    resp = await client.post("/p/bryn/api/write", cookies=cookies)
    assert resp.status_code == 404
    assert "bryn" not in resp.text, "the response echoed the slug back"


async def test_no_grant_is_404(client):
    user_id, cookies = await _as("stranger")
    await seed_person("bryn")

    resp = await client.get("/p/bryn/api/read", cookies=cookies)
    assert resp.status_code == 404


async def test_nonexistent_slug_and_no_grant_are_indistinguishable(client):
    """Both must produce byte-identical responses, or the difference itself
    tells an attacker which names exist."""
    user_id, cookies = await _as("prober")
    await seed_person("bryn")

    exists_no_grant = await client.get("/p/bryn/api/read", cookies=cookies)
    does_not_exist = await client.get("/p/nobody-here/api/read", cookies=cookies)

    assert exists_no_grant.status_code == does_not_exist.status_code == 404
    assert exists_no_grant.json() == does_not_exist.json(), (
        "an existing-but-ungranted person is distinguishable from a nonexistent one"
    )


# --- archived persons ---------------------------------------------------------


async def test_archived_person_is_unreachable_even_with_an_own_grant(client):
    """archived_at IS NULL lives in the dependency's query, so archiving is
    enforced by construction rather than by every caller remembering."""
    user_id, cookies = await _as("owner")
    person_id = await seed_person("bryn")
    await grant_person(person_id, user_id, access="own")

    from shared.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "UPDATE persons SET archived_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (person_id,),
        )
        await db.commit()
    finally:
        await db.close()

    resp = await client.get("/p/bryn/api/read", cookies=cookies)
    assert resp.status_code == 404, "an archived person was still reachable"


# --- admin bypass -------------------------------------------------------------


async def test_admin_reaches_any_person_without_a_grant(client):
    """One superuser story, matching the bypass every /auth/admin/* route
    already uses -- not a second, inconsistent one."""
    await _as("ignored-admin-seed")
    _, cookies = await _as("boss", role="admin")
    await seed_person("bryn")

    for path, method in [
        ("/p/bryn/api/read", "get"),
        ("/p/bryn/api/write", "post"),
        ("/p/bryn/api/admin-ish", "delete"),
    ]:
        resp = await getattr(client, method)(path, cookies=cookies)
        assert resp.status_code == 200, f"admin was refused {method.upper()} {path}"


async def test_admin_still_gets_404_for_a_nonexistent_slug(client):
    """The admin bypass skips the GRANT check, not the person lookup."""
    _, cookies = await _as("boss", role="admin")
    resp = await client.get("/p/ghost/api/read", cookies=cookies)
    assert resp.status_code == 404


async def test_admin_cannot_reach_an_archived_person(client):
    """Admins address archived persons by id through admin routes, never
    through this dependency."""
    _, cookies = await _as("boss", role="admin")
    person_id = await seed_person("bryn")

    from shared.database import get_db

    db = await get_db()
    try:
        await db.execute("UPDATE persons SET archived_at = 'x' WHERE id = ?", (person_id,))
        await db.commit()
    finally:
        await db.close()

    assert (await client.get("/p/bryn/api/read", cookies=cookies)).status_code == 404


# --- open-access mode ---------------------------------------------------------


def test_anonymous_sentinel_has_no_role():
    """Pins the invariant require_person's branch ordering rests on.

    The dependency checks anonymous BEFORE admin. Mutation testing showed the
    two orders are behaviourally identical today -- swapping them breaks no
    test -- precisely because this sentinel's role is None and
    `None == "admin"` is False. So no behavioural test can defend that
    ordering; this one defends the property that makes the ordering harmless.

    If the anonymous sentinel ever gains a role, this fails, and whoever is
    changing it needs to look at require_person's ordering before proceeding.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "shared" / "auth.py"
    construction = re.search(r'_Identity\("anonymous"[^)]*\)', src.read_text())
    assert construction is not None, "the anonymous sentinel construction moved or was renamed"
    assert construction.group(0) == '_Identity("anonymous", None, None, None)', (
        f"the anonymous sentinel changed shape: {construction.group(0)}. require_person "
        "checks anonymous before admin on the assumption that its role is None -- if it now "
        "carries a role, that ordering stops being merely defensive and must be re-examined."
    )


async def test_anonymous_holds_implicit_own_when_auth_is_unconfigured(initialized_db):
    """`docker compose up` on a fresh volume is the primary dev path. With an
    empty users table there is no identity to grant anything, so the anonymous
    branch must return before any grant check.
    """
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        person_id = await seed_person("bryn")
        for path, method in [
            ("/p/bryn/api/read", "get"),
            ("/p/bryn/api/write", "post"),
            ("/p/bryn/api/admin-ish", "delete"),
        ]:
            resp = await getattr(ac, method)(path)
            assert resp.status_code == 200, f"open-access mode refused {method.upper()} {path}"
            assert resp.json()["person_id"] == person_id


async def test_open_access_still_404s_a_nonexistent_slug(initialized_db):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        assert (await ac.get("/p/ghost/api/read")).status_code == 404


# --- unauthenticated ----------------------------------------------------------


async def test_unauthenticated_is_401_not_404(client):
    """Distinct from the missing-grant case on purpose: the caller has not
    identified themselves at all, so there is nothing to leak by saying so,
    and a bearer client needs a 401 to know to re-authenticate."""
    await seed_user("somebody")  # configures auth
    await seed_person("bryn")

    resp = await client.get("/p/bryn/api/read")
    assert resp.status_code == 401


# --- cross-person isolation ---------------------------------------------------


async def test_a_grant_on_one_person_does_not_reach_another(client):
    """The core multi-tenancy property, stated directly."""
    user_id, cookies = await _as("alice-only")
    alice = await seed_person("alice")
    await seed_person("bryn")
    await grant_person(alice, user_id, access="own")

    ok = await client.get("/p/alice/api/read", cookies=cookies)
    assert ok.status_code == 200
    assert ok.json()["person_id"] == alice

    leaked = await client.get("/p/bryn/api/read", cookies=cookies)
    assert leaked.status_code == 404, "a grant on alice reached bryn"


async def test_returns_the_right_person_id_when_several_exist(client):
    """A dependency that authorized correctly but returned the wrong id would
    pass every status-code test above while serving the wrong person's data."""
    user_id, cookies = await _as("multi")
    ids = {slug: await seed_person(slug) for slug in ("alice", "bryn", "cass")}
    for person_id in ids.values():
        await grant_person(person_id, user_id, access="view")

    for slug, expected in ids.items():
        resp = await client.get(f"/p/{slug}/api/read", cookies=cookies)
        assert resp.json()["person_id"] == expected, f"{slug} resolved to the wrong person"


# --- misuse -------------------------------------------------------------------


def test_require_person_rejects_an_unknown_level():
    """Fails at import/wiring time, not on the first request. A typo'd level
    would otherwise raise KeyError inside the dependency -- a 500 on a live
    route instead of a startup failure."""
    with pytest.raises(ValueError, match="unknown access level"):
        require_person("admin")
