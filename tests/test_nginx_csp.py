"""The nginx config ships a Report-Only CSP with no inline allowances."""

import re
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "nginx" / "nginx.conf"


def _policy() -> str:
    match = re.search(
        r'add_header\s+Content-Security-Policy-Report-Only\s+"([^"]+)"\s+always;',
        CONF.read_text(),
    )
    assert match, "Content-Security-Policy-Report-Only header missing"
    return match.group(1)


def test_report_only_header_present_and_not_enforcing():
    text = CONF.read_text()
    assert not re.search(r"add_header\s+Content-Security-Policy\s", text)
    assert _policy()


def test_policy_has_no_inline_allowance():
    policy = _policy()
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy
    assert "nonce-" not in policy


def test_policy_locks_down_core_directives():
    policy = _policy()
    for directive in (
        "default-src 'self'",
        "script-src 'self' https://cdn.jsdelivr.net",
        "style-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in policy
