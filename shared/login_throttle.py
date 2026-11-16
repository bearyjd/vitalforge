"""In-memory brute-force throttle for POST /auth/login.

Failed logins are tracked per account (case-insensitive username) and per
client IP in a sliding window. Once a key reaches its limit, further attempts
are refused until a lockout expires; each failure past the limit doubles the
lockout up to a cap. Process-local, like the step-up tracker in shared.auth:
each service process throttles on its own.
"""

import time
from collections import deque

WINDOW_SECONDS = 15 * 60
ACCOUNT_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
BASE_LOCKOUT_SECONDS = 30
MAX_LOCKOUT_SECONDS = 15 * 60

_failures: dict[tuple[str, str], deque[float]] = {}


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    _failures.clear()


def _keys(ip: str, username: str) -> list[tuple[tuple[str, str], int]]:
    return [(("ip", ip), IP_FAILURE_LIMIT), (("user", username.strip().lower()), ACCOUNT_FAILURE_LIMIT)]


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


def retry_after(ip: str, username: str) -> int:
    """Seconds the caller must wait before another attempt; 0 when allowed."""
    now = _now()
    wait = 0.0
    for key, limit in _keys(ip, username):
        failures = _live(key, now)
        if len(failures) >= limit:
            until = failures[-1] + _lockout_seconds(len(failures), limit)
            wait = max(wait, until - now)
    return int(wait) + 1 if wait > 0 else 0


def record_failure(ip: str, username: str) -> None:
    now = _now()
    for key, _ in _keys(ip, username):
        _live(key, now)
        _failures.setdefault(key, deque()).append(now)


def record_success(username: str) -> None:
    _failures.pop(("user", username.strip().lower()), None)
