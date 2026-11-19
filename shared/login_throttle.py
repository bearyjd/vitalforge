"""In-memory brute-force throttle for POST /auth/login.

Per-IP AND per-username, exponential backoff rather than a hard lockout (a
lockout would let an attacker lock the real user out).  Per-process and in
memory on purpose, like the step-up limiter in ``shared.auth``.  Backoff is
capped so a spoofing attacker can only delay the real user by
``MAX_DELAY_SECONDS``.

Proxy trust is explicit: ``VITALFORGE_TRUSTED_PROXY_IPS`` lists the exact
addresses of the nginx hop.  It is validated at startup
(``validate_trusted_proxy_config``, called from ``add_auth_routes``): a
malformed entry stops the service instead of silently trusting nothing.

Fail safe, never a shared bucket: a loopback/private peer that is not a trusted
proxy is almost certainly an unconfigured reverse proxy, and a trusted proxy
that sent no valid ``X-Forwarded-For`` (or a loopback/private one, e.g. a front
proxy on the host reaching nginx over loopback) gives no client address either.
In all these cases ``client_ip`` returns ``None`` and the IP bucket is skipped (the username
bucket still applies) rather than putting every client in one bucket.

There is deliberately no "clean-IP probe" slot, and a successful login does not
clear the username bucket: either would hand an IP-rotating attacker extra
guesses.  Real users are protected by the known-device bypass and the capped
backoff; a device that never logged in is delayed while an attack lasts.
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


# When the username table is full of live (non-stale) keys, failures for
# untracked usernames go to this one shared entry instead of evicting a live
# key: evicting would reset a blocked key's backoff (fail closed).  Usernames
# are truncated to 256 characters, so no real key can equal it.  The IP table
# has no overflow entry (it would be one IP bucket shared by every new client):
# an untracked IP while it is full simply gets no IP bucket.
_OVERFLOW_KEY = "\x00overflow" + "\x00" * 256

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
    # The pending-count keys actually incremented (the overflow key when the
    # table was full); ``release`` decrements exactly these.
    ip_slot: str | None = None
    username_slot: str | None = None


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


def parse_trusted_proxies(raw: str) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse the comma-separated proxy list; raise ValueError on any entry that
    is not a single IP address (no hostnames, ports or CIDR ranges)."""
    entries = [v.strip() for v in raw.split(",") if v.strip()]
    try:
        return frozenset(ipaddress.ip_address(v) for v in entries)
    except ValueError as exc:
        raise ValueError(
            f"{TRUSTED_PROXY_IPS_ENV} must be a comma-separated list of single IP addresses "
            f"(the nginx hop as the app sees it; no hostnames, ports or CIDR ranges): {exc}"
        ) from exc


def validate_trusted_proxy_config() -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Startup check: fail fast on a malformed value and log the effective mode."""
    proxies = parse_trusted_proxies(os.environ.get(TRUSTED_PROXY_IPS_ENV, ""))
    if proxies:
        logger.info(
            "Login throttle: X-Forwarded-For trusted only from %s",
            ", ".join(sorted(str(p) for p in proxies)),
        )
    else:
        logger.warning(
            "Login throttle: %s is unset, so X-Forwarded-For is ignored. Fine for direct "
            "connections; behind nginx set it to the address the app sees nginx connect from, "
            "or the per-IP throttle is skipped for that proxy (per-username throttle only).",
            TRUSTED_PROXY_IPS_ENV,
        )
    return proxies


def client_ip(request: Request) -> str | None:
    """Throttle key for the client, or ``None`` when no trustworthy IP exists.

    ``X-Forwarded-For`` is honoured only from a trusted proxy peer; nginx
    overwrites the header with the address it saw, so the rightmost entry is
    the one the trusted hop appended.  It must be a valid IP; a missing or
    invalid value gives ``None``, never the proxy's own (shared) address.  A
    loopback/private peer that is not trusted is treated as an unconfigured
    reverse proxy: ``None`` (skip the IP bucket) rather than one bucket shared
    by every client.
    """
    peer = request.client.host if request.client is not None else None
    if peer is None:
        return None
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if peer_addr in _trusted_proxies():
        return _forwarded_client_key(request.headers.get("x-forwarded-for", ""))
    if _is_private(peer_addr):
        _warn_if_proxy_peer(peer)
        return None
    return _bucket_key(peer_addr)


def _forwarded_client_key(forwarded: str) -> str | None:
    """Bucket key for the rightmost ``X-Forwarded-For`` entry from a trusted hop.

    A loopback/private forwarded address gets the same fail-safe as a private
    peer: a front proxy on the host (cloudflared, Caddy, Tailscale) that reaches
    nginx over loopback makes ``$remote_addr`` 127.0.0.1 for every client, and
    that must not become one bucket shared by everyone.  The public address of
    a CDN edge in front of nginx cannot be told apart this way; trust it in
    nginx (``set_real_ip_from``) so ``$remote_addr`` is the real client.
    """
    last = forwarded.split(",")[-1].strip()
    try:
        addr = ipaddress.ip_address(last)
    except ValueError:
        return None
    if _is_private(addr):
        _warn_if_proxy_peer(str(addr))
        return None
    return _bucket_key(addr)


def _trusted_proxies() -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return parse_trusted_proxies(os.environ.get(TRUSTED_PROXY_IPS_ENV, ""))


def _warn_if_proxy_peer(peer: str) -> None:
    """Warn (once) that a private/loopback client address disables the IP bucket."""
    global _proxy_warned
    if _proxy_warned:
        return
    _proxy_warned = True
    logger.warning(
        "Login from private/loopback address %s (an untrusted peer, or the X-Forwarded-For "
        "of a trusted one): the per-IP login throttle is skipped for such addresses (only "
        "the per-username throttle applies). If this is a reverse proxy, set %s to the "
        "address the app sees it connect from; if another proxy sits in front of nginx, "
        "configure nginx real_ip for it so $remote_addr is the real client.",
        peer,
        TRUSTED_PROXY_IPS_ENV,
    )


def _wait_for(
    table: dict[str, _Entry], pending: dict[str, int], key: str, now: float
) -> float | None:
    """Seconds to wait on this key, or None if an attempt may proceed.

    In-flight attempts count toward the free-failure allowance, and once the
    allowance is spent only one attempt per key may be in flight.
    """
    slot = _resolve_key(table, key, now)
    if slot is None:
        return None
    key = slot
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
    ip_slot = _resolve_key(_by_ip, ip, now) if ip is not None else None
    user_slot = _resolve_key(_by_username, ukey, now) if use_username else None
    if ip_slot is not None:
        _pending_ip[ip_slot] = _pending_ip.get(ip_slot, 0) + 1
    if user_slot is not None:
        _pending_username[user_slot] = _pending_username.get(user_slot, 0) + 1
    return Reservation(ip, ukey if use_username else None, ip_slot, user_slot)


def _decrement(pending: dict[str, int], key: str) -> None:
    left = pending.get(key, 0) - 1
    if left > 0:
        pending[key] = left
    else:
        pending.pop(key, None)


def release(reservation: Reservation) -> None:
    if reservation.ip_slot is not None:
        _decrement(_pending_ip, reservation.ip_slot)
    if reservation.username_slot is not None:
        _decrement(_pending_username, reservation.username_slot)


def retry_after(ip: str | None, username: str, now: float) -> int | None:
    """Seconds to wait, or None if this attempt may proceed."""
    waits = []
    keys = [(_by_username, _username_key(username))]
    if ip is not None:
        keys.append((_by_ip, ip))
    for table, key in keys:
        slot = _resolve_key(table, key, now)
        entry = table.get(slot) if slot is not None else None
        if entry is not None and entry.blocked_until > now:
            waits.append(entry.blocked_until - now)
    if not waits:
        return None
    return max(1, int(max(waits)) + 1)


def _tracked_count(table: dict[str, _Entry]) -> int:
    return len(table) - (1 if _OVERFLOW_KEY in table else 0)


def _resolve_key(table: dict[str, _Entry], key: str, now: float) -> str | None:
    """The key to read/write for ``key`` when it is untracked and the table is
    full of live entries: the overflow key for the username table, ``None``
    (skip the bucket, never a shared one) for the IP table.

    Only stale entries (quiet for ``RESET_AFTER_SECONDS``) are ever evicted;
    a live entry, above all one with an active block, is never dropped to make
    room, so flooding the table cannot reset a target's backoff.  The table is
    bounded at ``MAX_TRACKED_KEYS`` plus the one overflow entry.
    """
    if key in table or _tracked_count(table) < MAX_TRACKED_KEYS:
        return key
    _evict_stale(table, now)
    if _tracked_count(table) < MAX_TRACKED_KEYS:
        return key
    return _OVERFLOW_KEY if table is _by_username else None


def _bump(table: dict[str, _Entry], key: str, now: float) -> None:
    slot = _resolve_key(table, key, now)
    if slot is None or (slot == _OVERFLOW_KEY and slot not in table):
        logger.warning(
            "Login throttle %s table is full of live entries (%d): %s until entries "
            "decay (possible flood).",
            "IP" if table is _by_ip else "username",
            MAX_TRACKED_KEYS,
            "new IPs get no per-IP bucket" if slot is None else "new usernames share one overflow bucket",
        )
    if slot is None:
        return
    prev = table.get(slot)
    failures = 1 if prev is None or now - prev.last_failure > RESET_AFTER_SECONDS else prev.failures + 1
    table[slot] = _Entry(failures, now + _delay_for(failures), now)


def _evict_stale(table: dict[str, _Entry], now: float) -> None:
    stale = [k for k, e in table.items() if now - e.last_failure > RESET_AFTER_SECONDS]
    for k in stale:
        table.pop(k, None)


def record_failure(ip: str | None, username: str, now: float) -> None:
    if ip is not None:
        _bump(_by_ip, ip, now)
    _bump(_by_username, _username_key(username), now)


def record_success(username: str, ip: str | None = None, now: float | None = None) -> None:
    """Remember the (ip, username) pair as a known device.

    Neither bucket is cleared: resetting the IP's count would let an attacker
    with one account reset it at will, and resetting the username's count would
    give an IP-rotating attacker a fresh free-failure allowance after every
    legitimate login.  Both decay after ``RESET_AFTER_SECONDS`` of quiet."""
    ukey = _username_key(username)
    if ip is not None and now is not None:
        if (ip, ukey) not in _known_devices and len(_known_devices) >= MAX_TRACKED_KEYS:
            oldest = min(_known_devices, key=_known_devices.__getitem__)
            _known_devices.pop(oldest, None)
        _known_devices[(ip, ukey)] = now
