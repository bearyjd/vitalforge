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
    assert lt.client_ip(_Req(None)) == "unknown"


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
