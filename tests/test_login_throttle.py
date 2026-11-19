"""Brute-force throttle for POST /auth/login."""

import pytest

from shared import login_throttle as lt


@pytest.fixture(autouse=True)
def _reset():
    lt.reset()
    yield
    lt.reset()


def test_free_failures_then_exponential_backoff():
    for i in range(lt.FREE_FAILURES - 1):
        lt.record_failure("1.1.1.1", f"u{i}", 100.0)
    assert lt.retry_after("1.1.1.1", "other", 100.0) is None
    lt.record_failure("1.1.1.1", "u9", 100.0)
    first = lt.retry_after("1.1.1.1", "other", 100.0)
    assert first is not None
    lt.record_failure("1.1.1.1", "u9", 100.0 + first)
    second = lt.retry_after("1.1.1.1", "other", 100.0 + first)
    assert second > first


def test_backoff_is_capped_not_lockout():
    for _ in range(50):
        lt.record_failure("1.1.1.1", "alice", 0.0)
    assert lt.retry_after("1.1.1.1", "alice", 0.0) <= lt.MAX_DELAY_SECONDS + 1
    assert lt.retry_after("1.1.1.1", "alice", lt.MAX_DELAY_SECONDS + 1) is None


def test_per_username_applies_across_ips():
    for n in range(lt.FREE_FAILURES):
        lt.record_failure(f"9.9.9.{n}", "alice", 0.0)
    assert lt.retry_after("8.8.8.8", "Alice ", 0.0) is not None
    assert lt.retry_after("8.8.8.8", "bob", 0.0) is None


def test_success_clears_username_but_not_ip():
    for _ in range(lt.FREE_FAILURES):
        lt.record_failure("1.1.1.1", "alice", 0.0)
    lt.record_success("alice")
    assert lt.retry_after("2.2.2.2", "alice", 0.0) is None
    assert lt.retry_after("1.1.1.1", "bob", 0.0) is not None


def test_failures_forgotten_after_quiet_period():
    for _ in range(lt.FREE_FAILURES):
        lt.record_failure("1.1.1.1", "alice", 0.0)
    later = lt.RESET_AFTER_SECONDS + 1
    lt.record_failure("1.1.1.1", "alice", later)
    assert lt.retry_after("1.1.1.1", "alice", later) is None


def test_tables_are_bounded(monkeypatch):
    monkeypatch.setattr(lt, "MAX_TRACKED_KEYS", 5)
    for n in range(20):
        lt.record_failure(f"10.0.0.{n}", f"u{n}", float(n))
    assert len(lt._by_ip) <= 5 and len(lt._by_username) <= 5


class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_xff_ignored_from_untrusted_peer(monkeypatch):
    monkeypatch.delenv(lt.TRUSTED_PROXY_IPS_ENV, raising=False)
    assert lt.client_ip(_Req("5.5.5.5", "1.2.3.4")) == "5.5.5.5"


def test_xff_rightmost_used_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv(lt.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    assert lt.client_ip(_Req("127.0.0.1", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    assert lt.client_ip(_Req("127.0.0.1")) == "127.0.0.1"
    assert lt.client_ip(_Req(None)) is None


@pytest.fixture
async def client(initialized_db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from shared.auth_routes import add_auth_routes

    app = FastAPI()
    add_auth_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_login_route_returns_429_with_retry_after(client):
    body = {"username": "nobody", "password": "x" * 12}
    for _ in range(lt.FREE_FAILURES):
        assert (await client.post("/auth/login", json=body)).status_code == 401
    r = await client.post("/auth/login", json=body)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


async def test_login_success_clears_username_backoff(client):
    from tests.conftest import seed_user

    await seed_user("someone", "correct-password-1")
    good = {"username": "someone", "password": "correct-password-1"}
    bad = {"username": "someone", "password": "wrong-password-1"}
    for _ in range(lt.FREE_FAILURES - 1):
        await client.post("/auth/login", json=bad)
    r = await client.post("/auth/login", json=good)
    assert r.status_code == 200
    assert lt.retry_after("x", "someone", 0.0) is None
    assert "someone" not in lt._by_username


def test_reserve_counts_pending_toward_limit():
    held = [lt.reserve("1.1.1.1", "alice", 0.0) for _ in range(lt.FREE_FAILURES)]
    with pytest.raises(lt.ThrottledError):
        lt.reserve("1.1.1.1", "alice", 0.0)
    for r in held:
        lt.release(r)
    assert not lt._pending_ip and not lt._pending_username
    lt.reserve("1.1.1.1", "alice", 0.0)


def test_known_device_bypasses_username_bucket_only():
    lt.record_success("alice", "7.7.7.7", 0.0)
    for n in range(lt.FREE_FAILURES):
        lt.record_failure(f"9.9.9.{n}", "alice", 1.0)
    assert lt.retry_after("7.7.7.7", "alice", 1.0) is not None
    res = lt.reserve("7.7.7.7", "alice", 1.0)
    assert res.username_key is None
    with pytest.raises(lt.ThrottledError):
        lt.reserve("8.8.8.8", "alice", 1.0)  # no probe slot: unknown devices stay throttled
    for n in range(lt.FREE_FAILURES):
        lt.record_failure("7.7.7.7", "x", 1.0)
    with pytest.raises(lt.ThrottledError):
        lt.reserve("7.7.7.7", "alice", 1.0)




def test_concurrent_burst_is_throttled(monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from shared import auth, auth_routes

    calls = 0

    async def fake_auth(username, password):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return None

    monkeypatch.setattr(auth, "_authenticate_credentials", fake_auth)
    app = FastAPI()
    auth_routes.add_auth_routes(app)

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await asyncio.gather(
                *[c.post("/auth/login", json={"username": "alice", "password": "x"}) for _ in range(60)]
            )

    codes = [r.status_code for r in asyncio.run(run())]
    assert calls <= lt.FREE_FAILURES
    assert codes.count(429) >= 60 - lt.FREE_FAILURES


def test_known_device_still_hits_ip_bucket():
    lt.record_success("alice", "7.7.7.7", 0.0)
    for _ in range(lt.FREE_FAILURES):
        lt.record_failure("7.7.7.7", "x", 1.0)
    with pytest.raises(lt.ThrottledError):
        lt.reserve("7.7.7.7", "alice", 1.0)


def test_no_clean_ip_probe_rotating_ip_attacker_gets_capped_guesses():
    admitted = 0
    for i in range(0, 24 * 3600, 1):  # attacker guesses every second from a fresh IP
        t = float(i)
        ip = f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}"
        try:
            res = lt.reserve(ip, "alice", t)
        except lt.ThrottledError:
            continue
        admitted += 1
        lt.record_failure(res.ip, "alice", t)
        lt.release(res)
    # one guess per MAX_DELAY window once backed off, plus the free failures
    assert admitted <= lt.FREE_FAILURES + 24 * 3600 // lt.MAX_DELAY_SECONDS + 10


def test_legitimate_known_device_logs_in_while_rotating_attacker_hammers():
    lt.record_success("alice", "7.7.7.7", 0.0)
    for i in range(50):
        try:
            res = lt.reserve(f"203.0.113.{i}", "alice", float(i))
        except lt.ThrottledError:
            continue
        lt.record_failure(res.ip, "alice", float(i))
        lt.release(res)
    res = lt.reserve("7.7.7.7", "alice", 60.0)  # owner's known device is not blocked
    lt.release(res)


@pytest.mark.parametrize("trusted", [None, "172.20.0.2"])  # unset, or the old wrong example
def test_private_untrusted_peer_skips_ip_bucket(monkeypatch, trusted):
    if trusted is None:
        monkeypatch.delenv(lt.TRUSTED_PROXY_IPS_ENV, raising=False)
    else:
        monkeypatch.setenv(lt.TRUSTED_PROXY_IPS_ENV, trusted)
    assert lt.client_ip(_Req("172.20.0.1", "6.6.6.6")) is None
    assert lt.client_ip(_Req("127.0.0.1", "6.6.6.6")) is None


def test_misconfigured_proxy_attacker_cannot_block_other_users_correct_login(monkeypatch):
    monkeypatch.delenv(lt.TRUSTED_PROXY_IPS_ENV, raising=False)
    now = 0.0
    for _ in range(100):  # one anonymous client behind the proxy hammers "mallory"
        ip = lt.client_ip(_Req("172.20.0.1", "6.6.6.6"))
        try:
            res = lt.reserve(ip, "mallory", now)
        except lt.ThrottledError:
            continue
        lt.record_failure(res.ip, "mallory", now)
        lt.release(res)
    ip = lt.client_ip(_Req("172.20.0.1", "9.9.9.9"))
    lt.release(lt.reserve(ip, "bob", now))  # a different user is not blocked


def test_invalid_xff_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv(lt.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    assert lt.client_ip(_Req("127.0.0.1", "not-an-ip")) == "127.0.0.1"
    assert lt.client_ip(_Req("127.0.0.1", "x" * 5000)) == "127.0.0.1"


def test_ipv6_normalised_to_slash_64(monkeypatch):
    monkeypatch.delenv(lt.TRUSTED_PROXY_IPS_ENV, raising=False)
    a = lt.client_ip(_Req("2606:4700:1:2:aaaa::1"))
    b = lt.client_ip(_Req("2606:4700:1:2:bbbb::9"))
    c = lt.client_ip(_Req("2606:4700:1:3::1"))
    assert a == b == "2606:4700:1:2::/64"
    assert c != a


def test_ipv6_rotation_within_64_is_one_ip_bucket(monkeypatch):
    monkeypatch.delenv(lt.TRUSTED_PROXY_IPS_ENV, raising=False)
    for n in range(lt.FREE_FAILURES):
        ip = lt.client_ip(_Req(f"2606:4700:1:2::{n + 1}"))
        lt.record_failure(ip, f"u{n}", 0.0)
    with pytest.raises(lt.ThrottledError):
        lt.reserve(lt.client_ip(_Req("2606:4700:1:2::ffff")), "fresh", 0.0)
