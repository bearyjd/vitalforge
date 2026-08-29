"""Structural guards that the person-scoping sweep is complete.

Phase 2's failure mode is not a crash. A route that still resolves "the person"
through the Phase 1 shim keeps working perfectly for one person and silently
serves the wrong one the moment a second exists. Nothing in a normal test suite
notices, because with one person every answer is correct.

So these tests do not exercise behaviour -- they assert over the source itself,
which is the only way to catch a call site nobody thought to write a test for.
The design spec is explicit about why the helper is the hazard (f.1:1610):
"there is no module-level helper that returns a person_id without authorizing,
because such a helper is the thing that gets called by mistake."
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVICES = ["vitalforge-dashboard/app.py", "vitalforge-weight/app.py"]

# The only places get_primary_person_id() may legitimately survive. Both have
# no request to authorize, so require_person cannot serve them.
#
#   shared/database.py        -- defines it, and ensure_primary_person_grant()
#                                uses it as startup bootstrap before any admin
#                                exists to own the person.
#   vitalforge-dashboard/sync.py -- scheduled_sync has no request. Phase 4's
#                                round-robin cursor replaces this; until then
#                                the shim is the honest answer, and saying so
#                                here is what stops Phase 4 leaking forward.
#   scripts/seed_db.py        -- a CLI tool. There is no request and no
#                                caller to authorize; --person addresses a
#                                person explicitly and this is only the
#                                default when it is omitted.
_ALLOWED_SHIM_FILES = {
    "shared/database.py",
    "vitalforge-dashboard/sync.py",
    "scripts/seed_db.py",
}

# Every route shared/persons_admin.py is allowed to mount. Person-COLLECTION
# routes only: create, list, rename/promote, archive, and grant management.
# See test_the_person_admin_module_stays_a_collection_surface for why this is
# an exact list rather than a prefix.
_PERSON_ADMIN_PATHS = {
    "/auth/admin/persons",
    "/api/persons",
    "/api/persons/{person_id}",
    "/api/persons/{person_id}/archive",
    "/api/persons/{person_id}/grants",
    "/api/persons/{person_id}/grants/{user_id}",
}


def _python_sources():
    for d in ("shared", "vitalforge-dashboard", "vitalforge-weight", "scripts"):
        for p in sorted((REPO / d).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p.relative_to(REPO).as_posix(), p.read_text()


def test_no_request_path_resolves_a_person_without_authorizing():
    """get_primary_person_id() must not be CALLED outside its sanctioned
    homes. This is the whole point of Phase 2: one supplier of person_id.

    Matches call sites via AST, not substring. A substring check also flags
    docstrings and comments -- it flagged shared/auth.py, whose require_person
    docstring merely explains that the shim is retired from request paths. A
    guard that fires on prose about itself trains people to widen the
    allowlist, which is exactly how the thing it guards gets back in.
    """
    offenders = []
    for rel, src in _python_sources():
        if rel in _ALLOWED_SHIM_FILES:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "get_primary_person_id":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"{offenders} still resolve a person without authorizing. Every request path must "
        "obtain person_id from Depends(require_person(...)). If a new non-request caller is "
        "genuinely needed, add it to _ALLOWED_SHIM_FILES with a comment saying why it has no "
        "request to authorize."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_every_person_scoped_route_is_mounted_under_a_slug(service):
    """A person-scoped route left at the root has no {slug} for
    require_person to read, so it cannot be authorizing -- it is either
    unscoped or using some other person source."""
    src = (REPO / service).read_text()
    tree = ast.parse(src)

    unscoped = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in {"get", "post", "patch", "put", "delete"}:
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            path = dec.args[0].value
            if not isinstance(path, str) or not path.startswith("/api/"):
                continue
            # A root /api/ route is only acceptable if it takes no person at
            # all. Any mention of person_id in its signature or body means it
            # is person-scoped and belongs under /p/{slug}/.
            body_src = ast.get_source_segment(src, node) or ""
            if "person_id" in body_src:
                unscoped.append(f"{path} ({node.name}, line {node.lineno})")

    assert not unscoped, (
        f"{service}: {unscoped} are person-scoped but still mounted at the root. "
        "They must move under /p/{slug}/ so require_person can authorize them."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_every_person_scoped_route_declares_require_person(service):
    """Being under /p/{slug}/ is not authorization -- the URL shape is a
    routing convenience, and the design spec is explicit that safety comes
    from the dependency, not the path (f.2:1667). A route under a slug that
    forgot the Depends would read `slug` and ignore it."""
    src = (REPO / service).read_text()
    tree = ast.parse(src)

    missing = []
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in {"get", "post", "patch", "put", "delete"}:
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            path = dec.args[0].value
            if not isinstance(path, str) or not path.startswith("/p/{slug}/api/"):
                continue
            seen += 1
            sig_src = ast.get_source_segment(src, node) or ""
            head = sig_src.split(":\n", 1)[0]
            if "require_person" not in head:
                missing.append(f"{path} ({node.name}, line {node.lineno})")

    # A FLOOR, not a count. Its only job is to stop this test passing
    # vacuously over an empty set -- which is what it did before the sweep
    # landed, and would do again if a refactor moved every route out from
    # under the prefix or the matching above stopped recognising them.
    # Deliberately not the exact number of routes: that would need bumping
    # every time one is added, which turns into a reflex rather than a check.
    # Exhaustiveness is guaranteed by
    # test_every_person_scoped_route_is_mounted_under_a_slug instead.
    _NOT_VACUOUS = 4
    assert seen >= _NOT_VACUOUS, (
        f"{service}: found only {seen} routes under /p/{{slug}}/api/. This test cannot "
        "pass by finding nothing -- either the sweep is incomplete or the route-matching "
        "above stopped recognising them."
    )

    assert not missing, (
        f"{service}: {missing} sit under /p/{{slug}}/ but do not Depends on require_person. "
        "The path is routing; the dependency is authorization. A route with the former and "
        "not the latter reads a slug it never checks."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_require_person_is_never_used_outside_a_slug_path(service):
    """The inverse of the test above, and the one that catches the real
    footgun.

    FastAPI binds `slug` from the QUERY STRING when a route's path has no
    {slug} placeholder -- silently, documenting it as `in: query`. Verified
    empirically: a route at /api/x with Depends(require_person("view"))
    answered GET /api/x?slug=primary with 200. That makes ?slug= a second way
    to address a person, which is the implicit-fallback path this phase
    exists to delete.

    require_person carries a runtime guard for it, but a 500 on a live route
    is a bad place to learn this. Catch it here, before merge.
    """
    src = (REPO / service).read_text()
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        sig_src = ast.get_source_segment(src, node) or ""
        head = sig_src.split(":\n", 1)[0]
        if "require_person" not in head:
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in {"get", "post", "patch", "put", "delete"}:
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            path = dec.args[0].value
            if isinstance(path, str) and "{slug}" not in path:
                offenders.append(f"{path} ({node.name}, line {node.lineno})")

    assert not offenders, (
        f"{service}: {offenders} use require_person on a path with no {{slug}} placeholder. "
        "FastAPI will bind slug from the query string instead, silently, and the route will "
        "answer 200 to ?slug=<anyone>."
    )


def test_the_person_admin_module_stays_a_collection_surface():
    """The guards above scan only the two service app.py files, so routes added
    to shared/persons_admin.py escape them entirely.

    That module is Phase 2's one deliberate exception to "person-scoped means
    /p/{slug}/": the person COLLECTION is addressed by id at the root, because
    it must reach archived persons and require_person deliberately cannot. The
    exception is bounded here rather than left implicit -- a route that both
    lives in this module and takes a person as its SUBJECT belongs under
    /p/{slug}/ with the rest, and would otherwise be invisible to every
    structural guard in this file.
    """
    src = (REPO / "shared/persons_admin.py").read_text()
    tree = ast.parse(src)

    seen = 0
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in {"get", "post", "patch", "put", "delete"}:
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            path = dec.args[0].value
            if not isinstance(path, str):
                continue
            seen += 1
            # An exact allowlist, not a prefix. `startswith("/api/persons")`
            # would wave through `/api/persons/{person_id}/metrics/{name}` --
            # a person-SUBJECT route serving health data from a root path,
            # which is precisely what this guard exists to stop. Adding a route
            # here should be a deliberate edit to this list.
            if path not in _PERSON_ADMIN_PATHS:
                offenders.append(f"{path} ({node.name}, line {node.lineno})")
            # Matched via AST, not substring: this module's own
            # `_require_person_owner` CONTAINS the string "require_person", and
            # a substring check flags it -- a guard that fires on the helper it
            # is meant to protect trains people to delete it.
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name == "require_person":
                    offenders.append(
                        f"{path} ({node.name}, line {node.lineno}) uses require_person on a "
                        "path with no {slug}; FastAPI would bind slug from the query string"
                    )

    # A FLOOR, not a count -- see the same reasoning in
    # test_every_person_scoped_route_declares_require_person.
    assert seen >= 5, (
        f"found only {seen} routes in shared/persons_admin.py; either the module moved or "
        "the route-matching above stopped recognising them"
    )
    assert not offenders, (
        f"shared/persons_admin.py mounts {offenders}. This module is the person COLLECTION "
        "surface only. Anything whose subject is one person belongs under /p/{slug}/ where "
        "require_person and the guards above can see it."
    )


def test_no_frontend_code_calls_an_unscoped_api_path():
    """Templates and static JS calling /api/... would hit routes that no
    longer exist. Catches the case where a route moved and its caller did
    not."""
    offenders = []
    pattern = re.compile(r"""(?:fetch|url|href)\s*[(=]\s*[`'"]/api/""", re.IGNORECASE)
    for d in ("vitalforge-dashboard", "vitalforge-weight"):
        for ext in ("*.html", "*.js"):
            for p in sorted((REPO / d).rglob(ext)):
                for i, line in enumerate(p.read_text().splitlines(), 1):
                    if pattern.search(line):
                        offenders.append(f"{p.relative_to(REPO)}:{i}")
    assert not offenders, f"unscoped /api/ calls remain in the frontend: {offenders}"


def test_service_worker_cache_names_were_bumped():
    """Both service workers pre-cache the app shell and fall back to cache on
    network failure. If CACHE_NAME does not change when the URL shape does,
    EXISTING installs keep serving the old shell against the new backend and
    fetch routes that now 404. Fresh browser profiles never reproduce it,
    which is exactly why it needs a test rather than manual checking."""
    stale = []
    for d in ("vitalforge-dashboard", "vitalforge-weight"):
        sw = REPO / d / "static" / "sw.js"
        assert sw.exists(), f"{sw} is missing"
        m = re.search(r"""CACHE_NAME\s*=\s*['"]([^'"]+)['"]""", sw.read_text())
        assert m, f"{sw}: CACHE_NAME not found"
        if m.group(1).endswith("-v1"):
            stale.append(f"{sw.relative_to(REPO)} -> {m.group(1)}")
    assert not stale, (
        f"service worker cache not bumped for the person-scoped URL change: {stale}. "
        "Existing installs would serve the old shell against the new backend."
    )
