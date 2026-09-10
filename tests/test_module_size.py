"""The 800-line ceiling this repo sets for itself, enforced.

vitalforge_weight/app.py reached 2,056 lines before it was split. Nothing
noticed, because a line count is exactly the kind of thing everyone can see and
nobody is responsible for. This makes it fail instead.

Scoped to the service and shared packages: tests are allowed to be long (a test
file is a list, not a control flow), and the ceiling is about code you have to
hold in your head to change safely.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CEILING = 800

# One module predates this guard. Recorded here rather than raising the ceiling
# for everyone: a named exemption is visible and arguable, a higher global limit
# is neither. Delete an entry when its module is split.

GRANDFATHERED = {
    "shared/auth.py",  # 1,518 lines
}

MODULES = sorted(
    str(p.relative_to(REPO))
    for d in ("vitalforge_weight", "vitalforge_dashboard", "shared")
    for p in (REPO / d).glob("*.py")
)


def test_the_module_list_is_not_empty():
    """A glob that stops matching would make every check below vacuous."""
    assert len(MODULES) >= 10, f"only found {MODULES}; the glob is not matching"


@pytest.mark.parametrize("module", MODULES)
def test_module_is_under_the_line_ceiling(module):
    if module in GRANDFATHERED:
        pytest.skip(f"{module} predates this guard; see GRANDFATHERED")
    n = len((REPO / module).read_text().splitlines())
    assert n <= CEILING, (
        f"{module} is {n} lines, over the {CEILING}-line ceiling this repo sets in "
        "CLAUDE.md. Split it by domain rather than raising the ceiling."
    )
