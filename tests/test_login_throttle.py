"""Brute-force throttling on POST /auth/login: per-account and per-IP, with
exponential backoff once a limit is reached."""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shared import login_throttle
from shared.auth_routes import add_auth_routes
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _reset_throttle():
    login_throttle.reset()
    yield
    login_throttle.reset()


@pytest.fixture
async def client(initialized_db):
    app = FastAPI()
    add_auth_routes(app)
    transport = ASGITransport(app=app, client=("203.0.113.9", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client, username, password):
    return await client.post("/auth/login", json={"username": username, "password": password})


async def test_account_locked_after_repeated_failures_even_with_right_password(client):
    await seed_user("admin", password="correct-pass")
    for _ in range(login_throttle.ACCOUNT_FAILURE_LIMIT):
        assert (await _login(client, "admin", "wrong")).status_code == 401
    resp = await _login(client, "admin", "correct-pass")
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1


async def test_username_match_is_case_insensitive(client):
    await seed_user("admin", password="correct-pass")
    for _ in range(login_throttle.ACCOUNT_FAILURE_LIMIT):
        await _login(client, "ADMIN", "wrong")
    assert (await _login(client, "admin", "correct-pass")).status_code == 429


async def test_ip_throttled_across_many_usernames(client):
    for i in range(login_throttle.IP_FAILURE_LIMIT):
        assert (await _login(client, f"user{i}", "wrong")).status_code == 401
    assert (await _login(client, "fresh-user", "wrong")).status_code == 429


async def test_success_clears_account_failures(client):
    await seed_user("admin", password="correct-pass")
    for _ in range(login_throttle.ACCOUNT_FAILURE_LIMIT - 1):
        await _login(client, "admin", "wrong")
    assert (await _login(client, "admin", "correct-pass")).status_code == 200
    for _ in range(login_throttle.ACCOUNT_FAILURE_LIMIT - 1):
        assert (await _login(client, "admin", "wrong")).status_code == 401


async def test_backoff_grows_with_continued_failures(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(login_throttle, "_now", lambda: now[0])
    limit = login_throttle.ACCOUNT_FAILURE_LIMIT
    for _ in range(limit):
        login_throttle.record_failure("1.1.1.1", "bob")
    first = login_throttle.retry_after("1.1.1.1", "bob")
    now[0] += first
    assert login_throttle.retry_after("1.1.1.1", "bob") == 0
    login_throttle.record_failure("1.1.1.1", "bob")
    assert login_throttle.retry_after("1.1.1.1", "bob") > first


async def test_failures_expire_after_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(login_throttle, "_now", lambda: now[0])
    for _ in range(login_throttle.ACCOUNT_FAILURE_LIMIT):
        login_throttle.record_failure("1.1.1.1", "bob")
    now[0] += login_throttle.WINDOW_SECONDS + 1
    assert login_throttle.retry_after("1.1.1.1", "bob") == 0


async def test_concurrent_burst_cannot_bypass_account_limit(client):
    await seed_user("admin", password="correct-pass")
    responses = await asyncio.gather(*(_login(client, "admin", "wrong") for _ in range(50)))
    codes = [r.status_code for r in responses]
    assert codes.count(401) <= login_throttle.ACCOUNT_FAILURE_LIMIT
    assert codes.count(429) >= 50 - login_throttle.ACCOUNT_FAILURE_LIMIT
    assert login_throttle._pending == {}


async def test_reservation_released_when_verification_raises(client, monkeypatch):
    async def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr("shared.auth._authenticate_credentials", boom)
    with pytest.raises(RuntimeError):
        await _login(client, "admin", "x")
    assert login_throttle._pending == {}


async def test_proxy_peer_does_not_share_one_ip_bucket(initialized_db):
    await seed_user("admin", password="correct-pass")
    app = FastAPI()
    add_auth_routes(app)
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for i in range(login_throttle.IP_FAILURE_LIMIT + 5):
            resp = await ac.post("/auth/login", json={"username": f"u{i}", "password": "x"})
            assert resp.status_code == 401
        assert (await _login(ac, "admin", "correct-pass")).status_code == 200


async def test_trusted_proxy_uses_x_real_ip_bucket(initialized_db):
    app = FastAPI()
    add_auth_routes(app)
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    headers = {"X-Real-IP": "198.51.100.7"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for i in range(login_throttle.IP_FAILURE_LIMIT):
            r = await ac.post("/auth/login", json={"username": f"u{i}", "password": "x"}, headers=headers)
            assert r.status_code == 401
        r = await ac.post("/auth/login", json={"username": "new", "password": "x"}, headers=headers)
        assert r.status_code == 429
        other = {"X-Real-IP": "198.51.100.8"}
        r = await ac.post("/auth/login", json={"username": "new", "password": "x"}, headers=other)
        assert r.status_code == 401
