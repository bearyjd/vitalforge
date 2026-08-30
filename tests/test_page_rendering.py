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

from tests.conftest import PRIMARY_SLUG


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
        "person_slug did not reach the template; the page would load and then fail "
        "to build any API URL"
    )
