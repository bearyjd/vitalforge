"""The one subtle rule in the shared/auth.py split, pinned.

shared/auth_routes.py imports most of what it needs from shared.auth by name.
Two things it must NOT: `_authenticate_credentials` and `_require_step_up`.
Tests monkeypatch those on `shared.auth`, and a by-name import binds the
function at import time -- so the patch would land on shared.auth while the
route kept calling the original. The test would pass having exercised the real
credential check or the real step-up gate, which is the opposite of what it
asserts.

This is the same by-value binding trap the weight and dashboard splits hit. It
is worse here because the names are the auth boundary.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AUTH_ROUTES = REPO / "shared" / "auth_routes.py"

# Every name any test monkeypatches on shared.auth. Keep in sync: a name added
# here that auth_routes imports by value fails the test below.
PATCHED_BY_TESTS = {
    "_authenticate_credentials",
    "_require_step_up",
    "_ACCESS_ORDER",
    "get_current_user_role",
    "_USER",
    "_PASS",
}


def _names_imported_by_value() -> set[str]:
    tree = ast.parse(AUTH_ROUTES.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "shared.auth":
            names |= {a.asname or a.name for a in node.names}
    return names


def test_the_module_under_test_actually_imports_from_shared_auth():
    """A rename or restructure that stopped this file importing from shared.auth
    would make the real check below vacuously true."""
    assert _names_imported_by_value(), (
        "auth_routes.py imports nothing from shared.auth by value any more -- either "
        "the split changed shape or this guard is now checking nothing"
    )


@pytest.mark.parametrize("name", sorted(PATCHED_BY_TESTS))
def test_monkeypatched_names_are_not_imported_by_value(name):
    assert name not in _names_imported_by_value(), (
        f"shared/auth_routes.py imports {name} from shared.auth by value, but tests "
        f"monkeypatch it on shared.auth. The patch would not reach this module's "
        f"binding and the test would pass against the real implementation. Reach it "
        f"through the module instead: `from shared import auth` then `auth.{name}`."
    )
