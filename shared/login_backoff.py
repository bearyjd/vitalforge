"""Login backoff for POST /auth/login (VF-2 of the lockout/backoff redesign).

Invariant: any counter an unauthenticated client can raise is either skipped
for a credential the attacker cannot forge (the signed device cookie) or is a
bounded *delay* that never becomes a hard lock.  Nothing here evicts, spills
into a shared overflow bucket, or resets another account's state.

Lanes (n = failures recorded; the attempt that makes n reach `free` starts the
delay, so exactly `free` attempts are admitted back to back):

    A  (identifier, source)   no valid cookie   free 5   30s * 2^(n-5)  cap 15 min
    B  identifier             no valid cookie   free 10  30s * 2^(n-10) cap 15 min
       counts a failure only while A.n <= P (3), so one source adds at most 3
    D  (identifier, jti)      valid cookie      free 5   2s  * 2^(n-5)  cap 5 min
    G  global token bucket    no valid cookie   5/s, burst 20 (new-device logins only)
    S  per-IP rate            nginx limit_req (VF-1); NOT here -- see "Direct port"

Reserve-before-verify: `reserve()` checks every applicable lane and, if all are
open, counts the attempt as a failure *before* the password hash runs.  A
blocked attempt records nothing (hammering cannot extend a block) and skips the
hash.  `record_success()` clears A (or D), refunds the one B increment this
reservation made (never resets B: a reset would hand a rotating attacker a
fresh budget), and the route then issues/refreshes the device cookie.  check +
increment are synchronous (no await), so a parallel burst cannot over-admit.

Keys are HMACs, fixed length, with the (variable) source last, so a crafted
identifier cannot alias another key.  Lanes depend only on the *submitted*
identifier, never on whether the account exists, so 429s are identical for
real and unknown accounts.

Storage and collision policy: two fixed tables of 65,536 slots (A/B share one;
D has its own), indexed by HMAC(key) mod N.  No eviction, no overflow bucket,
constant memory.  Two keys that collide share one slot and so one counter; the
worst outcome is a delay capped at 15 min that ages out after 1 h idle -- never
a lock.  Slot indices are keyed by a server secret, so an attacker cannot aim
at a victim's slot; blanketing the table takes S-limited traffic from many
sources.  D is a separate table that unauthenticated requests can never touch
(they have no valid signed cookie), so a cookie holder is immune to collisions
caused by attackers.  Success clears its slot, which can also clear a
colliding stranger's count: a harmless loss of throttle, accepted.

Device cookie: stateless, signed with a dedicated salt, 30-day expiry, bound
to the account (idh = first 16 hex of the identifier HMAC, plus uid) and to
the account's session_version.  `parse_device_cookie` does no DB access; the
caller looks up the session_version only after the signature and binding pass,
so a forged cookie costs no DB read.  A bad/expired/mismatched cookie is
treated exactly like no cookie.  A stolen cookie still needs the password and
has its own small backoff (D).

Residual risks (documented, accepted):
- Direct port access bypasses nginx, so lane S does not apply there and this
  module must stand alone: A/B/D/G still bound guessing, but one host can drain
  G (delaying new-device logins, never cookie holders).  The client address is
  taken from X-Real-IP only when the TCP peer is in VITALFORGE_TRUSTED_PROXIES (default loopback) (nginx on the same
  host); otherwise the peer address is used and the header ignored.
- The dashboard (:8086) and weight (:8085) apps are separate processes with
  separate memory: an attacker gets up to twice the budget, and a restart
  clears state.  Single host, so clock skew does not matter.
"""

import hashlib
import hmac
import ipaddress
import math
import os
import secrets
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

from shared.auth import _SECRET

TABLE_SIZE = 65536
DEVICE_COOKIE_NAME = "vf_device"
DEVICE_COOKIE_MAX_AGE = 30 * 24 * 3600
IDENT_MAX_LEN = 256
P = 3  # failures one source may add to the account lane
IDLE_TTL = 3600.0

# lane -> (free attempts, base delay s, cap s)
_LANES = {"A": (5, 30, 900), "B": (10, 30, 900), "D": (5, 2, 300)}
_G_RATE = 5.0
_G_BURST = 20.0

_EMPTY = (0, 0.0, 0.0)  # (n, blocked_until, last)
_ab = [_EMPTY] * TABLE_SIZE
_d = [_EMPTY] * TABLE_SIZE
_g = [_G_BURST, 0.0]  # tokens, last refill

_KEY = hmac.new(_SECRET.encode(), b"login-backoff-key", hashlib.sha256).digest()
_device_serializer = URLSafeTimedSerializer(_SECRET, salt="login-device")


class LoginBlocked(Exception):
    def __init__(self, retry_after: int):
        super().__init__(retry_after)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Reservation:
    ident: str
    a_idx: int | None
    b_idx: int | None  # set only if this reservation incremented B (conditional refund)
    d_idx: int | None


def _clock() -> float:
    return time.monotonic()


def reset() -> None:
    _ab[:] = [_EMPTY] * TABLE_SIZE
    _d[:] = [_EMPTY] * TABLE_SIZE
    _g[:] = [_G_BURST, 0.0]


def _mac(*parts: str) -> bytes:
    return hmac.new(_KEY, "\x00".join(parts).encode(), hashlib.sha256).digest()


def normalize_id(username) -> str:
    text = username if isinstance(username, str) else ""
    return _mac("id", text.strip().lower()[:IDENT_MAX_LEN]).hex()[:32]


def _slot_index(lane: str, ident: str, tail: str = "") -> int:
    # ident is fixed-length hex and `tail` (source/jti) comes last: no aliasing.
    return int.from_bytes(_mac("slot", lane, ident, tail)[:4], "big") % TABLE_SIZE


def _table(lane: str) -> list:
    return _d if lane == "D" else _ab


def lane_n(lane: str, ident: str, src: str = "", jti: str = "") -> int:
    """Current failure count for a lane (diagnostics/tests)."""
    return _live(_table(lane), _slot_index(lane, ident, src or jti))[0]


def _live(table: list, idx: int) -> tuple:
    n, blocked_until, last = table[idx]
    if n and _clock() - last > IDLE_TTL:
        table[idx] = _EMPTY
        return _EMPTY
    return table[idx]


def _delay(lane: str, n: int) -> int:
    free, base, cap = _LANES[lane]
    return min(cap, base * 2 ** min(n - free, 30)) if n >= free else 0


def _bump(table: list, lane: str, idx: int) -> None:
    n = table[idx][0] + 1
    now = _clock()
    table[idx] = (n, now + _delay(lane, n), now)


# Peers allowed to set X-Real-IP.  The standard deployment runs nginx on the HOST
# proxying to 127.0.0.1:8085/8086; through Docker port publishing the container
# sees the bridge gateway (e.g. 172.18.0.1), not loopback.  Default: loopback plus
# RFC1918 and IPv6 ULA.  Override with VITALFORGE_TRUSTED_PROXIES (comma-separated
# CIDRs).  Residual risk: a client reaching the published port directly from a
# trusted range can spoof X-Real-IP, but that only lets it pick its own lane-A key.
_DEFAULT_TRUSTED = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


def _parse_trusted(spec: str) -> list:
    return [ipaddress.ip_network(n.strip(), strict=False) for n in spec.split(",") if n.strip()]


_TRUSTED = _parse_trusted(os.environ.get("VITALFORGE_TRUSTED_PROXIES", _DEFAULT_TRUSTED))


def _is_trusted(peer: str) -> bool:
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net for net in _TRUSTED)


def client_src(peer: str | None, x_real_ip: str | None) -> str:
    """Rate-limit key for the caller: IPv6 by /64, IPv4-mapped unwrapped."""
    raw = peer or "unknown"
    if _is_trusted(raw):
        try:
            ip = ipaddress.ip_address((x_real_ip or "").strip())
        except ValueError:
            if ipaddress.ip_address(raw).is_loopback:
                ip = ipaddress.ip_address(raw)
            else:
                # Proxy hop without a usable X-Real-IP: the peer is shared by every
                # client, so a peer-keyed lane A would be lockable by anyone.  Use a
                # one-off key; lanes B and G still bound guessing.
                return "direct:" + secrets.token_hex(8)
    else:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def issue_device_cookie(ident: str, user_id: int, session_version: int, jti: str | None = None) -> str:
    return _device_serializer.dumps(
        {"uid": user_id, "sv": session_version, "idh": ident[:16], "jti": jti or secrets.token_hex(8)}
    )


def parse_device_cookie(raw, ident: str) -> dict | None:
    """Verify signature, age and account binding.  No DB access.  The caller
    must still compare `sv` with the account's current session_version."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        data = _device_serializer.loads(raw, max_age=DEVICE_COOKIE_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("idh", "")), ident[:16]):
        return None
    if not isinstance(data.get("uid"), int) or not isinstance(data.get("sv"), int):
        return None
    if not isinstance(data.get("jti"), str):
        return None
    return data


def _gate(table: list, idx: int, now: float) -> int:
    blocked_until = _live(table, idx)[1]
    return math.ceil(blocked_until - now) if blocked_until > now else 0


def _take_token(now: float) -> int:
    tokens = min(_G_BURST, _g[0] + max(0.0, now - _g[1]) * _G_RATE) if _g[1] else _G_BURST
    if tokens < 1:
        return 1
    _g[:] = [tokens - 1, now]
    return 0


def reserve(ident: str, src: str, device: dict | None = None) -> Reservation:
    """Check every applicable lane, then count this attempt as a failure.
    Raises LoginBlocked (recording nothing) when any lane is closed."""
    now = _clock()
    if device is not None:
        d_idx = _slot_index("D", ident, device["jti"])
        wait = _gate(_d, d_idx, now)
        if wait:
            raise LoginBlocked(wait)
        _bump(_d, "D", d_idx)
        return Reservation(ident, None, None, d_idx)
    a_idx = _slot_index("A", ident, src)
    b_idx = _slot_index("B", ident)
    wait = max(_gate(_ab, a_idx, now), _gate(_ab, b_idx, now))
    if wait:
        raise LoginBlocked(wait)
    if _take_token(now):
        raise LoginBlocked(1)
    _bump(_ab, "A", a_idx)
    counted = _ab[a_idx][0] <= P
    if counted:
        _bump(_ab, "B", b_idx)
    return Reservation(ident, a_idx, b_idx if counted else None, None)


def record_success(res: Reservation) -> None:
    if res.d_idx is not None:
        _d[res.d_idx] = _EMPTY
    if res.a_idx is not None:
        _ab[res.a_idx] = _EMPTY
    if res.b_idx is not None:
        n, _, last = _ab[res.b_idx]
        # Undo this reservation's delay too, not just its count.
        _ab[res.b_idx] = (n - 1, last + _delay("B", n - 1), last) if n > 1 else _EMPTY
