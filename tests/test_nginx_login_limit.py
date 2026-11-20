"""POST /auth/login is rate limited per client IP at nginx (lane S of the
login lockout/backoff design). The limit is a rate, never a lockout: excess
requests get 429 and nothing is recorded, so one IP cannot lock a victim out.
"""

import re
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "nginx" / "nginx.conf"


def _text() -> str:
    return re.sub(r"(?m)^\s*#.*$", "", CONF.read_text())


def _block(text: str, start: int) -> str:
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
    text = _text()
    return [_block(text, m.start()) for m in re.finditer(r"(?m)^server\s*\{", text)]


def _login_locations() -> list[str]:
    out = []
    for body in _servers():
        m = re.search(r"location\s+=\s+/auth/login\s*\{", body)
        assert m, "server block missing exact-match location for /auth/login"
        out.append(_block(body, m.start()))
    return out


def test_zone_keyed_on_client_ip_at_http_level():
    m = re.search(
        r"(?m)^limit_req_zone\s+\$login_limit_key\s+zone=login:(\d+)m\s+rate=(\d+)r/m;",
        _text(),
    )
    assert m, "limit_req_zone login keyed on $login_limit_key missing"
    assert int(m.group(2)) <= 10, "per-IP login rate must stay low"


def test_key_is_client_ip_for_post_only():
    m = re.search(r"map\s+\$request_method\s+\$login_limit_key\s*\{", _text())
    assert m, "map for $login_limit_key missing"
    body = _block(_text(), m.start())
    assert re.search(r"POST\s+\$binary_remote_addr;", body)
    assert re.search(r'default\s+"";', body)


def test_every_server_limits_login():
    for loc in _login_locations():
        assert re.search(r"limit_req\s+zone=login\s+burst=\d+\s+nodelay;", loc)
        assert re.search(r"limit_req_status\s+429;", loc)


def test_login_location_still_proxies():
    for loc in _login_locations():
        assert "proxy_pass http://vitalforge-" in loc
        assert "proxy_set_header X-Real-IP $remote_addr;" in loc


def test_login_location_does_not_override_add_header():
    for loc in _login_locations():
        assert "add_header" not in loc
