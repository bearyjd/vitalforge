"""VF-2 login backoff (lanes A/B/D/G + device cookie), see shared/login_backoff.py.

Unit tests drive `reserve`/`record_success` with a fake clock; route tests go
through POST /auth/login on a throwaway app, like test_user_management.py.
"""

import asyncio
import ipaddress

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


def test_lane_d_keyed_per_jti_not_per_account(clock):
    ident = lb.normalize_id("ivy")
    first, second = _dev(ident, jti="jA"), _dev(ident, jti="jB")
    with pytest.raises(lb.LoginBlocked):
        for _ in range(12):
            _fail(ident, "6.6.6.6", first)
            clock[0] += 0.3
    lb.reserve(ident, "6.6.6.6", second)  # other device unaffected


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


def test_x_real_ip_ignored_from_untrusted_peers_honoured_from_loopback():
    # direct-port access: the header is attacker-controlled, the peer is not
    assert lb.client_src("198.51.100.9", "1.2.3.4") == "198.51.100.9"
    assert lb.client_src("::1", "1.2.3.4") == "1.2.3.4"
    assert lb.client_src("127.0.0.1", "1.2.3.4") == "1.2.3.4"
    assert lb.client_src("127.0.0.1", None) == "127.0.0.1"


GATEWAY = "172.18.0.1"


@pytest.fixture
def gateway_trusted(monkeypatch):
    monkeypatch.setattr(lb, "_TRUSTED", lb._parse_trusted(f"127.0.0.0/8,::1/128,{GATEWAY}/32"))


def test_nginx_on_host_via_docker_gateway_yields_distinct_src_keys(gateway_trusted):
    # nginx on the host -> 127.0.0.1:8085 -> docker port publishing: peer is the bridge gateway
    a = lb.client_src(GATEWAY, "203.0.113.9")
    b = lb.client_src(GATEWAY, "203.0.113.10")
    assert a == "203.0.113.9" and b == "203.0.113.10" and a != b


def test_gateway_hop_without_valid_x_real_ip_keys_on_peer(gateway_trusted):
    # deterministic peer key (no random per-request key); lanes B/G bound the damage
    keys = {lb.client_src(GATEWAY, h) for h in (None, "", "not-an-ip")}
    assert keys == {GATEWAY}


@pytest.mark.parametrize("peer", ["192.168.1.50", "10.1.2.3", "172.19.0.7", "fd00::1"])
def test_lan_peer_never_trusted_by_default(peer, gateway_trusted):
    assert not lb._is_trusted(peer)
    assert lb.client_src(peer, "203.0.113.9") == lb.client_src(peer, None) == lb.client_src(peer, "garbage")


def test_lan_host_rotating_x_real_ip_adds_at_most_p_to_lane_b(clock, gateway_trusted):
    ident = lb.normalize_id("victim")
    for i in range(40):
        src = lb.client_src("192.168.1.50", f"203.0.113.{i}")
        try:
            _fail(ident, src)
        except lb.LoginBlocked as exc:
            clock[0] += exc.retry_after
    assert lb._ab[lb._slot_index("B", ident)][0] <= lb.P


def test_lan_host_without_header_adds_at_most_p_to_lane_b(clock, gateway_trusted):
    ident = lb.normalize_id("victim")
    for _ in range(40):
        src = lb.client_src("192.168.1.50", None)
        try:
            _fail(ident, src)
        except lb.LoginBlocked as exc:
            clock[0] += exc.retry_after
    assert lb._ab[lb._slot_index("B", ident)][0] <= lb.P


def test_lan_host_cannot_lock_another_sources_lane_a(clock, gateway_trusted):
    ident = lb.normalize_id("victim")
    victim_src = lb.client_src("127.0.0.1", "198.51.100.7")  # via nginx
    for _ in range(40):
        try:
            _fail(ident, lb.client_src("192.168.1.50", "198.51.100.7"))  # spoofed
        except lb.LoginBlocked as exc:
            clock[0] += exc.retry_after
    assert lb._ab[lb._slot_index("A", ident, victim_src)][0] == 0


def test_trusted_proxies_configurable(monkeypatch):
    monkeypatch.setattr(lb, "_TRUSTED", lb._parse_trusted("172.18.0.1/32"))
    assert lb.client_src("172.18.0.1", "203.0.113.9") == "203.0.113.9"
    assert lb.client_src("172.18.0.2", "203.0.113.9") == "172.18.0.2"


def test_invalid_trusted_proxies_error_names_the_variable():
    with pytest.raises(ValueError, match="VITALFORGE_TRUSTED_PROXIES.*not-a-cidr"):
        lb._build_trusted("127.0.0.1/32,not-a-cidr", route_text="")


ROUTE = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    "eth1\t0010A8C0\t0A10A8C0\t0003\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    "eth0\t00000000\t0112A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t0012A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


def test_default_gateway_parsed_from_proc_net_route():
    assert lb._default_gateway(ROUTE) == "192.168.18.1"
    assert lb._default_gateway("") is None
    assert lb._default_gateway("garbage\nno route here") is None


def test_default_trust_is_loopback_plus_detected_gateway_or_loopback_only():
    with_gw = lb._build_trusted(None, route_text=ROUTE)
    assert any(ipaddress.ip_address("192.168.18.1") in n for n in with_gw if n.version == 4)
    assert not any(ipaddress.ip_address("192.168.18.2") in n for n in with_gw if n.version == 4)
    assert any(ipaddress.ip_address("127.0.0.1") in n for n in with_gw if n.version == 4)
    assert not any(ipaddress.ip_address("192.168.1.50") in n for n in with_gw if n.version == 4)
    lo_only = lb._build_trusted(None, route_text="")
    assert not any(ipaddress.ip_address("192.168.18.1") in n for n in lo_only if n.version == 4)


def test_trusted_proxies_env_replaces_loopback_not_merges():
    trusted = lb._build_trusted("172.18.0.1/32", route_text=ROUTE)
    assert not any(ipaddress.ip_address("127.0.0.1") in n for n in trusted if n.version == 4)
    assert not any(ipaddress.ip_address("::1") in n for n in trusted if n.version == 6)
    assert not any(ipaddress.ip_address("192.168.18.1") in n for n in trusted if n.version == 4)


def test_missing_route_file_falls_back_to_loopback_only(monkeypatch, tmp_path):
    monkeypatch.setattr(lb, "_ROUTE_FILE", str(tmp_path / "no-such-route"))
    trusted = lb._build_trusted(None)
    assert {str(n) for n in trusted} == {"127.0.0.0/8", "::1/128"}


@pytest.mark.parametrize("spec", [" , ", ",", "  "])
def test_empty_trusted_proxies_after_parsing_is_an_error(spec):
    with pytest.raises(ValueError, match="VITALFORGE_TRUSTED_PROXIES"):
        lb._build_trusted(spec, route_text="")


def test_compose_publishes_ports_ipv4_only():
    """Gateway trust is only sound if docker-proxy never fronts IPv6 clients."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        text = (root / name).read_text()
        mappings = re.findall(r'^\s*-\s*"?([0-9.:]*:?(?:8085|8086):(?:8085|8086))"?\s*$', text, re.M)
        assert mappings, name
        for m in mappings:
            assert m.startswith(("0.0.0.0:", "127.0.0.1:")), f"{name}: {m} is not IPv4-bound"


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
