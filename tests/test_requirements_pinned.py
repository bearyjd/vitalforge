"""Every dashboard requirement is exact-pinned.

An unbounded range (``anthropic>=0.40.0``) lets the resolution baked into the
image drift from the one pip-audit checked, so a green audit says nothing about
what ships.
"""

import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "vitalforge_dashboard" / "requirements.txt"
EXACT_PIN = re.compile(r"^[A-Za-z0-9_.-]+(\[[A-Za-z0-9_,.-]+\])?==[0-9][^\s;,<>=!~]*$")


def _requirement_lines():
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split(" #")[0].strip()
        if line and not line.startswith("#"):
            yield line


def test_every_requirement_is_an_exact_pin():
    unpinned = [line for line in _requirement_lines() if not EXACT_PIN.match(line)]
    assert unpinned == []
