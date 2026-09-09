"""The server-rendered person page actually renders, on both services.

This file exists because of a gap, not a feature. Every HTML page in this
project is rendered by exactly two `templates.TemplateResponse(...)` calls, and
until now NOTHING in the default `pytest -q` lane exercised either of them --
the one fast-lane request to `/p/{slug}/` (tests/test_landing_parity.py) asserts
a **404**, and every other test hits a JSON API. Template rendering was covered
only by the Playwright lane, which is a separate process, needs a browser, and
cannot run on every developer's machine.

A starlette major bump found the gap the expensive way: the
`TemplateResponse(name, {"request": ...})` signature was removed, so the
arguments rebound as `request="index.html", name={...}`, the context dict
reached Jinja2's template cache as a key, and every page render became
`TypeError: unhashable type: 'dict'`. 649 fast tests passed; four Playwright
tests failed in CI with selector timeouts that said nothing about the cause.

So these are deliberately cheap: a real request through the real app, asserting
the page rendered AND that the context reached the template. No browser.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import shared.auth as shared_auth
from shared.auth import create_session_cookie
from tests.conftest import PRIMARY_SLUG, grant_person, primary_person_id, seed_user


@pytest.fixture
def service_module(request):
    """Both services, by fixture name -- they are sync fixtures wrapping an
    async one, so they cannot be pulled in via getfixturevalue from inside an
    async test."""
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize(
    "service_module", ["weight_app_module", "dashboard_app_module"], indirect=True
)
async def test_the_person_page_renders(service_module):
    """Runs in open-access mode (the fixtures leave `users` empty), so
    require_person admits the anonymous sentinel and this is purely a
    rendering test."""
    transport = ASGITransport(app=service_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/p/{PRIMARY_SLUG}/")

    assert resp.status_code == 200, f"the person page did not render: {resp.text[:400]}"
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "service_module", ["weight_app_module", "dashboard_app_module"], indirect=True
)
async def test_the_person_page_receives_its_template_context(service_module):
    """A 200 alone would pass against a template rendered with an empty
    context -- and an empty `person_slug` is not cosmetic here: both templates
    build every API URL from `PERSON_SLUG` and throw if it is missing, so the
    page would load and then do nothing."""
    transport = ASGITransport(app=service_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/p/{PRIMARY_SLUG}/")

    assert f'const PERSON_SLUG = "{PRIMARY_SLUG}";' in resp.text, (
        "person_slug did not reach the template as a JS constant -- either the "
        "context never arrived (the page would load and then fail to build any "
        "API URL), or the template's formatting for that line changed. Check both "
        "before assuming a rendering break."
    )


@pytest.mark.parametrize(
    "service_module", ["weight_app_module", "dashboard_app_module"], indirect=True
)
async def test_the_person_page_renders_for_an_authenticated_grantee(service_module):
    """The two tests above run in open-access mode, which is not the path any
    real user takes. This one enables auth (seeding a user is what flips
    _is_auth_configured), presents a session cookie, and makes require_person
    resolve a real grant before the template renders -- a page that rendered
    only while the `users` table is empty would pass both tests above.
    """
    username = "renderer"
    await seed_user(username)
    user_id, session_version = await shared_auth._get_user_id_and_session_version(username)
    await grant_person(await primary_person_id(), user_id, "view")

    transport = ASGITransport(app=service_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"vf_session": create_session_cookie(username, user_id, session_version)},
    ) as ac:
        resp = await ac.get(f"/p/{PRIMARY_SLUG}/")

    assert resp.status_code == 200, f"the person page did not render for a grantee: {resp.text[:400]}"
    assert f'const PERSON_SLUG = "{PRIMARY_SLUG}";' in resp.text


# --- the /static/ mount: what PYSEC-2026-1942 is actually reachable through ----
#
# This bump's whole justification is a Range-header DoS in starlette's
# FileResponse, reachable unauthenticated through these mounts
# (vitalforge_weight/app.py, vitalforge_dashboard/app.py -- `app.mount("/static",
# StaticFiles(...))`). Nothing in the suite issued a single request to either
# one: the only "/static/" strings in tests/ are a stub route on a throwaway app
# in test_auth_middleware.py. Bumping a dependency to fix a path CI never
# exercises is how the TemplateResponse break shipped -- so the path gets a test.


@pytest.mark.parametrize(
    "service_module", ["weight_app_module", "dashboard_app_module"], indirect=True
)
async def test_the_static_mount_serves(service_module):
    """Both services ship a manifest.json; this asserts our wiring, not starlette's."""
    transport = ASGITransport(app=service_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/static/manifest.json")

    assert resp.status_code == 200, f"the /static/ mount did not serve: {resp.text[:200]}"
    assert resp.content, "the /static/ mount served an empty body"


@pytest.mark.parametrize(
    "service_module", ["weight_app_module", "dashboard_app_module"], indirect=True
)
async def test_the_static_mount_honours_a_bounded_range(service_module):
    """The CVE's exact shape: a bounded Range must come back 206 with exactly
    the requested bytes, not 200 with the whole file. This does not re-test
    starlette's range parser -- it pins that range handling is reached at all
    through our mount, so a future bump that changes it cannot land silently.
    """
    transport = ASGITransport(app=service_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/static/manifest.json", headers={"Range": "bytes=0-9"})

    assert resp.status_code == 206, f"expected 206 for a bounded Range, got {resp.status_code}"
    assert len(resp.content) == 10, f"expected exactly 10 bytes, got {len(resp.content)}"
