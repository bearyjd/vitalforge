"""In-memory brute-force throttle for POST /auth/login.

Per-IP AND per-username, exponential backoff rather than a hard lockout (a
lockout would let an attacker lock the real user out).  Per-process and in
memory on purpose, like the step-up limiter in ``shared.auth``.  Backoff is
capped so a spoofing attacker can only delay the real user by
``MAX_DELAY_SECONDS``.
"""

import os
from dataclasses import dataclass

from fastapi import Request

FREE_FAILURES = 3
BASE_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 5 * 60
# Failures are forgotten once a key has been quiet this long.
RESET_AFTER_SECONDS = 15 * 60
MAX_TRACKED_KEYS = 10_000
TRUSTED_PROXY_IPS_ENV = "VITALFORGE_TRUSTED_PROXY_IPS"


@dataclass(frozen=True)
class _Entry:
    failures: int
    blocked_until: float
    last_failure: float


_by_ip: dict[str, _Entry] = {}
_by_username: dict[str, _Entry] = {}


def reset() -> None:
    _by_ip.clear()
    _by_username.clear()


def _delay_for(failures: int) -> int:
    if failures < FREE_FAILURES:
        return 0
    return min(BASE_DELAY_SECONDS * 2 ** (failures - FREE_FAILURES), MAX_DELAY_SECONDS)


def _username_key(username: str) -> str:
    return username.strip().lower()[:256]


def client_ip(request: Request) -> str:
    """Peer address; ``X-Forwarded-For`` only when the peer is a trusted proxy.

    nginx overwrites the header with the address it saw, so the rightmost
    entry is the one the trusted hop appended.
    """
    peer = request.client.host if request.client is not None else "unknown"
    trusted = {
        v.strip() for v in os.environ.get(TRUSTED_PROXY_IPS_ENV, "").split(",") if v.strip()
    }
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return peer


def retry_after(ip: str, username: str, now: float) -> int | None:
    """Seconds to wait, or None if this attempt may proceed."""
    waits = []
    for table, key in ((_by_ip, ip), (_by_username, _username_key(username))):
        entry = table.get(key)
        if entry is not None and entry.blocked_until > now:
            waits.append(entry.blocked_until - now)
    if not waits:
        return None
    return max(1, int(max(waits)) + 1)


def _bump(table: dict[str, _Entry], key: str, now: float) -> None:
    prev = table.get(key)
    failures = 1 if prev is None or now - prev.last_failure > RESET_AFTER_SECONDS else prev.failures + 1
    if key not in table and len(table) >= MAX_TRACKED_KEYS:
        _evict(table, now)
    table[key] = _Entry(failures, now + _delay_for(failures), now)


def _evict(table: dict[str, _Entry], now: float) -> None:
    stale = [k for k, e in table.items() if now - e.last_failure > RESET_AFTER_SECONDS]
    for k in stale or [min(table, key=lambda k: table[k].last_failure)]:
        table.pop(k, None)


def record_failure(ip: str, username: str, now: float) -> None:
    _bump(_by_ip, ip, now)
    _bump(_by_username, _username_key(username), now)


def record_success(username: str) -> None:
    """Clear only the username entry; a valid login must not reset the IP's
    count, or an attacker with one account could reset it at will."""
    _by_username.pop(_username_key(username), None)
