"""In-memory brute-force throttle for POST /auth/login.

Failed logins are tracked per account (case-insensitive username) and per
client IP in a sliding window. Once a key reaches its limit, further attempts
are refused until a lockout expires; each failure past the limit doubles the
lockout up to a cap. Process-local, like the step-up tracker in shared.auth:
each service process throttles on its own.

Attempts are reserved before credentials are verified: ``reserve`` checks the
limits and counts in-flight attempts in one synchronous step (no await), so a
burst of concurrent requests cannot all pass the check before any failure is
recorded. This mirrors ``_reserve_step_up_attempt`` in shared.auth.
"""

import ipaddress
import os
import time
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 15 * 60
ACCOUNT_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
BASE_LOCKOUT_SECONDS = 30
MAX_LOCKOUT_SECONDS = 15 * 60

_failures: dict[tuple[str, str], deque[float]] = {}
_pending: dict[tuple[str, str], int] = {}

# Peers whose X-Real-IP header is trusted (the supplied nginx proxies from
# 127.0.0.1 and sets X-Real-IP from $remote_addr).
TRUSTED_PROXIES = frozenset(
    p.strip() for p in os.environ.get("LOGIN_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()
)


@dataclass(frozen=True)
class Reservation:
    keys: tuple[tuple[str, str], ...]


def client_ip(peer: str | None, real_ip_header: str | None) -> str | None:
    """Address to throttle on, or None when no trustworthy client IP exists.

    A trusted-proxy peer means request.client.host is the proxy, so a shared
    IP bucket would let anyone lock out every login; use the proxy's
    X-Real-IP, and skip the IP bucket if it is absent or malformed.
    """
    if peer is None:
        return None
    if peer not in TRUSTED_PROXIES:
        return peer
    try:
        return str(ipaddress.ip_address((real_ip_header or "").strip()))
    except ValueError:
        return None


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    _failures.clear()
    _pending.clear()


def _keys(ip: str | None, username: str) -> list[tuple[tuple[str, str], int]]:
    keys = [(("user", username.strip().lower()), ACCOUNT_FAILURE_LIMIT)]
    if ip is not None:
        keys.append((("ip", ip), IP_FAILURE_LIMIT))
    return keys


def _live(key: tuple[str, str], now: float) -> deque[float]:
    failures = _failures.get(key)
    if failures is None:
        return deque()
    while failures and failures[0] <= now - WINDOW_SECONDS:
        failures.popleft()
    if not failures:
        del _failures[key]
        return deque()
    return failures


def _lockout_seconds(count: int, limit: int) -> int:
    return min(BASE_LOCKOUT_SECONDS * 2 ** (count - limit), MAX_LOCKOUT_SECONDS)


def retry_after(ip: str | None, username: str) -> int:
    """Seconds the caller must wait before another attempt; 0 when allowed."""
    now = _now()
    wait = 0.0
    for key, limit in _keys(ip, username):
        failures = _live(key, now)
        if len(failures) >= limit:
            until = failures[-1] + _lockout_seconds(len(failures), limit)
            wait = max(wait, until - now)
    return int(wait) + 1 if wait > 0 else 0


def reserve(ip: str | None, username: str) -> Reservation | None:
    """Claim an attempt slot, or None when the caller must be refused (429).

    Synchronous on purpose: the check and the claim must not be separated by an
    await. Below the limit, failures plus in-flight attempts count against it.
    At or past the limit, attempts are refused during the lockout, and after it
    only one probe may be in flight at a time.
    """
    now = _now()
    keys = _keys(ip, username)
    for key, limit in keys:
        failures = _live(key, now)
        pending = _pending.get(key, 0)
        if len(failures) >= limit:
            locked = failures[-1] + _lockout_seconds(len(failures), limit) > now
            if locked or pending:
                return None
        elif len(failures) + pending >= limit:
            return None
    for key, _ in keys:
        _pending[key] = _pending.get(key, 0) + 1
    return Reservation(tuple(key for key, _ in keys))


def release(reservation: Reservation | None) -> None:
    if reservation is None:
        return
    for key in reservation.keys:
        remaining = _pending.get(key, 0) - 1
        if remaining > 0:
            _pending[key] = remaining
        else:
            _pending.pop(key, None)


def record_failure(ip: str | None, username: str) -> None:
    now = _now()
    for key, _ in _keys(ip, username):
        _live(key, now)
        _failures.setdefault(key, deque()).append(now)


def record_success(username: str) -> None:
    _failures.pop(("user", username.strip().lower()), None)
