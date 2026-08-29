"""The person-collection admin surface: CRUD, archiving, and grants.

Two properties here are security boundaries rather than features, and each has
a test whose failure mode was checked by mutation rather than assumed:

1. **Grant routes answer 404, never 403, to a caller who is neither an admin
   nor an `own` holder.** These routes address a person BY ID, so a 403 would
   confirm that person exists to anyone logged in who can count -- the same
   household-membership leak plan constraint 2 closes for `/p/{slug}/`.
2. **A slug is never reusable, archived persons included.** Freeing one lets a
   stale bookmark or a cached service-worker URL resolve to a different human,
   which the design spec calls "the worst failure this design can produce."

Every test seeds an admin explicitly. The default test database has an empty
`users` table, which is open-access mode -- and in that mode the anonymous
sentinel's role is None, so `require_admin` refuses it and every route here
403s. That is deliberate (see shared/persons_admin.py's docstring) and pinned
by test_open_access_mode_cannot_reach_the_person_admin_surface below, but it
means a test that forgets to seed an admin fails for the wrong reason.
"""

import re

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from shared.auth import add_auth_routes, create_session_cookie, require_person
from shared.database import get_db
from shared.persons_admin import add_person_routes
from tests.conftest import grant_person, primary_person_id, seed_person, seed_user


def _build_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)
    add_person_routes(app)

    # A require_person route so archiving can be checked end to end: "archived"
    # is only meaningful if the person actually stops being reachable through
    # the dependency every person-scoped route uses.
    @app.get("/p/{slug}/api/read")
    async def read(person_id: int = Depends(require_person("view"))):
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


async def _fetchone(sql: str, params: tuple = ()):
    db = await get_db()
    try:
        return await (await db.execute(sql, params)).fetchone()
    finally:
        await db.close()


# --- authorization ------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/persons", None),
        ("post", "/api/persons", {"display_name": "Bryn"}),
        ("patch", "/api/persons/1", {"display_name": "Bryn"}),
        ("post", "/api/persons/1/archive", None),
    ],
)
async def test_person_crud_is_admin_only(client, method, path, body):
    _, cookies = await _as("bob")
    kwargs = {"cookies": cookies}
    if body is not None:
        kwargs["json"] = body
    resp = await getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, f"{method.upper()} {path} gave {resp.status_code}"


async def test_person_crud_admin_positive_control(client):
    """The positive control for the test above. Without it, that test passes
    just as happily against a surface that 403s EVERYONE -- including a
    misconfigured route that no admin can reach either."""
    await _as("bob")
    _, admin_cookies = await _as("root", role="admin")
    resp = await client.get("/api/persons", cookies=admin_cookies)
    assert resp.status_code == 200
    assert [p["slug"] for p in resp.json()] == ["primary"]


async def test_person_admin_surface_requires_authentication(client):
    """No cookie, users table non-empty: the middleware answers 401 JSON
    because /api/persons is an API path, not a 302 to the login page."""
    await _as("root", role="admin")
    resp = await client.get("/api/persons")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


async def test_open_access_mode_cannot_reach_the_person_admin_surface(client):
    """Open access (empty users table) grants the anonymous sentinel implicit
    `own` on every person through require_person -- but NOT the admin surface,
    whose role check it fails exactly as it fails /auth/admin/users today. One
    superuser story, not two. There are also no users to grant anything to in
    that mode."""
    resp = await client.get("/api/persons")
    assert resp.status_code == 403

    # Positive control: the same client DOES reach a person-scoped route in
    # this mode, so the 403 above is the admin gate and not a dead app.
    reachable = await client.get("/p/primary/api/read")
    assert reachable.status_code == 200


# --- create -------------------------------------------------------------------


async def test_create_person_derives_a_slug_and_grants_the_creator_own(client):
    admin_id, cookies = await _as("root", role="admin")

    resp = await client.post("/api/persons", json={"display_name": "Bryn Ó Súilleabháin"}, cookies=cookies)
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["slug"] == "bryn-o-suilleabhain"

    grant = await _fetchone(
        "SELECT access, granted_by FROM person_grants WHERE person_id = ? AND user_id = ?",
        (created["id"], admin_id),
    )
    assert grant is not None, "the creating admin got no grant -- spec f.6 requires an automatic `own`"
    assert grant["access"] == "own"
    assert grant["granted_by"] == admin_id


async def test_created_person_is_immediately_reachable_by_its_creator(client):
    """End-to-end: the automatic grant is not just a row, it authorizes."""
    _, cookies = await _as("root", role="admin")
    await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)

    resp = await client.get("/p/bryn/api/read", cookies=cookies)
    assert resp.status_code == 200


async def test_create_person_accepts_an_explicit_slug(client):
    _, cookies = await _as("root", role="admin")
    resp = await client.post(
        "/api/persons", json={"display_name": "Bryn", "slug": "b"}, cookies=cookies
    )
    assert resp.status_code == 200
    assert resp.json()["slug"] == "b"


@pytest.mark.parametrize(
    "slug",
    [
        "Bryn",  # uppercase
        "has space",
        "..",
        "a/b",
        "-leading",
        "trailing-",
        "x" * 33,  # over the 32-char ceiling
        "",
    ],
)
async def test_create_person_rejects_a_malformed_slug(client, slug):
    """This is the first ROUTE that mints a slug. Until now SLUG_RE was
    enforced only in shared/migrations.py and scripts/seed_db.py, neither of
    which takes external input."""
    _, cookies = await _as("root", role="admin")
    resp = await client.post(
        "/api/persons", json={"display_name": "Bryn", "slug": slug}, cookies=cookies
    )
    assert resp.status_code == 422, f"slug {slug!r} was accepted"


@pytest.mark.parametrize("slug", ["api", "auth", "static", "health", "p", "admin", "persons"])
async def test_create_person_rejects_a_reserved_slug(client, slug):
    _, cookies = await _as("root", role="admin")
    resp = await client.post(
        "/api/persons", json={"display_name": "Someone", "slug": slug}, cookies=cookies
    )
    assert resp.status_code == 422


async def test_create_person_rejects_a_display_name_with_no_usable_slug(client):
    """slugify() returns "" when nothing survives, and its docstring is
    explicit that callers must handle that rather than persist an empty slug
    into a NOT NULL UNIQUE column."""
    _, cookies = await _as("root", role="admin")
    resp = await client.post("/api/persons", json={"display_name": "。。。"}, cookies=cookies)
    assert resp.status_code == 422
    assert "slug" in resp.json()["detail"].lower()


async def test_create_person_rejects_a_blank_display_name(client):
    _, cookies = await _as("root", role="admin")
    resp = await client.post("/api/persons", json={"display_name": "   "}, cookies=cookies)
    assert resp.status_code == 422


async def test_create_person_rejects_a_duplicate_slug(client):
    _, cookies = await _as("root", role="admin")
    await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    resp = await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    assert resp.status_code == 409


async def test_an_archived_persons_slug_stays_permanently_taken(client):
    """Plan constraint 6. If archiving freed the slug, a stale bookmark or a
    cached service-worker URL for /p/bryn/ would resolve to a DIFFERENT human's
    health data -- the worst failure this design can produce."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    archived = await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)
    assert archived.status_code == 200

    resp = await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    assert resp.status_code == 409, "an archived person's slug was handed to a new person"


# --- list ---------------------------------------------------------------------


async def test_list_persons_includes_archived_and_reports_grant_counts(client):
    """The admin surface addresses by id precisely so it can see archived
    persons; require_person deliberately cannot."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)

    listed = (await client.get("/api/persons", cookies=cookies)).json()
    by_slug = {p["slug"]: p for p in listed}
    assert "bryn" in by_slug, "archived persons must stay visible on the admin surface"
    assert by_slug["bryn"]["archived_at"] is not None
    assert by_slug["bryn"]["grant_count"] == 1


# --- patch --------------------------------------------------------------------


async def test_patch_renames_the_display_name(client):
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()

    resp = await client.patch(
        f"/api/persons/{created['id']}", json={"display_name": "Bryn W."}, cookies=cookies
    )
    assert resp.status_code == 200
    row = await _fetchone("SELECT display_name, slug FROM persons WHERE id = ?", (created["id"],))
    assert row["display_name"] == "Bryn W."
    assert row["slug"] == "bryn", "the slug must not move when the display name does"


async def test_patch_cannot_change_the_slug(client):
    """Not an oversight: renaming frees the old slug for a later create to
    claim, which reopens the stale-bookmark failure constraint 6 closes.
    model_config extra="forbid" turns the attempt into a 422 rather than a
    silently ignored field."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()

    resp = await client.patch(
        f"/api/persons/{created['id']}", json={"slug": "bryn2"}, cookies=cookies
    )
    assert resp.status_code == 422
    row = await _fetchone("SELECT slug FROM persons WHERE id = ?", (created["id"],))
    assert row["slug"] == "bryn"


async def test_promotion_is_refused_without_acknowledging_the_garmin_handover(client):
    """`is_primary` is ALSO what garmin_credential_person_id() returns, i.e.
    which person this deployment's single Garmin account is taken to describe.
    Promoting therefore reassigns that account, and the next scheduled sync
    files the original human's sleep, HRV and weight under the new primary.

    The contamination is invisible until someone reads the data and believes
    it, so the acknowledgement is enforced by the API rather than by the admin
    page's confirm() -- a scripted PATCH bypasses the dialog entirely.
    """
    _, cookies = await _as("root", role="admin")
    old_primary = await primary_person_id()
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()

    resp = await client.patch(
        f"/api/persons/{created['id']}", json={"is_primary": True}, cookies=cookies
    )
    assert resp.status_code == 409
    assert "Garmin" in resp.json()["detail"]
    assert await primary_person_id() == old_primary, "the promotion happened anyway"


async def test_renaming_does_not_require_the_garmin_acknowledgement(client):
    """The flag gates promotion specifically. A display-name change touches
    nothing Garmin-related and must not be made harder."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    resp = await client.patch(
        f"/api/persons/{created['id']}", json={"display_name": "Bryn W."}, cookies=cookies
    )
    assert resp.status_code == 200


async def test_patch_promotes_a_person_to_primary_and_demotes_the_old_one(client):
    _, cookies = await _as("root", role="admin")
    old_primary = await primary_person_id()
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()

    resp = await client.patch(
        f"/api/persons/{created['id']}",
        json={"is_primary": True, "acknowledge_garmin_reassignment": True},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text

    primaries = await _fetchone("SELECT COUNT(*) AS n FROM persons WHERE is_primary = 1")
    assert primaries["n"] == 1, "the partial unique index allows exactly one primary"
    assert await primary_person_id() == created["id"]
    assert (await _fetchone("SELECT is_primary FROM persons WHERE id = ?", (old_primary,)))[
        "is_primary"
    ] == 0


async def test_promoting_the_already_primary_person_is_a_no_op(client):
    _, cookies = await _as("root", role="admin")
    current = await primary_person_id()
    resp = await client.patch(f"/api/persons/{current}", json={"is_primary": True}, cookies=cookies)
    assert resp.status_code == 200
    assert await primary_person_id() == current


async def test_patch_refuses_to_demote_without_a_replacement(client):
    """There must always be exactly one primary: get_primary_person_id()
    raises when there is none, and scheduled_sync still depends on it."""
    _, cookies = await _as("root", role="admin")
    current = await primary_person_id()
    resp = await client.patch(
        f"/api/persons/{current}", json={"is_primary": False}, cookies=cookies
    )
    assert resp.status_code == 422
    assert await primary_person_id() == current


async def test_patch_refuses_to_promote_an_archived_person(client):
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)

    resp = await client.patch(
        f"/api/persons/{created['id']}",
        json={"is_primary": True, "acknowledge_garmin_reassignment": True},
        cookies=cookies,
    )
    assert resp.status_code == 409
    # The archived check must win over the Garmin acknowledgement check, or
    # this test would pass for the wrong reason.
    assert "archived" in resp.json()["detail"].lower()


async def test_promoting_a_nonexistent_person_is_404_not_the_garmin_409(client):
    """Order of checks: existence first. Otherwise a typo'd id gets a lecture
    about Garmin credentials instead of "no such person"."""
    _, cookies = await _as("root", role="admin")
    resp = await client.patch(
        "/api/persons/999999", json={"is_primary": True}, cookies=cookies
    )
    assert resp.status_code == 404


async def test_patch_nonexistent_person_returns_404(client):
    _, cookies = await _as("root", role="admin")
    resp = await client.patch("/api/persons/999999", json={"display_name": "X"}, cookies=cookies)
    assert resp.status_code == 404


async def test_empty_patch_is_rejected(client):
    _, cookies = await _as("root", role="admin")
    current = await primary_person_id()
    resp = await client.patch(f"/api/persons/{current}", json={}, cookies=cookies)
    assert resp.status_code == 422


# --- archive ------------------------------------------------------------------


async def test_archiving_makes_a_person_unreachable_through_require_person(client):
    """The point of archiving, asserted end to end rather than by reading the
    archived_at column."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    assert (await client.get("/p/bryn/api/read", cookies=cookies)).status_code == 200

    await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)

    resp = await client.get("/p/bryn/api/read", cookies=cookies)
    assert resp.status_code == 404, "an archived person stayed reachable"


async def test_cannot_archive_the_primary_person(client):
    _, cookies = await _as("root", role="admin")
    current = await primary_person_id()
    resp = await client.post(f"/api/persons/{current}/archive", cookies=cookies)
    assert resp.status_code == 409
    # The message must name a fix the API can actually perform -- an error
    # pointing at a nonexistent endpoint is worse than no advice.
    assert "is_primary" in resp.json()["detail"]


async def test_the_primary_person_can_be_archived_after_promoting_another(client):
    """The other half of the test above: the 409's advice has to work."""
    _, cookies = await _as("root", role="admin")
    old_primary = await primary_person_id()
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    await client.patch(
        f"/api/persons/{created['id']}",
        json={"is_primary": True, "acknowledge_garmin_reassignment": True},
        cookies=cookies,
    )

    resp = await client.post(f"/api/persons/{old_primary}/archive", cookies=cookies)
    assert resp.status_code == 200


async def test_archiving_is_idempotent(client):
    """A double-click must not rewrite archived_at."""
    _, cookies = await _as("root", role="admin")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    first = (await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)).json()
    second = (await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)).json()
    assert first["archived_at"] == second["archived_at"]


async def test_archiving_clears_a_default_person_id_pointing_at_it(client):
    """An archived person drops out of _reachable_persons, so a stale default
    leaves GET / falling through to the ambiguous-400 dead end -- which a
    non-admin cannot clear for themselves."""
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET default_person_id = ? WHERE id = ?", (created["id"], bob_id)
        )
        await db.commit()
    finally:
        await db.close()

    await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)

    row = await _fetchone("SELECT default_person_id FROM users WHERE id = ?", (bob_id,))
    assert row["default_person_id"] is None


async def test_archive_nonexistent_person_returns_404(client):
    _, cookies = await _as("root", role="admin")
    resp = await client.post("/api/persons/999999/archive", cookies=cookies)
    assert resp.status_code == 404


# --- grants -------------------------------------------------------------------


async def test_admin_can_manage_grants(client):
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")

    assert (
        await client.put(
            f"/api/persons/{person_id}/grants/{bob_id}", json={"access": "view"}, cookies=cookies
        )
    ).status_code == 200

    listed = (await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)).json()
    assert [(g["username"], g["access"]) for g in listed] == [("bob", "view")]
    assert listed[0]["granted_by_username"] == "root"

    assert (
        await client.delete(f"/api/persons/{person_id}/grants/{bob_id}", cookies=cookies)
    ).status_code == 200
    assert (await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)).json() == []


async def test_an_own_holder_who_is_not_an_admin_can_manage_grants(client):
    """Spec f.6: `own` on the person, OR any admin. Not admin-only."""
    owner_id, owner_cookies = await _as("owner")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")
    await grant_person(person_id, owner_id, access="own")

    resp = await client.put(
        f"/api/persons/{person_id}/grants/{bob_id}", json={"access": "manage"}, cookies=owner_cookies
    )
    assert resp.status_code == 200, resp.text
    listed = (await client.get(f"/api/persons/{person_id}/grants", cookies=owner_cookies)).json()
    assert {g["username"] for g in listed} == {"owner", "bob"}


# Every grant route, so the parametrized authorization tests below cover the
# WRITES too. Reviewing this PR found that `_require_person_owner` could be
# deleted from both upsert_grant and revoke_grant with the whole suite green,
# because only the read was exercised -- letting any authenticated account
# grant itself `own` on anyone. A guard that cannot be made to fail is
# decorative; a guard nothing calls is worse.
_GRANT_ROUTES = [
    ("get", "/api/persons/{person_id}/grants", None),
    ("put", "/api/persons/{person_id}/grants/{user_id}", {"access": "own"}),
    ("delete", "/api/persons/{person_id}/grants/{user_id}", None),
]


async def _call_grant_route(client, method, template, body, person_id, user_id, cookies):
    path = template.format(person_id=person_id, user_id=user_id)
    kwargs = {"cookies": cookies}
    if body is not None:
        kwargs["json"] = body
    return await getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("access", ["view", "manage"])
@pytest.mark.parametrize("method,template,body", _GRANT_ROUTES)
async def test_a_lesser_grant_holder_gets_404_not_403(client, access, method, template, body):
    """THE leak test for this surface. These routes address a person by ID, so
    a 403 would confirm that person exists to anyone logged in who can count
    upward. Mutation-checked: changing the 404 in _require_person_owner to a
    403 fails this test, and so does removing the call from any of the three
    routes."""
    user_id, cookies = await _as(f"holder-{access}-{method}")
    person_id = await seed_person("bryn")
    await grant_person(person_id, user_id, access=access)

    resp = await _call_grant_route(
        client, method, template, body, person_id, user_id, cookies
    )
    assert resp.status_code == 404, f"{method.upper()} {template} gave {resp.status_code}"
    assert "bryn" not in resp.text


@pytest.mark.parametrize("method,template,body", _GRANT_ROUTES)
async def test_an_account_with_no_grant_cannot_grant_itself_access(
    client, method, template, body
):
    """The escalation this surface must refuse: an authenticated account with
    no relationship to a person, granting itself `own` on them."""
    user_id, cookies = await _as(f"stranger-{method}")
    person_id = await seed_person("bryn")

    resp = await _call_grant_route(
        client, method, template, body, person_id, user_id, cookies
    )
    assert resp.status_code == 404

    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM person_grants WHERE person_id = ? AND user_id = ?",
        (person_id, user_id),
    )
    assert row["n"] == 0, "a caller with no grant wrote itself one"


@pytest.mark.parametrize("method,template,body", _GRANT_ROUTES)
async def test_grant_routes_positive_control_for_an_own_holder(client, method, template, body):
    """The control for both tests above: an `own` holder reaches all three, so
    neither can pass against routes that refuse everyone."""
    owner_id, owner_cookies = await _as(f"owner-{method}")
    person_id = await seed_person("bryn")
    await grant_person(person_id, owner_id, access="own")

    resp = await _call_grant_route(
        client, method, template, body, person_id, owner_id, owner_cookies
    )
    assert resp.status_code == 200, f"{method.upper()} {template} gave {resp.status_code}"


async def test_a_stranger_gets_the_same_404_as_a_nonexistent_person(client):
    """The two answers are identical on purpose -- if they differed, the
    difference itself would enumerate the household."""
    _, cookies = await _as("stranger")
    person_id = await seed_person("bryn")

    existing = await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)
    missing = await client.get("/api/persons/999999/grants", cookies=cookies)
    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json()


async def test_grant_route_positive_control(client):
    """Without this, every 404 test above passes just as well against a route
    that 404s unconditionally."""
    _, cookies = await _as("root", role="admin")
    person_id = await seed_person("bryn")
    assert (await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)).status_code == 200


async def test_grants_on_an_archived_person_stay_manageable(client):
    """Addressing by id exists so this works: require_person cannot reach an
    archived person, and grant cleanup on one would otherwise be impossible."""
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    created = (
        await client.post("/api/persons", json={"display_name": "Bryn"}, cookies=cookies)
    ).json()
    await client.post(f"/api/persons/{created['id']}/archive", cookies=cookies)

    resp = await client.put(
        f"/api/persons/{created['id']}/grants/{bob_id}", json={"access": "view"}, cookies=cookies
    )
    assert resp.status_code == 200


async def test_an_orphaned_grant_is_listed_and_revocable(client):
    """A grant whose account was deleted before the cascade shipped is counted
    by `grant_count`, so it must also be LISTED -- an inner join here made the
    people table say "1 grant" while the access table showed none, and the
    invisible row is exactly the id-reuse hazard the cascade closes.
    Unfixable from the UI is the worst of both.
    """
    _, cookies = await _as("root", role="admin")
    person_id = await seed_person("bryn")
    db = await get_db()
    try:
        # Straight to SQL: the route refuses to create this state, which is the
        # point -- it can only arrive from a database written before the fix.
        await db.execute(
            "INSERT INTO person_grants (person_id, user_id, access, granted_at) "
            "VALUES (?, 999999, 'view', '2026-01-01T00:00:00+00:00')",
            (person_id,),
        )
        await db.commit()
    finally:
        await db.close()

    listed = (await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)).json()
    assert len(listed) == 1, "the orphaned grant is invisible on the access page"
    assert listed[0]["user_id"] == 999999
    assert listed[0]["username"] is None

    counted = [
        p for p in (await client.get("/api/persons", cookies=cookies)).json() if p["id"] == person_id
    ][0]["grant_count"]
    assert counted == len(listed), "grant_count and the grant list disagree"

    revoked = await client.delete(f"/api/persons/{person_id}/grants/999999", cookies=cookies)
    assert revoked.status_code == 200
    assert (await client.get(f"/api/persons/{person_id}/grants", cookies=cookies)).json() == []


async def test_granting_to_a_nonexistent_user_is_refused(client):
    """SQLite foreign keys are off in this project, so person_grants'
    REFERENCES clause does not fire: without the in-transaction check this
    would persist a dangling grant whose user_id an AUTOINCREMENT reuse could
    later hand to an unrelated new account."""
    _, cookies = await _as("root", role="admin")
    person_id = await seed_person("bryn")

    resp = await client.put(
        f"/api/persons/{person_id}/grants/999999", json={"access": "view"}, cookies=cookies
    )
    assert resp.status_code == 404
    row = await _fetchone("SELECT COUNT(*) AS n FROM person_grants WHERE user_id = 999999")
    assert row["n"] == 0


async def test_grant_rejects_an_unknown_access_level(client):
    """A typo'd level must be a 422 from the schema, not an IntegrityError
    from person_grants' CHECK constraint surfacing as a 500."""
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")

    resp = await client.put(
        f"/api/persons/{person_id}/grants/{bob_id}", json={"access": "root"}, cookies=cookies
    )
    assert resp.status_code == 422


async def test_regranting_changes_the_level_rather_than_erroring(client):
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")

    await client.put(
        f"/api/persons/{person_id}/grants/{bob_id}", json={"access": "view"}, cookies=cookies
    )
    resp = await client.put(
        f"/api/persons/{person_id}/grants/{bob_id}", json={"access": "own"}, cookies=cookies
    )
    assert resp.status_code == 200
    row = await _fetchone(
        "SELECT access FROM person_grants WHERE person_id = ? AND user_id = ?",
        (person_id, bob_id),
    )
    assert row["access"] == "own"


async def test_revoking_your_own_last_own_grant_is_permitted(client):
    """Spec f.6, and deliberately NOT modeled on the "cannot demote the last
    admin" guard: that guard exists because there is no higher authority to
    recover from zero admins. Here any admin can re-grant."""
    owner_id, owner_cookies = await _as("owner")
    person_id = await seed_person("bryn")
    await grant_person(person_id, owner_id, access="own")

    resp = await client.delete(f"/api/persons/{person_id}/grants/{owner_id}", cookies=owner_cookies)
    assert resp.status_code == 200

    # And the caller has genuinely locked themselves out afterwards.
    assert (await client.get("/p/bryn/api/read", cookies=owner_cookies)).status_code == 404


async def test_a_zero_grant_person_stays_reachable_by_an_admin(client):
    """The orphaned person is a reachable, deliberate state (spec f.6). The
    rejected alternative -- "cannot delete the last grant" -- would make
    deleting a USER fail for reasons an admin cannot see from the users page."""
    _, admin_cookies = await _as("root", role="admin")
    owner_id, owner_cookies = await _as("owner")
    person_id = await seed_person("bryn")
    await grant_person(person_id, owner_id, access="own")
    await client.delete(f"/api/persons/{person_id}/grants/{owner_id}", cookies=owner_cookies)

    listed = (await client.get("/api/persons", cookies=admin_cookies)).json()
    assert [p for p in listed if p["slug"] == "bryn"][0]["grant_count"] == 0
    assert (await client.get("/p/bryn/api/read", cookies=admin_cookies)).status_code == 200


async def test_revoking_a_grant_clears_that_users_default_person_id(client):
    """Same dead end archiving creates, from the other direction: a default
    pointing at a person this user can no longer reach."""
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")
    await grant_person(person_id, bob_id, access="view")
    db = await get_db()
    try:
        await db.execute("UPDATE users SET default_person_id = ? WHERE id = ?", (person_id, bob_id))
        await db.commit()
    finally:
        await db.close()

    await client.delete(f"/api/persons/{person_id}/grants/{bob_id}", cookies=cookies)

    row = await _fetchone("SELECT default_person_id FROM users WHERE id = ?", (bob_id,))
    assert row["default_person_id"] is None


async def test_revoking_a_grant_that_does_not_exist_returns_404(client):
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    person_id = await seed_person("bryn")

    resp = await client.delete(f"/api/persons/{person_id}/grants/{bob_id}", cookies=cookies)
    assert resp.status_code == 404


async def test_revoking_one_grant_leaves_the_others_alone(client):
    """A DELETE missing its person_id predicate would strip this user's access
    to EVERY person and pass every test above."""
    _, cookies = await _as("root", role="admin")
    bob_id, _ = await _as("bob")
    bryn = await seed_person("bryn")
    cass = await seed_person("cass")
    await grant_person(bryn, bob_id, access="view")
    await grant_person(cass, bob_id, access="view")

    await client.delete(f"/api/persons/{bryn}/grants/{bob_id}", cookies=cookies)

    row = await _fetchone("SELECT COUNT(*) AS n FROM person_grants WHERE user_id = ?", (bob_id,))
    assert row["n"] == 1


# --- the admin page -----------------------------------------------------------


async def _assert_surface_registered(module, label):
    _, cookies = await _as("root", role="admin")
    transport = ASGITransport(app=module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/api/persons", "/auth/admin/persons"):
            resp = await ac.get(path, cookies=cookies)
            assert resp.status_code == 200, f"{label} does not serve {path}"


async def test_the_surface_is_registered_on_the_weight_service(weight_app_module):
    """`add_person_routes` is called from both app.py files so one login covers
    both ports. Every other test here builds a bare FastAPI app, so removing
    either registration left the whole suite green -- the cross-service claim
    had no coverage on either real service."""
    await _assert_surface_registered(weight_app_module, "vitalforge-weight")


async def test_the_surface_is_registered_on_the_dashboard_service(dashboard_app_module):
    await _assert_surface_registered(dashboard_app_module, "vitalforge-dashboard")


async def test_admin_persons_page_is_admin_only(client):
    _, cookies = await _as("bob")
    assert (await client.get("/auth/admin/persons", cookies=cookies)).status_code == 403

    _, admin_cookies = await _as("root", role="admin")
    assert (await client.get("/auth/admin/persons", cookies=admin_cookies)).status_code == 200


async def test_admin_persons_page_never_assigns_server_data_to_innerhtml(client):
    """display_name is the untrusted field on this page -- arbitrary TEXT with
    no SLUG_RE to constrain it -- and the page is a raw HTMLResponse with no
    Jinja autoescape backstop, so markup in a name would execute in another
    admin's browser on load.

    Asserted against the served response rather than the source constant, and
    both halves matter: the vulnerable pattern absent AND the safe one present.
    Checking only the first passes against a page that renders nothing at all.
    """
    _, cookies = await _as("root", role="admin")
    resp = await client.get("/auth/admin/persons", cookies=cookies)
    assert resp.status_code == 200
    html = resp.text

    assert not re.search(r"\.innerHTML\s*=", html), "server data is being assigned to innerHTML"
    assert "td.textContent = text;" in html
    assert "opt.textContent = u.username;" in html
