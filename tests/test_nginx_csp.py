"""The nginx config ships a Report-Only CSP with no inline allowances.

The header must live inside every server block (not at http level, where it
would bleed into other virtual hosts) and no location block may declare its
own add_header (which would silently drop the inherited CSP).
"""

import re
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "nginx" / "nginx.conf"
HEADER_RE = re.compile(
    r'add_header\s+Content-Security-Policy-Report-Only\s+"([^"]+)"\s+always;'
)


def _strip_comments(text: str) -> str:
    return re.sub(r"(?m)^\s*#.*$", "", text)


def _block(text: str, start: int) -> str:
    """Return the body of the brace block whose '{' is at/after `start`."""
    open_idx = text.index("{", start)
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    raise AssertionError("unbalanced braces in nginx.conf")


def _servers() -> list[str]:
    text = _strip_comments(CONF.read_text())
    return [_block(text, m.start()) for m in re.finditer(r"(?m)^server\s*\{", text)]


def _top_level(text: str) -> str:
    """Config with every brace block's contents removed."""
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _policies() -> list[str]:
    servers = _servers()
    assert len(servers) == 2, "expected the weight and dashboard server blocks"
    result = []
    for body in servers:
        match = HEADER_RE.search(body)
        assert match, "server block missing Content-Security-Policy-Report-Only"
        result.append(match.group(1))
    return result


def test_report_only_header_in_every_server_block_not_enforcing():
    assert not re.search(r"add_header\s+Content-Security-Policy\s", CONF.read_text())
    assert _policies()


def test_header_not_at_top_level():
    assert "add_header" not in _top_level(_strip_comments(CONF.read_text()))


def test_no_location_overrides_add_header():
    for body in _servers():
        for m in re.finditer(r"location\s+[^{]+\{", body):
            assert "add_header" not in _block(body, m.start()), (
                "a location with its own add_header drops the inherited CSP"
            )


def test_policy_has_no_inline_allowance():
    for policy in _policies():
        assert "'unsafe-inline'" not in policy
        assert "'unsafe-eval'" not in policy
        assert "nonce-" not in policy


def test_policy_locks_down_core_directives():
    for policy in _policies():
        for directive in (
            "default-src 'self'",
            "script-src 'self' https://cdn.jsdelivr.net/npm/chart.js@4.5.1;",
            "style-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
        ):
            assert directive in policy
        assert "https://cdn.jsdelivr.net " not in policy
        assert "https://cdn.jsdelivr.net;" not in policy


def test_server_policies_identical():
    first, second = _policies()
    assert first == second


def test_docs_warn_about_http_level_header_loss():
    conf = (Path(__file__).resolve().parent.parent / "nginx" / "nginx.conf").read_text()
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for text in (conf, readme):
        assert "http-level" in text or "`http` level" in text
        assert "HSTS" in text and "X-Frame-Options" in text
