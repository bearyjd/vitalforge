"""VF-2 login backoff (lanes A/B/D/G + device cookie), see shared/login_backoff.py.

Unit tests drive `reserve`/`record_success` with a fake clock; route tests go
through POST /auth/login on a throwaway app, like test_user_management.py.
"""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shared import auth as shared_auth
from shared import login_backoff as lb
from shared.auth_routes import add_auth_routes
from shared.database import get_db
from tests.conftest import seed_user

PW = "correct-horse-battery"


@pytest.fixture
def clock(monkeypatch):
    t = [10_000.0]
    monkeypatch.setattr(lb, "_clock", lambda: t[0])
    return t


def _fail(ident, src, device=None):
    """One reserved (=failed) attempt; success is recorded separately."""
    return lb.reserve(ident, src, device)


def _drive(clock, ident, src, n, device=None):
    """n reservations, waiting out any block; returns the max Retry-After seen."""
    worst = 0
    for _ in range(n):
        while True:
            try:
                _fail(ident, src, device)
                break
            except lb.LoginBlocked as exc:
                worst = max(worst, exc.retry_after)
                clock[0] += exc.retry_after
        clock[0] += 0.3  # keep the global bucket (5/s) from being the limiter
    return worst


def _dev(ident, uid=1, sv=0, jti="j1"):
    return lb.parse_device_cookie(lb.issue_device_cookie(ident, uid, sv, jti), ident)


# --- curve / cap ---------------------------------------------------------------


def test_pair_lane_backoff_curve_and_cap(clock):
    ident = lb.normalize_id("alice")
    for _ in range(4):
        _fail(ident, "1.1.1.1")
    delays = []
    for _ in range(9):
        _fail(ident, "1.1.1.1")  # the 5th attempt is the first to set a delay
        # after each reservation the *next* attempt is gated
        try:
            _fail(ident, "1.1.1.1")
            gate = 0
        except lb.LoginBlocked as exc:
            gate = exc.retry_after
            clock[0] += gate
        delays.append(gate)
    assert delays[:6] == [30, 60, 120, 240, 480, 900]
    assert max(delays) == 900


def test_account_delay_capped_and_never_hard_lock(clock):
    ident = lb.normalize_id("victim")
    worst = 0
    for i in range(1000):
        worst = max(worst, _drive(clock, ident, f"10.{i // 250}.{i % 250}.1", 1))
    assert 0 < worst <= 900
    # Still not a hard lock: waiting the advertised time admits the next attempt.
    assert lb.lane_n("B", ident) > 10


def test_one_source_contributes_at_most_P_to_account_lane(clock):
    ident = lb.normalize_id("victim")
    _drive(clock, ident, "9.9.9.9", 8)
    assert lb.lane_n("B", ident) == 3
    clock[0] += 0.3
    _fail(ident, "8.8.8.8")  # a second IP is not blocked by B


def test_username_spray_does_not_block_other_accounts(clock):
    for i in range(20_000):
        _fail(lb.normalize_id(f"spray{i}"), f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
        if i % 5 == 4:
            clock[0] += 1
    fresh = lb.normalize_id("fresh-account")
    _fail(fresh, "203.0.113.7")
    assert lb.TABLE_SIZE == 65536


def test_success_refunds_without_reset(clock):
    ident = lb.normalize_id("bob")
    for i in range(9):
        _fail(ident, f"1.0.0.{i}")
        clock[0] += 0.3
    assert lb.lane_n("B", ident) == 9
    res = _fail(ident, "1.0.0.99")
    lb.record_success(res)
    assert lb.lane_n("B", ident) == 9  # own reservation refunded, not reset
    clock[0] += 0.3
    _fail(ident, "1.0.0.100")
    assert lb.lane_n("B", ident) == 10


def test_refund_only_when_account_lane_was_incremented(clock):
    ident = lb.normalize_id("carol")
    for _ in range(3):
        _fail(ident, "2.2.2.2")
    _fail(ident, "3.3.3.3")
    assert lb.lane_n("B", ident) == 4
    res = _fail(ident, "2.2.2.2")  # 4th from this source: A.n > P, B untouched
    assert lb.lane_n("B", ident) == 4
    lb.record_success(res)
    assert lb.lane_n("B", ident) == 4
    assert lb.lane_n("A", ident, "2.2.2.2") == 0  # success clears the pair lane


def test_blocked_attempt_records_nothing(clock):
    ident = lb.normalize_id("dave")
    for _ in range(5):
        _fail(ident, "4.4.4.4")
    with pytest.raises(lb.LoginBlocked) as first:
        _fail(ident, "4.4.4.4")
    n = lb.lane_n("A", ident, "4.4.4.4")
    for _ in range(50):
        with pytest.raises(lb.LoginBlocked) as again:
            _fail(ident, "4.4.4.4")
    assert lb.lane_n("A", ident, "4.4.4.4") == n == 5
    assert again.value.retry_after == first.value.retry_after


def test_entry_expires_after_one_hour_idle(clock):
    ident = lb.normalize_id("erin")
    for _ in range(5):
        _fail(ident, "5.5.5.5")
    clock[0] += 3601
    assert lb.lane_n("A", ident, "5.5.5.5") == 0
    _fail(ident, "5.5.5.5")


# --- device cookie / lane D / lane G ---------------------------------------------


def test_stolen_cookie_gets_its_own_small_backoff(clock):
    ident = lb.normalize_id("frank")
    dev = _dev(ident)
    for _ in range(4):
        _fail(ident, "6.6.6.6", dev)
    worst = _drive(clock, ident, "6.6.6.6", 12, dev)
    assert worst <= 300  # D cap is 5 minutes, not the 15 of A/B
    with pytest.raises(lb.LoginBlocked) as exc:
        for _ in range(12):
            _fail(ident, "6.6.6.6", dev)
    assert exc.value.retry_after <= 300
    assert lb.lane_n("B", ident) == 0 and lb.lane_n("A", ident, "6.6.6.6") == 0


def test_cookie_holder_not_blocked_by_attacker_on_same_source(clock):
    ident = lb.normalize_id("grace")
    for _ in range(30):
        try:
            _fail(ident, "7.7.7.7")
        except lb.LoginBlocked:
            pass
    _fail(ident, "7.7.7.7", _dev(ident))


def test_cookie_bound_to_account():
    x, y = lb.normalize_id("x"), lb.normalize_id("y")
    raw = lb.issue_device_cookie(x, 1, 0, "j")
    assert lb.parse_device_cookie(raw, x) is not None
    assert lb.parse_device_cookie(raw, y) is None


def test_invalid_cookie_equals_no_cookie():
    ident = lb.normalize_id("h")
    raw = lb.issue_device_cookie(ident, 1, 0, "j")
    for bad in ("", "garbage", raw[:-2] + "xx", raw + "A", None):
        assert lb.parse_device_cookie(bad, ident) is None


def test_expired_cookie_rejected(clock, monkeypatch):
    ident = lb.normalize_id("i")
    raw = lb.issue_device_cookie(ident, 1, 0, "j")
    assert lb.parse_device_cookie(raw, ident) is not None
    import time as _time

    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 31 * 86400)
    assert lb.parse_device_cookie(raw, ident) is None


def test_global_bucket_exempts_cookie_holders(clock):
    blocked = 0
    for i in range(200):  # no clock advance: drains the 5/s bucket
        try:
            _fail(lb.normalize_id(f"u{i}"), f"11.0.{i // 250}.{i % 250}")
        except lb.LoginBlocked:
            blocked += 1
    assert blocked > 0
    ident = lb.normalize_id("holder")
    _fail(ident, "12.0.0.1", _dev(ident))


# --- identifiers, sources, collisions ---------------------------------------------


def test_crafted_identifier_cannot_alias_other_key():
    ids = {lb.normalize_id(s) for s in ("a", "a|b", "a:b", "a\x00b", "a" * 300, "a" * 256 + "z")}
    assert len(ids) == 5  # the over-long pair is capped to the same 256-char prefix
    assert all(len(i) == 32 for i in ids)
    assert lb.normalize_id("  Alice ") == lb.normalize_id("alice")


def test_src_ipv6_is_masked_to_64_and_mapped_v4_unwrapped():
    peer = "127.0.0.1"
    assert lb.client_src(peer, "2001:db8:1:2:aaaa::1") == lb.client_src(peer, "2001:db8:1:2:bbbb::9")
    assert lb.client_src(peer, "2001:db8:1:3::1") != lb.client_src(peer, "2001:db8:1:2::1")
    assert lb.client_src(peer, "::ffff:1.2.3.4") == lb.client_src(peer, "1.2.3.4")
    assert lb.client_src(peer, "not-an-ip") == lb.client_src(peer, None) == "127.0.0.1"


def test_x_real_ip_ignored_unless_peer_is_loopback():
    # direct-port access: the header is attacker-controlled, the peer is not
    assert lb.client_src("198.51.100.9", "1.2.3.4") == "198.51.100.9"
    assert lb.client_src("::1", "1.2.3.4") == "1.2.3.4"


def test_x_real_ip_trusted_from_configured_docker_gateway(monkeypatch):
    peer = "172.18.0.1"
    assert lb.client_src(peer, "203.0.113.9") == peer  # default list: gateway untrusted
    monkeypatch.setattr(lb, "_TRUSTED", [lb.ipaddress.ip_network("172.18.0.1/32")])
    assert lb.client_src(peer, "203.0.113.9") == "203.0.113.9"
    assert lb.client_src("172.18.0.2", "203.0.113.9") == "172.18.0.2"


def test_collision_shares_capped_delay_never_a_lock(clock, monkeypatch):
    """Collision policy: colliding keys share one slot (and so one counter),
    which is only ever a capped delay that ages out; cookie-holder lane D
    lives in a separate table that unauthenticated traffic cannot reach."""
    monkeypatch.setattr(lb, "_slot_index", lambda *parts: 7)
    a, b = lb.normalize_id("a"), lb.normalize_id("b")
    worst = _drive(clock, a, "1.1.1.1", 30)
    assert worst <= 900
    for _ in range(3):
        try:
            _fail(b, "2.2.2.2")
        except lb.LoginBlocked as exc:
            assert exc.retry_after <= 900
            clock[0] += exc.retry_after
    clock[0] += 3601
    _fail(b, "2.2.2.2")  # aged out
    _fail(b, "2.2.2.2", _dev(b))  # D unaffected by A/B pressure


# --- routes -----------------------------------------------------------------------


@pytest.fixture
async def client(initialized_db):
    app = FastAPI()
    add_auth_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _login(client, user, pw, ip="198.51.100.1", cookies=None):
    return await client.post(
        "/auth/login",
        json={"username": user, "password": pw},
        headers={"X-Real-IP": ip},
        cookies=cookies or {},
    )


async def test_single_source_cannot_lock_victim_with_cookie(client):
    await seed_user("victim", password=PW)
    ok = await _login(client, "victim", PW)
    assert ok.status_code == 200
    device = ok.cookies.get("vf_device")
    assert device
    client.cookies.clear()
    for _ in range(100):
        await _login(client, "victim", "wrong-guess-xx")
    assert (await _login(client, "victim", "wrong-guess-xx")).status_code == 429
    assert (await _login(client, "victim", PW)).status_code == 429  # no cookie: blocked
    client.cookies.clear()
    resp = await _login(client, "victim", PW, cookies={"vf_device": device})
    assert resp.status_code == 200


async def test_429_identical_for_existing_and_unknown_user(client):
    await seed_user("real", password=PW)
    seen = []
    for name in ("real", "ghost"):
        for _ in range(8):
            resp = await _login(client, name, "wrong-guess-xx", ip="203.0.113.5")
        seen.append((resp.status_code, resp.json(), resp.headers.get("retry-after")))
    assert seen[0] == seen[1]
    assert seen[0][0] == 429 and seen[0][2] is not None


async def test_blocked_attempt_skips_verify(client, monkeypatch):
    calls = []

    async def fake(username, password):
        calls.append(username)
        return None

    monkeypatch.setattr(shared_auth, "_authenticate_credentials", fake)
    for _ in range(5):
        assert (await _login(client, "u", "p")).status_code == 401
    resp = await _login(client, "u", "p")
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0
    assert len(calls) == 5


async def test_parallel_burst_admits_at_most_free(client, monkeypatch):
    calls = []

    async def fake(username, password):
        calls.append(1)
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(shared_auth, "_authenticate_credentials", fake)
    results = await asyncio.gather(*[_login(client, "u", "p") for _ in range(20)])
    assert len(calls) <= 5
    assert sum(r.status_code == 429 for r in results) >= 15


async def test_cookie_revoked_on_password_change(client):
    uid = await seed_user("zed", password=PW)
    ok = await _login(client, "zed", PW)
    device = ok.cookies.get("vf_device")
    client.cookies.clear()
    db = await get_db()
    try:
        await db.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (uid,))
        await db.commit()
    finally:
        await db.close()
    # pre-fill lane A from the same source with NO cookie
    for _ in range(5):
        await _login(client, "zed", "wrong-guess-xx")
    # revoked cookie == no cookie: A/B apply, so the same source is now blocked
    resp = await _login(client, "zed", PW, cookies={"vf_device": device})
    assert resp.status_code == 429


async def test_cookie_for_other_account_gives_no_bypass(client):
    await seed_user("xa", password=PW)
    await seed_user("ya", password=PW)
    device = (await _login(client, "xa", PW)).cookies.get("vf_device")
    client.cookies.clear()
    # pre-fill ya's lane A without a cookie; xa's cookie must not bypass it
    for _ in range(5):
        await _login(client, "ya", "wrong-guess-xx")
    assert (await _login(client, "ya", PW, cookies={"vf_device": device})).status_code == 429


async def test_success_sets_hardened_device_cookie(client):
    await seed_user("cookie-user", password=PW)
    resp = await _login(client, "cookie-user", PW)
    header = next(v for k, v in resp.headers.multi_items() if k == "set-cookie" and v.startswith("vf_device="))
    low = header.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/auth/login" in low
    assert "max-age=2592000" in low
