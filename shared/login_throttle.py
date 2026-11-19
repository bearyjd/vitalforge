"""In-memory brute-force throttle for POST /auth/login.

Per-IP AND per-username, exponential backoff rather than a hard lockout (a
lockout would let an attacker lock the real user out).  Per-process and in
memory on purpose, like the step-up limiter in ``shared.auth``.  Backoff is
capped so a spoofing attacker can only delay the real user by
``MAX_DELAY_SECONDS``.

Fail safe: a loopback/private peer that is not a trusted proxy is almost
certainly a reverse proxy, so every client would collapse into one shared IP
bucket.  ``client_ip`` returns ``None`` for such peers and the IP bucket is
skipped (the username bucket still applies).  There is deliberately no
"clean-IP probe" slot: a single first-come slot per username is claimed by an
IP-rotating attacker and multiplies its guesses without helping real users;
real users are protected by the known-device bypass and the capped backoff.
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
_proxy_warned = False


class ThrottledError(Exception):
    def __init__(self, wait: int) -> None:
        super().__init__(f"throttled for {wait}s")
        self.wait = wait


@dataclass(frozen=True)
class Reservation:
    ip: str | None  # None when the IP bucket is skipped (untrusted proxy peer)
    username_key: str | None  # None when the username bucket is bypassed


def reset() -> None:
    global _proxy_warned
    _by_ip.clear()
    _by_username.clear()
    _pending_ip.clear()
    _pending_username.clear()
    _known_devices.clear()
    _proxy_warned = False


def _delay_for(failures: int) -> int:
    if failures < FREE_FAILURES:
        return 0
    return min(BASE_DELAY_SECONDS * 2 ** (failures - FREE_FAILURES), MAX_DELAY_SECONDS)


def _username_key(username: str) -> str:
    return username.strip().lower()[:256]


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_loopback or addr.is_private


def _bucket_key(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """IPv6 clients are bucketed by /64 (one subscriber holds a whole /64, so
    per-/128 keys would give an attacker unlimited fresh IPs)."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def client_ip(request: Request) -> str | None:
    """Throttle key for the client, or ``None`` when no trustworthy IP exists.

    ``X-Forwarded-For`` is honoured only from a trusted proxy peer; nginx
    overwrites the header with the address it saw, so the rightmost entry is
    the one the trusted hop appended.  It is validated as an IP; anything else
    falls back to the peer.  A loopback/private peer that is not trusted is
    treated as an unconfigured reverse proxy: ``None`` (skip the IP bucket)
    rather than one bucket shared by every client.
    """
    peer = request.client.host if request.client is not None else None
    if peer is None:
        return None
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if peer in _trusted_proxies():
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        try:
            return _bucket_key(ipaddress.ip_address(last))
        except ValueError:
            return _bucket_key(peer_addr)
    if _is_private(peer_addr):
        _warn_if_proxy_peer(peer)
        return None
    return _bucket_key(peer_addr)


def _trusted_proxies() -> set[str]:
    raw = os.environ.get(TRUSTED_PROXY_IPS_ENV, "")
    return {v.strip() for v in raw.split(",") if v.strip()}


def _warn_if_proxy_peer(peer: str) -> None:
    """Warn (once) that a private/loopback untrusted peer disables the IP bucket."""
    global _proxy_warned
    if _proxy_warned:
        return
    _proxy_warned = True
    logger.warning(
        "Login from private/loopback peer %s that is not in %s: the per-IP login throttle "
        "is skipped for such peers (only the per-username throttle applies). If this is a "
        "reverse proxy, set %s to the address the app sees it connect from.",
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


def reserve(ip: str | None, username: str, now: float) -> Reservation:
    """Check and reserve in one synchronous step (no await between them).

    ``ip`` is None when the IP bucket does not apply.  Raises ThrottledError,
    otherwise returns a Reservation that the caller must ``release`` in a
    ``finally`` block.
    """
    ukey = _username_key(username)
    use_username = ip is None or not _is_known_device(ip, ukey, now)
    ip_wait = _wait_for(_by_ip, _pending_ip, ip, now) if ip is not None else None
    user_wait = _wait_for(_by_username, _pending_username, ukey, now) if use_username else None
    waits = [w for w in (ip_wait, user_wait) if w is not None]
    if waits:
        raise ThrottledError(max(1, int(max(waits)) + 1))
    if ip is not None:
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
    if reservation.ip is not None:
        _decrement(_pending_ip, reservation.ip)
    if reservation.username_key is not None:
        _decrement(_pending_username, reservation.username_key)


def retry_after(ip: str | None, username: str, now: float) -> int | None:
    """Seconds to wait, or None if this attempt may proceed."""
    waits = []
    keys = [(_by_username, _username_key(username))]
    if ip is not None:
        keys.append((_by_ip, ip))
    for table, key in keys:
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


def record_failure(ip: str | None, username: str, now: float) -> None:
    if ip is not None:
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
