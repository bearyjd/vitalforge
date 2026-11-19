"""In-memory brute-force throttle for POST /auth/login.

Per-IP AND per-username, exponential backoff rather than a hard lockout (a
lockout would let an attacker lock the real user out).  Per-process and in
memory on purpose, like the step-up limiter in ``shared.auth``.  Backoff is
capped so a spoofing attacker can only delay the real user by
``MAX_DELAY_SECONDS``.
"""

import ipaddress
import logging
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
# An (ip, username) pair that logged in successfully recently is a "known
# device": it bypasses the shared per-username bucket (still subject to its own
# IP bucket) so an attacker cannot keep the real user blocked.
KNOWN_DEVICE_TTL_SECONDS = 30 * 24 * 3600
# While a username is backed off, one attempt per interval from an IP with no
# recorded failures still reaches the password check, so an attacker rotating
# source IPs cannot keep the real user locked out indefinitely.  The price is
# at most one extra guess per username per interval.
CLEAN_IP_PROBE_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Entry:
    failures: int
    blocked_until: float
    last_failure: float


_by_ip: dict[str, _Entry] = {}
_by_username: dict[str, _Entry] = {}
# In-flight (reserved, not yet resolved) attempts, so a concurrent burst cannot
# all pass the check before any failure is recorded.
_pending_ip: dict[str, int] = {}
_pending_username: dict[str, int] = {}
_known_devices: dict[tuple[str, str], float] = {}
_last_probe: dict[str, float] = {}
_proxy_warned = False


class ThrottledError(Exception):
    def __init__(self, wait: int, ip_blocked: bool = False) -> None:
        super().__init__(f"throttled for {wait}s")
        self.wait = wait
        self.ip_blocked = ip_blocked


@dataclass(frozen=True)
class Reservation:
    ip: str
    username_key: str | None  # None when the username bucket is bypassed


def reset() -> None:
    global _proxy_warned
    _by_ip.clear()
    _by_username.clear()
    _pending_ip.clear()
    _pending_username.clear()
    _known_devices.clear()
    _last_probe.clear()
    _proxy_warned = False


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
    trusted = _trusted_proxies()
    if not trusted:
        _warn_if_proxy_peer(peer)
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return peer


def _trusted_proxies() -> set[str]:
    raw = os.environ.get(TRUSTED_PROXY_IPS_ENV, "")
    return {v.strip() for v in raw.split(",") if v.strip()}


def warn_if_shared_proxy_bucket(request: Request) -> None:
    """Called when an IP bucket blocks a request.  A block on a private/loopback
    peer that is not a trusted proxy usually means a reverse proxy (or a wrong
    trust list) is collapsing every client into this one bucket.  Logged on
    every such block, not once, so a wrong-but-non-empty trust list is visible."""
    peer = request.client.host if request.client is not None else "unknown"
    if peer in _trusted_proxies():
        return
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return
    if addr.is_loopback or addr.is_private:
        logger.warning(
            "Login IP bucket blocked untrusted private/loopback peer %s: if this is your "
            "reverse proxy, every user shares this bucket and one client can block all "
            "logins. Set %s to the address the app sees the proxy connect from "
            "(the docker bridge gateway when nginx runs on the host).",
            peer,
            TRUSTED_PROXY_IPS_ENV,
        )


def _warn_if_proxy_peer(peer: str) -> None:
    """Loopback/private peer with no trusted proxy configured usually means a
    reverse proxy: every user then shares one IP bucket."""
    global _proxy_warned
    if _proxy_warned:
        return
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return
    if addr.is_loopback or addr.is_private:
        _proxy_warned = True
        logger.warning(
            "Login from private/loopback peer %s and %s is unset: if this is a reverse "
            "proxy, all users share one login-throttle IP bucket. Set %s to the proxy "
            "address(es).",
            peer,
            TRUSTED_PROXY_IPS_ENV,
            TRUSTED_PROXY_IPS_ENV,
        )


def _wait_for(
    table: dict[str, _Entry], pending: dict[str, int], key: str, now: float
) -> float | None:
    """Seconds to wait on this key, or None if an attempt may proceed.

    In-flight attempts count toward the free-failure allowance, and once the
    allowance is spent only one attempt per key may be in flight.
    """
    entry = table.get(key)
    waiting = pending.get(key, 0)
    if entry is not None and entry.blocked_until > now:
        return entry.blocked_until - now
    failures = 0 if entry is None or now - entry.last_failure > RESET_AFTER_SECONDS else entry.failures
    if waiting and failures + waiting >= FREE_FAILURES:
        return float(BASE_DELAY_SECONDS)
    return None


def _is_known_device(ip: str, username_key: str, now: float) -> bool:
    seen = _known_devices.get((ip, username_key))
    return seen is not None and now - seen <= KNOWN_DEVICE_TTL_SECONDS


def _ip_is_clean(ip: str, now: float) -> bool:
    entry = _by_ip.get(ip)
    return entry is None or now - entry.last_failure > RESET_AFTER_SECONDS


def _claim_probe_slot(ukey: str, now: float) -> bool:
    last = _last_probe.get(ukey)
    if last is not None and now - last < CLEAN_IP_PROBE_INTERVAL_SECONDS:
        return False
    if ukey not in _last_probe and len(_last_probe) >= MAX_TRACKED_KEYS:
        stale = [k for k, t in _last_probe.items() if now - t >= CLEAN_IP_PROBE_INTERVAL_SECONDS]
        for k in stale or [min(_last_probe, key=_last_probe.__getitem__)]:
            _last_probe.pop(k, None)
    _last_probe[ukey] = now
    return True


def reserve(ip: str, username: str, now: float) -> Reservation:
    """Check and reserve in one synchronous step (no await between them).

    Raises ThrottledError, otherwise returns a Reservation that the caller must
    ``release`` in a ``finally`` block.
    """
    ukey = _username_key(username)
    use_username = not _is_known_device(ip, ukey, now)
    ip_wait = _wait_for(_by_ip, _pending_ip, ip, now)
    user_wait = _wait_for(_by_username, _pending_username, ukey, now) if use_username else None
    if ip_wait is not None:
        raise ThrottledError(max(1, int(max(ip_wait, user_wait or 0)) + 1), ip_blocked=True)
    if user_wait is not None:
        entry = _by_username.get(ukey)
        backed_off = entry is not None and entry.blocked_until > now
        if not (backed_off and _ip_is_clean(ip, now) and _claim_probe_slot(ukey, now)):
            raise ThrottledError(max(1, int(user_wait) + 1))
    _pending_ip[ip] = _pending_ip.get(ip, 0) + 1
    if use_username:
        _pending_username[ukey] = _pending_username.get(ukey, 0) + 1
    return Reservation(ip, ukey if use_username else None)


def _decrement(pending: dict[str, int], key: str) -> None:
    left = pending.get(key, 0) - 1
    if left > 0:
        pending[key] = left
    else:
        pending.pop(key, None)


def release(reservation: Reservation) -> None:
    _decrement(_pending_ip, reservation.ip)
    if reservation.username_key is not None:
        _decrement(_pending_username, reservation.username_key)


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


def record_success(username: str, ip: str | None = None, now: float | None = None) -> None:
    """Clear only the username entry; a valid login must not reset the IP's
    count, or an attacker with one account could reset it at will.  With ``ip``,
    also remember the (ip, username) pair as a known device."""
    ukey = _username_key(username)
    _by_username.pop(ukey, None)
    if ip is not None and now is not None:
        if (ip, ukey) not in _known_devices and len(_known_devices) >= MAX_TRACKED_KEYS:
            oldest = min(_known_devices, key=_known_devices.__getitem__)
            _known_devices.pop(oldest, None)
        _known_devices[(ip, ukey)] = now
