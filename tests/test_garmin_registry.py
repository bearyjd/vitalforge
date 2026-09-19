"""Focused contract tests for the Garmin registry's security boundary."""

import asyncio
import logging
import threading

import pytest
from fastapi import FastAPI
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.exceptions import GarminConnectNotFoundError
from httpx import ASGITransport, AsyncClient

from shared import garmin_client, garmin_registry, garmin_registry_locks, garmin_registry_runtime
from shared.auth import create_session_cookie
from shared.database import get_db, get_primary_person_id
from shared.persons_admin import add_person_routes
from tests.conftest import seed_person, seed_user

_LEGACY_ADOPTION_MARKER = garmin_registry_runtime._LEGACY_ADOPTION_MARKER


class _FakeClient:
    def __init__(self, generation: int):
        self.generation = generation


async def _link(person_id: int, generation: int = 1, state: str = "linked") -> None:
    db = await get_db()
    try:
        email = f"person-{person_id}@example.test" if state != "legacy_disabled" else None
        linked_at = "2026-09-15T00:00:00Z" if state != "legacy_disabled" else None
        await db.execute(
            """
            INSERT INTO garmin_links
                (person_id, state, garmin_email, generation, linked_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '2026-09-15T00:00:00Z')
            """,
            (person_id, state, email, generation, linked_at),
        )
        await db.commit()
    finally:
        await db.close()


async def _grant_manage(person_id: int, user_id: int) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO person_grants (person_id, user_id, access, granted_at) VALUES (?, ?, 'manage', ?)",
            (person_id, user_id, "2026-09-15T00:00:00Z"),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.fixture(autouse=True)
def _clear_clients(monkeypatch, tmp_path):
    garmin_client._clients.clear()
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    yield
    garmin_client._clients.clear()


def _write_fake_token_store(token_dir) -> None:
    """Leave the inert, non-empty artifact a real login persists."""
    token_dir.mkdir(mode=0o700, exist_ok=True)
    (token_dir / "garmin_tokens.json").write_text("{}", encoding="ascii")


async def _adoption_marker_recorded() -> bool:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM auth_migrations WHERE name = ?", (_LEGACY_ADOPTION_MARKER,))
        ).fetchone()
    finally:
        await db.close()
    return row is not None


async def _link_row(person_id: int):
    db = await get_db()
    try:
        return await (
            await db.execute(
                "SELECT state, garmin_email, generation FROM garmin_links WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
    finally:
        await db.close()


def _fake_auth(calls: list[tuple[int, int]], token_dirs: list | None = None):
    def authenticate(person_id, generation, token_dir, email, password):
        assert password is None
        assert email == f"person-{person_id}@example.test"
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        calls.append((person_id, generation))
        if token_dirs is not None:
            token_dirs.append(token_dir)
        return client

    return authenticate


def _lifecycle_auth(calls: list[tuple[int, int]]):
    """Fake login that leaves an inert token artifact in the staging dir."""
    def authenticate(person_id, generation, token_dir, email, password):
        assert password == "transient-password"
        _write_fake_token_store(token_dir)
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        calls.append((person_id, generation))
        return client

    return authenticate


def _recording_auth(calls: list[tuple[int, int, str | None]]):
    """Fake login that only records who authenticated with what.

    Unlike :func:`_fake_auth` it asserts nothing about the email, so a test
    that must prove a login never happened sees the call in ``calls`` rather
    than an AssertionError swallowed inside the registry's own except.
    """
    def authenticate(person_id, generation, token_dir, email, password):
        calls.append((person_id, generation, password))
        if password is not None:
            _write_fake_token_store(token_dir)
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        return client

    return authenticate


async def test_call_requires_a_durable_link_before_using_any_client(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    used = False

    def op(_client):
        nonlocal used
        used = True

    with pytest.raises(garmin_registry.GarminNotLinked, match="not linked"):
        await garmin_registry.call(person_id, op)

    assert not used

    db = await get_db()
    try:
        row = await (await db.execute("SELECT next_allowed_at FROM garmin_call_budget")).fetchone()
    finally:
        await db.close()
    assert row["next_allowed_at"] == 0


async def test_call_uses_durable_global_permit_and_returns_bounded_retry_after(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    # Cache hits make one outbound Garmin operation, and therefore use one
    # permit.  Cold starts are covered separately because login is itself a
    # Garmin call and needs a second permit before the operation.
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")
    assert await garmin_registry.call(person_id, lambda client: client.generation) == 1
    with pytest.raises(garmin_registry.GarminRateLimited) as exc_info:
        await garmin_registry.call(person_id, lambda _client: pytest.fail("must not call Garmin"))

    assert exc_info.value.retry_after == 2
    db = await get_db()
    try:
        row = await (await db.execute("SELECT next_allowed_at FROM garmin_call_budget")).fetchone()
    finally:
        await db.close()
    assert row["next_allowed_at"] == 102.0


async def test_call_permit_allows_only_one_of_two_concurrent_requests(initialized_db, monkeypatch):
    """BEGIN IMMEDIATE makes the deployment-wide bucket atomic, not advisory."""
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")

    async def reserve() -> str:
        try:
            await garmin_registry.reserve_call_permit()
        except garmin_registry.GarminRateLimited:
            return "limited"
        return "accepted"

    assert sorted(await asyncio.gather(reserve(), reserve())) == ["accepted", "limited"]


async def _set_next_allowed_at(value: float) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE garmin_call_budget SET next_allowed_at = ?", (value,))
        await db.commit()
    finally:
        await db.close()


async def _next_allowed_at() -> float:
    db = await get_db()
    try:
        row = await (await db.execute("SELECT next_allowed_at FROM garmin_call_budget")).fetchone()
    finally:
        await db.close()
    return row["next_allowed_at"]


async def test_call_permit_retry_after_stays_within_configured_bounds(initialized_db, monkeypatch):
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "60")
    await _set_next_allowed_at(160.0)

    with pytest.raises(garmin_registry.GarminRateLimited) as exc_info:
        await garmin_registry.reserve_call_permit()
    assert exc_info.value.retry_after == 60


async def test_call_permit_treats_a_slot_beyond_one_interval_as_clock_skew(initialized_db, monkeypatch):
    """A reservation only ever writes now + interval; anything further ahead
    is a clock regression, and honouring it would freeze Garmin calls until
    the wall clock caught up."""
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")
    await _set_next_allowed_at(10000.0)

    await garmin_registry.reserve_call_permit()

    assert await _next_allowed_at() == 102.0
    with pytest.raises(garmin_registry.GarminRateLimited):
        await garmin_registry.reserve_call_permit()


@pytest.mark.parametrize(
    "max_wait_seconds, expected_sleeps, outcome",
    (
        (5.0, [2], "succeeds"),
        (1.0, [], "rate_limited"),
        (0.0, [], "rate_limited"),
    ),
)
async def test_call_waits_for_a_permit_only_within_its_budget(
    initialized_db, monkeypatch, max_wait_seconds, expected_sleeps, outcome
):
    """An interactive push may absorb one interval; a zero budget fails fast."""
    person_id = await get_primary_person_id()
    await _link(person_id)
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")
    await _set_next_allowed_at(102.0)

    async def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(garmin_registry.asyncio, "sleep", advance)

    if outcome == "succeeds":
        result = await garmin_registry.call(
            person_id, lambda client: client.generation, max_wait_seconds=max_wait_seconds
        )
        assert result == 1
    else:
        with pytest.raises(garmin_registry.GarminRateLimited) as exc_info:
            await garmin_registry.call(
                person_id, lambda _client: pytest.fail("must not call Garmin"), max_wait_seconds=max_wait_seconds
            )
        assert exc_info.value.retry_after == 2
    assert sleeps == expected_sleeps


async def test_call_rejects_a_negative_wait_budget(initialized_db):
    with pytest.raises(ValueError):
        await garmin_registry.call(1, lambda _client: None, max_wait_seconds=-1)


async def test_call_evicts_a_stale_generation_before_running_the_operation(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id, generation=1)
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)
    db = await get_db()
    try:
        await db.execute("UPDATE garmin_links SET generation = 2 WHERE person_id = ?", (person_id,))
        await db.commit()
    finally:
        await db.close()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    # A cold client uses one permit to resume its token store and a second
    # permit for the requested Garmin operation.
    moments = iter((100.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))

    assert await garmin_registry.call(person_id, lambda client: client.generation) == 2
    assert calls == [(person_id, 2)]
    assert (person_id, 1) not in garmin_client._clients
    assert (person_id, 2) in garmin_client._clients


async def test_clients_with_identical_generations_never_cross_person_boundaries(initialized_db, monkeypatch):
    first_person = await get_primary_person_id()
    second_person = await seed_person("second-person")
    await _link(first_person, generation=1)
    await _link(second_person, generation=1)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    moments = iter((100.0, 101.0, 102.0, 103.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "1")

    first = await garmin_registry.call(first_person, lambda client: client)
    second = await garmin_registry.call(second_person, lambda client: client)

    assert first is not second
    assert calls == [(first_person, 1), (second_person, 1)]
    assert set(garmin_client._clients) == {(first_person, 1), (second_person, 1)}


async def test_waiting_for_a_person_lock_does_not_spend_its_permit_early(initialized_db, monkeypatch):
    first_person = await get_primary_person_id()
    second_person = await seed_person("unblocked-person")
    await _link(first_person)
    await _link(second_person)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    # The unblocked cold call spends permits at 100 (login) and 102
    # (operation). Once the queued task acquires the flock, it sees the same
    # 102 instant and is rate-limited rather than having spent a permit while
    # it was waiting for the lock.
    moments = iter((100.0, 102.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))

    async with garmin_registry.person_flock(first_person):
        queued = asyncio.create_task(garmin_registry.call(first_person, lambda client: client))
        await asyncio.sleep(0.01)
        assert calls == []
        assert await garmin_registry.call(second_person, lambda client: client) is not None

    with pytest.raises(garmin_registry.GarminRateLimited):
        await queued
    assert calls == [(second_person, 1)]


async def test_auth_failure_is_persisted_as_a_bounded_code_without_raw_exception(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    sentinel = "password-email-token-and-path-must-not-leak"

    def failed_auth(*_args):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", failed_auth)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(garmin_registry.GarminAuthenticationError) as exc_info:
        await garmin_registry.call(person_id, lambda _client: None)
    assert sentinel not in str(exc_info.value)
    assert exc_info.value.code == "unknown"

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT last_auth_error, last_auth_error_at FROM garmin_links WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
    finally:
        await db.close()
    assert row["last_auth_error"] == "unknown"
    assert row["last_auth_error_at"] is not None


async def test_cold_call_reserves_separate_intervals_for_login_and_operation(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    auth_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(auth_calls))
    # The first permit admits login at 100; the second admits the operation
    # only after the two-second interval has elapsed.
    moments = iter((100.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")

    assert await garmin_registry.call(person_id, lambda client: client.generation) == 1
    assert auth_calls == [(person_id, 1)]
    db = await get_db()
    try:
        row = await (await db.execute("SELECT next_allowed_at FROM garmin_call_budget")).fetchone()
    finally:
        await db.close()
    assert row["next_allowed_at"] == 104.0


async def test_cold_call_waits_for_its_second_permit(initialized_db, monkeypatch):
    """A cold logical call must not reject its own post-login operation."""
    person_id = await get_primary_person_id()
    await _link(person_id)
    auth_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(auth_calls))
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")

    async def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(garmin_registry.asyncio, "sleep", advance)
    assert await garmin_registry.call(person_id, lambda client: client.generation) == 1
    assert auth_calls == [(person_id, 1)]
    assert sleeps == [2]


async def test_paced_call_waits_between_dashboard_batch_operations(initialized_db, monkeypatch):
    """The paced sync path still consumes one global permit per Garmin call."""
    person_id = await get_primary_person_id()
    await _link(person_id)
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)
    clock = {"now": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")

    async def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(garmin_registry.asyncio, "sleep", advance)
    assert await garmin_registry.call_paced(person_id, lambda client: client.generation) == 1
    assert await garmin_registry.call_paced(person_id, lambda client: client.generation) == 1
    assert sleeps == [2]


def _flat_store(tmp_path):
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    (root / "garmin_tokens.json").write_text("{}", encoding="ascii")
    return root


async def test_bootstrap_adopts_a_verified_flat_store_once_by_moving_it(
    initialized_db, monkeypatch, tmp_path
):
    """Adoption moves the flat store under the primary person, publishes an
    ordinary 'linked' row and records its one-time marker; a second boot is
    a no-op that never contacts Garmin again."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f" Person-{person_id}@Example.Test ")

    assert await garmin_registry.bootstrap_legacy_token_store() is True
    assert await garmin_registry.bootstrap_legacy_token_store() is False
    assert calls == [(person_id, 1)]

    durable = garmin_registry._generation_token_dir(person_id, 1)
    assert not (root / "garmin_tokens.json").exists(), "the flat store must be moved, not copied"
    assert (durable / "garmin_tokens.json").read_text(encoding="ascii") == "{}"
    assert durable.stat().st_mode & 0o777 == 0o700
    # The verification client's persistence path is the flat root; it must not
    # stay cached and dump a refreshed token there later.
    assert (person_id, 1) not in garmin_client._clients
    assert tuple(await _link_row(person_id)) == ("linked", f"person-{person_id}@example.test", 1)
    assert await _adoption_marker_recorded()
    db = await get_db()
    try:
        ledger = await (
            await db.execute("SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert ledger["generation"] == 1


async def test_first_call_after_adoption_resumes_from_the_moved_store(initialized_db, monkeypatch, tmp_path):
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    token_dirs: list = []
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls, token_dirs))
    moments = iter((100.0, 110.0, 112.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is True
    assert await garmin_registry.call(person_id, lambda client: client.generation) == 1

    assert calls == [(person_id, 1), (person_id, 1)]
    assert token_dirs == [root, garmin_registry._generation_token_dir(person_id, 1)]


async def test_bootstrap_never_re_adopts_after_unlink_even_if_the_flat_store_returns(
    initialized_db, monkeypatch, tmp_path
):
    """Unlink keeps the person's generation-ledger row, and that row alone
    refuses re-adoption for the same person (``_adoptable_primary_person``).
    The marker is what covers the case this does not: a *different* primary
    with no rows at all -- see the archive-and-promote test below."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    actor_id = await seed_user("post-adoption-unlink", role="admin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is True
    assert await garmin_registry.unlink(person_id, actor_id, 1) is True
    assert not garmin_registry._person_token_root(person_id).exists()
    # A restored backup (or a stray copy) reappears at the flat root.
    (root / "garmin_tokens.json").write_text("{}", encoding="ascii")

    assert await garmin_registry.bootstrap_legacy_token_store() is False
    assert calls == [(person_id, 1)], "the flat store must never be verified again"
    assert await _link_row(person_id) is None
    assert (root / "garmin_tokens.json").is_file(), "an un-adoptable store is left where it was"


async def test_bootstrap_completes_an_interrupted_adoption_without_moving_again(
    initialized_db, monkeypatch, tmp_path
):
    """SIGKILL between the file move and the commit leaves the store under
    generation-1 with no row and no marker; the next boot only publishes."""
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    person_id = await get_primary_person_id()
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    durable = garmin_registry._generation_token_dir(person_id, 1)
    durable.mkdir(parents=True, mode=0o700)
    (durable / "garmin_tokens.json").write_text("{}", encoding="ascii")
    calls: list[tuple[int, int]] = []
    token_dirs: list = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls, token_dirs))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is True

    assert calls == [(person_id, 1)]
    assert token_dirs == [durable]
    assert (durable / "garmin_tokens.json").is_file()
    assert not (root / "garmin_tokens.json").exists()
    assert tuple(await _link_row(person_id)) == ("linked", f"person-{person_id}@example.test", 1)
    assert await _adoption_marker_recorded()
    assert (person_id, 1) not in garmin_client._clients


async def test_bootstrap_restores_the_flat_store_when_publication_fails(
    initialized_db, monkeypatch, tmp_path, caplog
):
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    sentinel = "publication-failure-path-must-not-escape"

    async def failed_publish(*_args):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry_runtime, "_publish_legacy_adoption", failed_publish)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")
    caplog.set_level(logging.WARNING, logger="shared.garmin_registry_runtime")

    assert await garmin_registry.bootstrap_legacy_token_store() is False

    assert calls == [(person_id, 1)]
    assert (root / "garmin_tokens.json").read_text(encoding="ascii") == "{}"
    assert not (garmin_registry._generation_token_dir(person_id, 1) / "garmin_tokens.json").exists()
    assert await _link_row(person_id) is None
    assert not await _adoption_marker_recorded()
    assert sentinel not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("prior", ("link_row", "ledger_row", "conflicting_email"))
async def test_bootstrap_skips_a_primary_the_new_lifecycle_already_touched(
    initialized_db, monkeypatch, tmp_path, prior
):
    """Once any per-person lifecycle state exists, the flat store is residue."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        if prior == "link_row":
            await db.execute(
                "INSERT INTO garmin_links (person_id, state, garmin_email, generation, linked_at, updated_at) "
                "VALUES (?, 'linked', 'other@example.test', 2, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')",
                (person_id,),
            )
        elif prior == "ledger_row":
            await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (person_id,))
        else:
            other = await seed_person("owner-of-that-email")
            await db.execute(
                "INSERT INTO garmin_links (person_id, state, garmin_email, generation, linked_at, updated_at) "
                "VALUES (?, 'linked', ?, 1, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')",
                (other, f"person-{person_id}@example.test"),
            )
        await db.commit()
    finally:
        await db.close()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is False

    assert calls == []
    assert (root / "garmin_tokens.json").is_file()
    assert not await _adoption_marker_recorded()


async def test_concurrent_startups_adopt_a_verified_flat_store_exactly_once(
    initialized_db, monkeypatch, tmp_path
):
    """The GARTH parent flock makes two service lifespans one adoption."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    auth_started = threading.Event()
    permit_publish = threading.Event()

    def gated_authenticate(person, generation, token_dir, email, password):
        assert (person, generation, token_dir, email, password) == (
            person_id,
            1,
            root,
            f"person-{person_id}@example.test",
            None,
        )
        calls.append((person, generation))
        auth_started.set()
        assert permit_publish.wait(timeout=5), "test did not release the first startup"
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", gated_authenticate)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    first = asyncio.create_task(garmin_registry.bootstrap_legacy_token_store())
    assert await asyncio.wait_for(asyncio.to_thread(auth_started.wait, 5), timeout=5)
    second = asyncio.create_task(garmin_registry.bootstrap_legacy_token_store())
    await asyncio.sleep(0)
    assert calls == [(person_id, 1)], "the second startup entered provider authentication"

    permit_publish.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert sorted(outcomes) == [False, True]
    assert calls == [(person_id, 1)]

    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT state, garmin_email, generation FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchall()
    finally:
        await db.close()
    assert [tuple(row) for row in rows] == [("linked", f"person-{person_id}@example.test", 1)]
    assert not (root / "garmin_tokens.json").exists()
    assert (garmin_registry._generation_token_dir(person_id, 1) / "garmin_tokens.json").is_file()


async def test_bootstrap_does_not_bind_a_flat_store_that_fails_verification(
    initialized_db, monkeypatch, tmp_path, caplog
):
    """A provider failure leaves no durable link, moves nothing, and never
    logs token detail."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    sensitive_detail = "legacy-token-content-or-path-must-not-escape"

    def failed_auth(*_args):
        raise RuntimeError(sensitive_detail)

    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", failed_auth)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is False
    assert sensitive_detail not in caplog.text
    assert (person_id, 1) not in garmin_client._clients
    assert (root / "garmin_tokens.json").is_file()
    assert not garmin_registry._person_token_root(person_id).exists()
    assert await _link_row(person_id) is None
    assert not await _adoption_marker_recorded()


async def test_adoption_holds_the_person_flock_so_a_concurrent_link_waits(
    initialized_db, monkeypatch, tmp_path
):
    """Adoption verifies the flat store and then moves it under generation-1.
    A route link for the same person that starts inside that window must
    queue behind the person flock.  Without it, the move landed on top of
    the freshly linked generation-1 store, the refused publication dragged
    that file back to the flat root, and the published link was left with
    an empty directory."""
    root = _flat_store(tmp_path)
    (root / "garmin_tokens.json").write_text('{"legacy": true}', encoding="ascii")
    person_id = await get_primary_person_id()
    actor_id = await seed_user("adoption-race-actor", role="admin")
    verify_started = threading.Event()
    release_verify = threading.Event()
    link_login_started = threading.Event()
    release_link_login = threading.Event()
    calls: list[tuple[int, str | None]] = []

    def gated_authenticate(person, generation, token_dir, email, password):
        calls.append((generation, password))
        if password is None:
            verify_started.set()
            assert release_verify.wait(timeout=5), "test did not release the adoption"
        else:
            token_dir.mkdir(mode=0o700, exist_ok=True)
            (token_dir / "garmin_tokens.json").write_text('{"account": "other"}', encoding="ascii")
            link_login_started.set()
            assert release_link_login.wait(timeout=5), "test did not release the link"
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    clock = {"now": 100.0}
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", gated_authenticate)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_EMAIL", "legacy@example.test")

    adoption = asyncio.create_task(garmin_registry.bootstrap_legacy_token_store())
    assert await asyncio.to_thread(verify_started.wait, 5)
    clock["now"] = 200.0
    link = asyncio.create_task(
        garmin_registry.link(person_id, actor_id, 1, "other@example.test", "transient-password")
    )
    await asyncio.sleep(0.05)
    assert not link.done()
    assert calls == [(1, None)], "the link reached Garmin while adoption still held the person"

    release_verify.set()
    assert await asyncio.wait_for(adoption, timeout=5) is True
    assert await asyncio.to_thread(link_login_started.wait, 5)
    # The link is now in its own login, in a staging directory: adoption's
    # move landed in generation-1 untouched, and nothing went back to the root.
    durable_1 = garmin_registry._generation_token_dir(person_id, 1)
    assert (durable_1 / "garmin_tokens.json").read_text(encoding="ascii") == '{"legacy": true}'
    assert not (root / "garmin_tokens.json").exists()
    assert tuple(await _link_row(person_id)) == ("linked", "legacy@example.test", 1)

    release_link_login.set()
    published = await asyncio.wait_for(link, timeout=5)
    assert published == garmin_registry.GarminLink(person_id, 2, "linked")
    assert calls == [(1, None), (2, "transient-password")]
    durable_2 = garmin_registry._generation_token_dir(person_id, 2)
    assert (durable_2 / "garmin_tokens.json").read_text(encoding="ascii") == '{"account": "other"}'
    assert not durable_1.exists(), "the adopted generation is superseded like any other re-link"
    assert tuple(await _link_row(person_id)) == ("linked", "other@example.test", 2)
    assert not (root / "garmin_tokens.json").exists()
    assert not list(root.glob("*.staging"))


async def test_adoption_re_checks_under_the_person_flock_after_a_link_wins(
    initialized_db, monkeypatch, tmp_path
):
    """A route link that already holds the person flock but has not yet
    reserved its generation passes adoption's first check.  Adoption must
    then queue behind the flock and re-check: the ledger row the link wrote
    meanwhile makes the flat store residue, so nothing is moved and the flat
    store is never verified against the link's fresh generation-1."""
    root = _flat_store(tmp_path)
    (root / "garmin_tokens.json").write_text('{"legacy": true}', encoding="ascii")
    person_id = await get_primary_person_id()
    actor_id = await seed_user("link-first-actor", role="admin")
    link_holds_flock = asyncio.Event()
    release_link = asyncio.Event()
    real_reserve_generation = garmin_registry._reserve_generation

    async def gated_reserve_generation(person: int) -> int:
        link_holds_flock.set()
        await release_link.wait()
        return await real_reserve_generation(person)

    calls: list[tuple[int, str | None]] = []

    def authenticate(person, generation, token_dir, email, password):
        calls.append((generation, password))
        token_dir.mkdir(mode=0o700, exist_ok=True)
        (token_dir / "garmin_tokens.json").write_text('{"account": "other"}', encoding="ascii")
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    clock = {"now": 100.0}
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry, "_reserve_generation", gated_reserve_generation)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_EMAIL", "legacy@example.test")

    link = asyncio.create_task(
        garmin_registry.link(person_id, actor_id, 1, "other@example.test", "transient-password")
    )
    await asyncio.wait_for(link_holds_flock.wait(), timeout=5)
    clock["now"] = 200.0
    adoption = asyncio.create_task(garmin_registry.bootstrap_legacy_token_store())
    await asyncio.sleep(0.05)
    assert not adoption.done(), "adoption must wait for the person flock"
    assert calls == []

    release_link.set()
    assert (await asyncio.wait_for(link, timeout=5)).generation == 1
    assert await asyncio.wait_for(adoption, timeout=5) is False

    assert calls == [(1, "transient-password")], "adoption verified the flat store after the link"
    assert (root / "garmin_tokens.json").read_text(encoding="ascii") == '{"legacy": true}'
    durable = garmin_registry._generation_token_dir(person_id, 1)
    assert (durable / "garmin_tokens.json").read_text(encoding="ascii") == '{"account": "other"}'
    assert tuple(await _link_row(person_id)) == ("linked", "other@example.test", 1)
    assert not await _adoption_marker_recorded()


async def test_marker_blocks_re_adoption_for_a_different_primary(initialized_db, monkeypatch, tmp_path):
    """After the adopted person is archived and another one promoted, the
    new primary has no lifecycle rows at all; only the marker says the flat
    store was adopted once.  A restored backup at the root must not become
    the new primary's credential."""
    root = _flat_store(tmp_path)
    first_primary = await get_primary_person_id()
    second = await seed_person("next-primary")
    admin_id = await seed_user("promote-and-archive-admin", role="admin")
    calls: list[tuple[int, int, str | None]] = []
    clock = {"now": 100.0}
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _recording_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setenv("GARMIN_EMAIL", "legacy@example.test")

    assert await garmin_registry.bootstrap_legacy_token_store() is True
    assert await _adoption_marker_recorded()

    db = await get_db()
    try:
        await db.execute("UPDATE persons SET is_primary = 0")
        await db.execute("UPDATE persons SET is_primary = 1 WHERE id = ?", (second,))
        await db.commit()
    finally:
        await db.close()
    # Archive through the real admin route: it deletes the link row and
    # removes the person's token root in the same lifecycle as production.
    app = FastAPI()
    add_person_routes(app)
    cookies = {"vf_session": create_session_cookie("promote-and-archive-admin", admin_id, 1)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        archived = await client.post(f"/api/persons/{first_primary}/archive", cookies=cookies)
    assert archived.status_code == 200
    assert await _link_row(first_primary) is None
    assert not garmin_registry._person_token_root(first_primary).exists()
    (root / "garmin_tokens.json").write_text("{}", encoding="ascii")
    # A free call permit: only the marker may refuse this second adoption.
    clock["now"] = 200.0

    assert await garmin_registry.bootstrap_legacy_token_store() is False

    assert calls == [(first_primary, 1, None)], "the new primary's adoption reached Garmin"
    assert await _link_row(second) is None
    assert not garmin_registry._person_token_root(second).exists()
    assert (root / "garmin_tokens.json").is_file()


async def test_bootstrap_waits_for_a_busy_permit_instead_of_deferring(initialized_db, monkeypatch, tmp_path):
    """Adoption is one boot-time verification; a permit one interval away is
    worth a short wait, not a whole boot cycle."""
    root = _flat_store(tmp_path)
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    clock = {"now": 100.0}
    sleeps: list[float] = []

    async def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setattr(garmin_registry.asyncio, "sleep", advance)
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")
    await _set_next_allowed_at(102.0)

    assert await garmin_registry.bootstrap_legacy_token_store() is True

    assert sleeps == [2]
    assert calls == [(person_id, 1)]
    assert tuple(await _link_row(person_id)) == ("linked", f"person-{person_id}@example.test", 1)


async def test_bootstrap_warns_about_a_moved_store_the_primary_change_orphaned(
    initialized_db, monkeypatch, tmp_path, caplog
):
    """A crash between the move and the commit, followed by a change of
    primary, leaves generation-1 under a person that never got its row.
    Boot names that person (never the file's contents) and leaves it be."""
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    primary = await get_primary_person_id()
    orphaned = await seed_person("previous-primary")
    linked = await seed_person("genuinely-linked")
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    for person in (orphaned, linked):
        generation_dir = garmin_registry._generation_token_dir(person, 1)
        generation_dir.mkdir(parents=True, mode=0o700)
        _write_fake_token_store(generation_dir)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (linked,))
        await db.commit()
    finally:
        await db.close()
    calls: list[tuple[int, int, str | None]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _recording_auth(calls))
    monkeypatch.setenv("GARMIN_EMAIL", "legacy@example.test")
    caplog.set_level(logging.WARNING, logger="shared.garmin_registry_runtime")

    assert await garmin_registry.bootstrap_legacy_token_store() is False

    assert calls == []
    assert f"for person {orphaned} was never published" in caplog.text
    assert f"for person {linked} was never published" not in caplog.text
    assert f"for person {primary} was never published" not in caplog.text
    assert (garmin_registry._generation_token_dir(orphaned, 1) / "garmin_tokens.json").is_file()
    assert await _link_row(primary) is None


async def test_token_root_or_flock_setup_error_is_sanitized(initialized_db, monkeypatch):
    sentinel_path = "/private/garth/person-1"

    def cannot_prepare_lock(_person_id):
        raise OSError(f"permission denied: {sentinel_path}")

    monkeypatch.setattr(garmin_registry_locks, "_person_lock_path", cannot_prepare_lock)

    with pytest.raises(garmin_registry.GarminOperationError) as exc_info:
        await garmin_registry.call(1, lambda _client: pytest.fail("must not run"))
    assert exc_info.value.code == "unknown"
    assert sentinel_path not in str(exc_info.value)


async def test_link_attempt_reservation_is_per_user_and_resets_after_fifteen_minutes(
    initialized_db, monkeypatch
):
    first_user = await seed_user("first-user")
    second_user = await seed_user("second-user")
    clock = {"now": 100.0}
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])

    for _ in range(3):
        await garmin_registry.reserve_link_attempt(first_user)
    await garmin_registry.reserve_link_attempt(second_user)

    with pytest.raises(garmin_registry.GarminLinkAttemptRateLimited) as exc_info:
        await garmin_registry.reserve_link_attempt(first_user)
    assert exc_info.value.retry_after == 900

    # The interval is inclusive at exactly 15 minutes; it expires just after
    # that boundary and the stored dense slots are then compacted atomically.
    clock["now"] += 900.001
    await garmin_registry.reserve_link_attempt(first_user)
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT attempt_count FROM garmin_link_attempts WHERE user_id = ?", (first_user,)
            )
        ).fetchone()
    finally:
        await db.close()
    assert row["attempt_count"] == 1


async def test_link_attempt_retry_after_is_bounded_when_the_clock_moves_back(initialized_db, monkeypatch):
    user_id = await seed_user("clock-skew-user")
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 1.0)
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO garmin_link_attempts
                (user_id, window_started_at, attempt_count, attempted_at_1, attempted_at_2, attempted_at_3)
            VALUES (?, ?, 3, ?, ?, ?)
            """,
            (user_id, 10000.0, 10000.0, 10000.0, 10000.0),
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(garmin_registry.GarminLinkAttemptRateLimited) as exc_info:
        await garmin_registry.reserve_link_attempt(user_id)
    assert exc_info.value.retry_after == 900


async def test_link_attempt_reservation_is_atomic_under_concurrency(initialized_db, monkeypatch):
    user_id = await seed_user("attempt-user")
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    async def reserve() -> str:
        try:
            await garmin_registry.reserve_link_attempt(user_id)
        except garmin_registry.GarminLinkAttemptRateLimited:
            return "limited"
        return "accepted"

    outcomes = await asyncio.gather(*(reserve() for _ in range(4)))
    assert outcomes.count("accepted") == 3
    assert outcomes.count("limited") == 1


async def test_link_attempt_window_discards_only_expired_slots(initialized_db, monkeypatch):
    user_id = await seed_user("rolling-attempt-user")
    clock = {"now": 100.0}
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])

    for timestamp in (100.0, 200.0, 300.0):
        clock["now"] = timestamp
        await garmin_registry.reserve_link_attempt(user_id)

    clock["now"] = 1000.0  # attempt at 100 is still in the inclusive window.
    with pytest.raises(garmin_registry.GarminLinkAttemptRateLimited) as exc_info:
        await garmin_registry.reserve_link_attempt(user_id)
    assert exc_info.value.retry_after == 1

    clock["now"] = 1000.001
    await garmin_registry.reserve_link_attempt(user_id)
    db = await get_db()
    try:
        row = await (
            await db.execute(
                """
                SELECT window_started_at, attempt_count, attempted_at_1, attempted_at_2, attempted_at_3
                FROM garmin_link_attempts WHERE user_id = ?
                """,
                (user_id,),
            )
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == (200.0, 3, 200.0, 300.0, 1000.001)


async def test_operation_failure_hides_raw_exception_detail(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    calls: list[tuple[int, int]] = []
    sentinel = "third-party-response-with-email-token-and-path"
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    moments = iter((100.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))

    def failed_op(_client):
        raise RuntimeError(sentinel)

    with pytest.raises(garmin_registry.GarminOperationError) as exc_info:
        await garmin_registry.call(person_id, failed_op)
    assert sentinel not in str(exc_info.value)
    assert exc_info.value.code == "unknown"


async def test_auth_rejection_from_an_operation_evicts_cached_client(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    await _link(person_id)
    cached = _FakeClient(1)
    garmin_client._clients[(person_id, 1)] = cached
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    class Rejected(Exception):
        status_code = 401

    def rejected_op(_client):
        raise Rejected("do not expose this response")

    with pytest.raises(garmin_registry.GarminOperationError) as exc_info:
        await garmin_registry.call(person_id, rejected_op)
    assert exc_info.value.code == "auth_failed"
    assert (person_id, 1) not in garmin_client._clients

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT last_auth_error FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["last_auth_error"] == "auth_failed"


async def test_person_flock_serializes_same_person_operations(monkeypatch, tmp_path):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    person_id = 1
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with garmin_registry.person_flock(person_id):
            first_entered.set()
            await release_first.wait()

    async def second():
        await first_entered.wait()
        async with garmin_registry.person_flock(person_id):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


async def test_cancelling_a_waiting_flock_acquisition_releases_its_eventual_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    person_id = 1
    waiter_started = asyncio.Event()

    async def waiting_task():
        waiter_started.set()
        async with garmin_registry.person_flock(person_id):
            pytest.fail("the cancelled task must never enter the flock")

    async with garmin_registry.person_flock(person_id):
        waiter = asyncio.create_task(waiting_task())
        await waiter_started.wait()
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await waiter

    # The cancelled waiter may only acquire after the outer context exits;
    # its cancellation path must close that handle before this fresh entrant.
    async with garmin_registry.person_flock(person_id):
        pass


async def test_person_token_directories_are_distinct_and_private(monkeypatch, tmp_path):
    root = tmp_path / "garth"
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)

    first = garmin_registry.resolve_token_dir(1, 1)
    second = garmin_registry.resolve_token_dir(2, 1)
    garmin_client._ensure_token_dir(first)
    garmin_client._ensure_token_dir(second)

    assert first == root / "person-1" / "generation-1"
    assert second == root / "person-2" / "generation-1"
    assert first != second
    assert root.stat().st_mode & 0o777 == 0o700
    assert first.stat().st_mode & 0o777 == 0o700
    assert second.stat().st_mode & 0o777 == 0o700


async def test_link_publishes_canonical_email_generation_and_staged_store(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("link-actor", role="admin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    result = await garmin_registry.link(
        person_id, actor_id, 1, "  OWNER@Example.Test  ", "transient-password"
    )

    assert result == garmin_registry.GarminLink(person_id, 1, "linked")
    assert calls == [(person_id, 1)]
    db = await get_db()
    try:
        link_row = await (
            await db.execute(
                "SELECT state, garmin_email, generation, linked_by FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
        ledger = await (
            await db.execute("SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert tuple(link_row) == ("linked", "owner@example.test", 1, actor_id)
    assert ledger["generation"] == 1
    final = garmin_registry._generation_token_dir(person_id, 1)
    assert (final / "garmin_tokens.json").exists()
    assert final.stat().st_mode & 0o777 == 0o700
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))
    # The link-time client's persistence path is the renamed-away staging
    # directory; it must not stay cached and dump a refreshed token there.
    assert not garmin_client._clients


async def test_first_call_after_link_resumes_from_the_published_generation(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("cold-after-link-actor", role="admin")
    token_dirs: list = []

    def authenticate(person, generation, token_dir, email, password):
        token_dirs.append((generation, token_dir, password))
        _write_fake_token_store(token_dir)
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)
    moments = iter((100.0, 100.0, 110.0, 112.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))

    await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")
    assert await garmin_registry.call(person_id, lambda client: client.generation) == 1

    final = garmin_registry._generation_token_dir(person_id, 1)
    assert [(generation, password) for generation, _dir, password in token_dirs] == [
        (1, "transient-password"),
        (1, None),
    ]
    assert token_dirs[0][1] != final, "login must happen in a private staging directory"
    assert token_dirs[1][1] == final


async def test_link_refuses_to_publish_a_login_that_left_no_token_store(initialized_db, monkeypatch):
    """login() can return without persisting; publishing that would create a
    link whose first cold call resumes nothing."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("empty-store-actor", role="admin")

    def authenticate(person, generation, token_dir, email, password):
        token_dir.mkdir(mode=0o700, exist_ok=True)
        (token_dir / "garmin_tokens.json").touch()
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(garmin_registry.GarminOperationError) as exc_info:
        await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")

    assert exc_info.value.code == "unknown"
    assert await _link_row(person_id) is None
    assert not garmin_registry._generation_token_dir(person_id, 1).exists()
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))
    assert not garmin_client._clients


async def test_link_and_unlink_sweep_staging_residue_of_a_killed_attempt(initialized_db, monkeypatch):
    """A SIGKILL mid-login leaves a staging directory nothing else references."""
    person_id = await get_primary_person_id()
    other_person = await seed_person("other-residue-person")
    actor_id = await seed_user("residue-actor", role="admin")
    root = garmin_registry._ensure_token_root()
    residue = root / f".person-{person_id}-generation-7-deadbeef.staging"
    other_residue = root / f".person-{other_person}-generation-1-cafe.staging"
    for path in (residue, other_residue):
        _write_fake_token_store(path)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")
    assert not residue.exists()
    assert other_residue.exists(), "another person's residue is outside this person's flock"

    _write_fake_token_store(residue)
    assert await garmin_registry.unlink(person_id, actor_id, 1)
    assert not residue.exists()
    assert other_residue.exists()

    # Residue belongs to no link row, so an unlink with nothing to remove
    # still sweeps it.
    _write_fake_token_store(residue)
    assert await garmin_registry.unlink(person_id, actor_id, 1) is False
    assert not residue.exists()
    assert other_residue.exists()


async def test_cancelled_login_burns_its_generation_and_never_reaches_a_later_link(initialized_db, monkeypatch):
    """The login thread cannot be interrupted.  Its generation is reserved
    before it starts, so whatever it caches or writes later belongs to a
    number no published link will ever resolve."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("cancel-login-actor", role="admin")
    first_started = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    calls: list[tuple[int, str]] = []

    def gated_authenticate(person, generation, token_dir, email, password):
        calls.append((generation, email))
        _write_fake_token_store(token_dir)
        if email == "first@example.test":
            first_started.set()
            assert release_first.wait(timeout=5)
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        if email == "first@example.test":
            first_finished.set()
        return client

    clock = {"now": 100.0}
    real_sleep = asyncio.sleep
    monkeypatch.setattr(garmin_registry.time, "time", lambda: clock["now"])
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", gated_authenticate)

    stale = asyncio.create_task(
        garmin_registry.link(person_id, actor_id, 1, "first@example.test", "transient-password")
    )
    assert await asyncio.to_thread(first_started.wait, 5)
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    clock["now"] = 200.0
    published = await garmin_registry.link(person_id, actor_id, 1, "second@example.test", "transient-password")
    assert published.generation == 2
    assert calls == [(1, "first@example.test"), (2, "second@example.test")]
    assert (person_id, 2) not in garmin_client._clients

    release_first.set()
    assert await asyncio.to_thread(first_finished.wait, 5)
    for _ in range(100):
        if (person_id, 1) not in garmin_client._clients:
            break
        await real_sleep(0.01)
    assert (person_id, 1) not in garmin_client._clients, "the abandoned login's client was not discarded"
    assert (person_id, 2) not in garmin_client._clients, "the stale thread must not touch the newer generation"

    async def advance(seconds: float) -> None:
        clock["now"] += seconds
        await real_sleep(0)

    clock["now"] = 300.0
    monkeypatch.setattr(garmin_registry.asyncio, "sleep", advance)
    assert await garmin_registry.call(person_id, lambda client: client.generation) == 2
    assert calls[-1] == (2, "second@example.test")
    assert set(garmin_client._clients) == {(person_id, 2)}


async def test_failed_relink_preserves_existing_link_and_token_store(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("relink-actor", role="admin")
    await _link(person_id, generation=1)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    final = garmin_registry._generation_token_dir(person_id, 1)
    final.mkdir(parents=True, mode=0o700)
    marker = final / "old-token"
    marker.touch()

    def failed_auth(*_args):
        raise RuntimeError("password-and-token-must-not-escape")

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", failed_auth)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    db = await get_db()
    try:
        await db.execute(
            "UPDATE garmin_links SET last_auth_ok = '2026-09-15T00:00:00Z' WHERE person_id = ?", (person_id,)
        )
        await db.commit()
    finally:
        await db.close()
    with pytest.raises(garmin_registry.GarminAuthenticationError) as exc_info:
        await garmin_registry.relink(person_id, actor_id, 1, "new@example.test", "transient-password")
    assert "password-and-token" not in str(exc_info.value)
    assert marker.exists()
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT garmin_email, generation, last_auth_ok, last_auth_error, last_auth_error_at "
                "FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
        ledger = await (
            await db.execute("SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    # The failed attempt had its own credentials; the existing generation's
    # health must not be stamped with that attempt's rejection.
    assert tuple(row) == (f"person-{person_id}@example.test", 1, "2026-09-15T00:00:00Z", None, None)
    assert ledger["generation"] == 2, "a failed attempt burns its reserved generation"


async def test_failed_publication_removes_new_generation_and_preserves_old_store(initialized_db, monkeypatch):
    """A post-login re-link failure never makes the new store reachable."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("swap-recovery-actor", role="admin")
    await _link(person_id, generation=1)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    final = garmin_registry._generation_token_dir(person_id, 1)
    final.mkdir(parents=True, mode=0o700)
    (final / "account-marker").write_text("old", encoding="ascii")

    def authenticate(person_id, generation, token_dir, email, password):
        assert password == "transient-password"
        _write_fake_token_store(token_dir)
        (token_dir / "account-marker").write_text("new", encoding="ascii")
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        return client

    async def rejected_publish(*_args):
        raise garmin_registry.GarminSessionExpired()

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)
    monkeypatch.setattr(garmin_registry, "_publish_link", rejected_publish)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    with pytest.raises(garmin_registry.GarminSessionExpired):
        await garmin_registry.relink(person_id, actor_id, 1, "new@example.test", "transient-password")

    assert (final / "account-marker").read_text(encoding="ascii") == "old"
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_email, generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == (f"person-{person_id}@example.test", 1)
    assert not garmin_registry._generation_token_dir(person_id, 2).exists()
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))


async def test_link_evicts_the_link_time_client_even_when_cancelled_after_publication(
    initialized_db, monkeypatch
):
    """The publication's post-commit ``db.close()`` is an await; a request
    cancelled exactly there has a durable link and a cached client whose
    SDK persistence path is the staging directory that no longer exists."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("cancel-after-publish-actor", role="admin")
    calls: list[tuple[int, int]] = []
    real_publish = garmin_registry._publish_link

    async def publish_then_cancel(*args):
        await real_publish(*args)
        raise asyncio.CancelledError()

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry, "_publish_link", publish_then_cancel)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(asyncio.CancelledError):
        await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")

    assert calls == [(person_id, 1)]
    assert tuple(await _link_row(person_id)) == ("linked", "owner@example.test", 1)
    assert (garmin_registry._generation_token_dir(person_id, 1) / "garmin_tokens.json").is_file()
    assert not garmin_client._clients, "the staging-pathed client outlived the cancelled request"


async def test_relink_still_succeeds_when_the_superseded_generation_cannot_be_removed(
    initialized_db, monkeypatch, caplog
):
    """Once the new generation is durable the link has succeeded; failing to
    remove the old directory is residue for the next call() to sweep, not a
    reason to answer the user with an error."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("stubborn-cleanup-actor", role="admin")
    await _link(person_id, generation=1)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    old_dir = garmin_registry._generation_token_dir(person_id, 1)
    old_dir.mkdir(parents=True, mode=0o700)
    _write_fake_token_store(old_dir)
    calls: list[tuple[int, int]] = []
    real_remove = garmin_registry._remove_token_dir

    def stubborn_remove(path):
        if path == old_dir:
            raise PermissionError("directory-detail-must-not-escape")
        real_remove(path)

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry, "_remove_token_dir", stubborn_remove)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    caplog.set_level(logging.WARNING, logger="shared.garmin_registry")

    published = await garmin_registry.relink(person_id, actor_id, 1, "new@example.test", "transient-password")

    assert published == garmin_registry.GarminLink(person_id, 2, "linked")
    assert tuple(await _link_row(person_id)) == ("linked", "new@example.test", 2)
    assert (garmin_registry._generation_token_dir(person_id, 2) / "garmin_tokens.json").is_file()
    assert old_dir.exists()
    assert "PermissionError" in caplog.text
    assert "directory-detail" not in caplog.text
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))


async def test_link_rejects_an_account_already_linked_to_another_person(initialized_db, monkeypatch):
    first_person = await get_primary_person_id()
    second_person = await seed_person("other-link-person")
    actor_id = await seed_user("conflict-actor", role="admin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    moments = iter((100.0, 100.0, 100.0, 102.0, 102.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))

    await garmin_registry.link(first_person, actor_id, 1, "same@example.test", "transient-password")
    with pytest.raises(garmin_registry.GarminLinkConflict):
        await garmin_registry.link(second_person, actor_id, 1, " SAME@example.test ", "transient-password")

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT person_id FROM garmin_links WHERE garmin_email = ?", ("same@example.test",))
        ).fetchone()
        second = await (
            await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (second_person,))
        ).fetchone()
    finally:
        await db.close()
    assert row["person_id"] == first_person
    assert second is None
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))


async def test_link_requires_current_actor_session_at_atomic_publish(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("stale-session-actor", role="admin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(garmin_registry.GarminSessionExpired):
        await garmin_registry.link(person_id, actor_id, 2, "owner@example.test", "transient-password")
    # A stale browser session must be rejected before its submitted Garmin
    # password is sent to the adapter.  _publish_link repeats this check in
    # BEGIN IMMEDIATE to close the later login-to-publication race.
    assert calls == []
    db = await get_db()
    try:
        row = await (await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))).fetchone()
    finally:
        await db.close()
    assert row is None
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))
    assert not garmin_client._clients


async def test_link_rejects_a_removed_person_before_garmin_login(initialized_db, monkeypatch):
    actor_id = await seed_user("removed-target-actor", role="admin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(garmin_registry.GarminNotLinked):
        await garmin_registry.link(999_999, actor_id, 1, "owner@example.test", "transient-password")

    assert calls == []
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))


async def test_link_rechecks_manage_grant_revoked_while_login_is_in_flight(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("revoked-link-grant")
    await _grant_manage(person_id, actor_id)
    auth_started = threading.Event()
    release_auth = threading.Event()

    def authenticate(person, generation, token_dir, _email, _password):
        _write_fake_token_store(token_dir)
        auth_started.set()
        assert release_auth.wait(timeout=5)
        client = _FakeClient(generation)
        garmin_client._clients[(person, generation)] = client
        return client

    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    task = asyncio.create_task(
        garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")
    )
    assert await asyncio.to_thread(auth_started.wait, 5)
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM person_grants WHERE person_id = ? AND user_id = ?", (person_id, actor_id)
        )
        await db.commit()
    finally:
        await db.close()
    release_auth.set()
    with pytest.raises(garmin_registry.GarminSessionExpired):
        await task
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row is None
    assert (person_id, 1) not in garmin_client._clients


async def test_link_and_atomic_publish_refuse_an_archived_person_after_authorization(
    initialized_db, monkeypatch
):
    """An already-authorized request must not revive a person archive.

    Person authorization happens in the route before ``link`` obtains its
    flock.  If an archive commits while such a request is waiting, the fresh
    pre-login check rejects it.  The direct publication assertion pins the
    second check that closes the later credential-login-to-commit interval.
    """
    person_id = await seed_person("archived-link-target")
    actor_id = await seed_user("archived-link-actor", role="admin")
    db = await get_db()
    try:
        await db.execute(
            "UPDATE persons SET archived_at = ? WHERE id = ?",
            ("2026-09-15T00:00:00Z", person_id),
        )
        await db.commit()
    finally:
        await db.close()

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _lifecycle_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    with pytest.raises(garmin_registry.GarminNotLinked):
        await garmin_registry.link(
            person_id, actor_id, 1, "owner@example.test", "transient-password"
        )
    assert calls == [], "an archived target must be rejected before Garmin login"

    with pytest.raises(garmin_registry.GarminNotLinked):
        await garmin_registry._publish_link(person_id, actor_id, 1, "owner@example.test", 1)
    db = await get_db()
    try:
        link = await (
            await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert link is None


async def test_concurrent_links_cannot_claim_one_canonical_account_twice(initialized_db, monkeypatch):
    """The uniqueness query and publication are one BEGIN IMMEDIATE transaction."""
    first_person = await get_primary_person_id()
    second_person = await seed_person("simultaneous-link-person")
    actor_id = await seed_user("simultaneous-link-actor", role="admin")
    calls: list[tuple[int, int]] = []
    calls_lock = threading.Lock()
    both_authentications_started = threading.Event()
    release_authentications = threading.Event()

    def authenticate(person_id, generation, token_dir, email, password):
        assert password == "transient-password"
        _write_fake_token_store(token_dir)
        with calls_lock:
            calls.append((person_id, generation))
            if len(calls) == 2:
                both_authentications_started.set()
        assert release_authentications.wait(timeout=2)
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        return client

    # Each lifecycle login receives a global permit; the two later calls here
    # are the independent per-user attempt timestamps.
    moments = iter((100.0, 102.0, 102.0, 102.0))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(moments))
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", authenticate)

    first = asyncio.create_task(
        garmin_registry.link(first_person, actor_id, 1, "same@example.test", "transient-password")
    )
    second = asyncio.create_task(
        garmin_registry.link(second_person, actor_id, 1, " SAME@example.test ", "transient-password")
    )
    assert await asyncio.to_thread(both_authentications_started.wait, 2)
    release_authentications.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(outcome, garmin_registry.GarminLink) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, garmin_registry.GarminLinkConflict) for outcome in outcomes) == 1
    db = await get_db()
    try:
        rows = await (
            await db.execute("SELECT person_id FROM garmin_links WHERE garmin_email = ?", ("same@example.test",))
        ).fetchall()
    finally:
        await db.close()
    assert len(rows) == 1
    assert not list(garmin_registry.GARTH_TOKEN_DIR.glob("*.staging"))


async def test_unlink_removes_normal_link_after_commit_and_keeps_generation_ledger(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("unlink-actor", role="admin")
    await _link(person_id, generation=1)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 1)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    final = garmin_registry._generation_token_dir(person_id, 1)
    final.mkdir(parents=True, mode=0o700)
    (final / "garmin_tokens.json").touch()
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)

    assert await garmin_registry.unlink(person_id, actor_id, 1)
    db = await get_db()
    try:
        link = await (await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))).fetchone()
        ledger = await (
            await db.execute("SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert link is None
    assert ledger["generation"] == 1
    assert not final.exists()
    assert not garmin_registry._person_token_root(person_id).exists()
    assert not garmin_client._clients


async def test_unlink_deletes_a_retired_legacy_bound_row_and_leaves_the_flat_root_alone(initialized_db):
    """A database from the release that wrote 'legacy_bound' still carries the
    row; unlink removes it like any other and never touches the flat root."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("legacy-unlink-actor", role="admin")
    await _link(person_id, generation=3, state="legacy_bound")
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 3)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    flat_token = garmin_registry._ensure_token_root() / "garmin_tokens.json"
    flat_token.write_text("{}", encoding="ascii")

    with pytest.raises(garmin_registry.GarminNotLinked):
        await garmin_registry.call(person_id, lambda _client: pytest.fail("a retired state is not usable"))
    assert await garmin_registry.unlink(person_id, actor_id, 1)
    assert await _link_row(person_id) is None
    assert flat_token.is_file()
    db = await get_db()
    try:
        ledger = await (
            await db.execute("SELECT generation FROM garmin_link_generations WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert ledger["generation"] == 3


async def test_unlink_rejects_stale_actor_session_without_mutating_or_cleaning(initialized_db):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("stale-unlink-actor", role="admin")
    await _link(person_id, generation=1)
    token_dir = garmin_registry._generation_token_dir(person_id, 1)
    token_dir.mkdir(parents=True, mode=0o700)
    (token_dir / "garmin_tokens.json").touch()
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)

    with pytest.raises(garmin_registry.GarminSessionExpired):
        await garmin_registry.unlink(person_id, actor_id, 2)

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT state, generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == ("linked", 1)
    assert token_dir.exists()
    assert (person_id, 1) in garmin_client._clients


async def test_unlink_rechecks_manage_grant_revoked_while_waiting_for_its_flock(initialized_db):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("revoked-unlink-grant")
    await _grant_manage(person_id, actor_id)
    await _link(person_id)
    token_dir = garmin_registry._generation_token_dir(person_id, 1)
    token_dir.mkdir(parents=True, mode=0o700)
    (token_dir / "garmin_tokens.json").touch()
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)

    async with garmin_registry.person_flock(person_id):
        task = asyncio.create_task(garmin_registry.unlink(person_id, actor_id, 1))
        await asyncio.sleep(0)
        db = await get_db()
        try:
            await db.execute(
                "DELETE FROM person_grants WHERE person_id = ? AND user_id = ?", (person_id, actor_id)
            )
            await db.commit()
        finally:
            await db.close()

    with pytest.raises(garmin_registry.GarminSessionExpired):
        await task
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row is not None
    assert token_dir.exists()
    assert (person_id, 1) in garmin_client._clients


async def test_exhausted_link_attempt_preflight_does_not_spend_global_call_budget(initialized_db, monkeypatch):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("exhausted-link-attempt", role="admin")
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    for _ in range(3):
        await garmin_registry.reserve_link_attempt(actor_id)

    with pytest.raises(garmin_registry.GarminLinkAttemptRateLimited):
        await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT next_allowed_at FROM garmin_call_budget WHERE singleton = 1")
        ).fetchone()
    finally:
        await db.close()
    assert row["next_allowed_at"] == 0


async def test_call_cleans_post_publication_crash_stale_generation_directory(initialized_db, monkeypatch):
    """A crash after DB publication but before old-dir cleanup is self-healed.

    Generation 2 is durable, while generation 1 is an inert residue.  The
    first safe registry entry owns the person flock and removes only 1; it
    must keep the durable generation 2 store intact.
    """
    person_id = await get_primary_person_id()
    await _link(person_id, generation=2)
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 2)", (person_id,))
        await db.commit()
    finally:
        await db.close()
    old_dir = garmin_registry._generation_token_dir(person_id, 1)
    new_dir = garmin_registry._generation_token_dir(person_id, 2)
    old_dir.mkdir(parents=True, mode=0o700)
    new_dir.mkdir(parents=True, mode=0o700)
    (old_dir / "garmin_tokens.json").touch()
    (new_dir / "garmin_tokens.json").touch()
    garmin_client._clients[(person_id, 2)] = _FakeClient(2)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    assert await garmin_registry.call(person_id, lambda client: client.generation) == 2
    assert not old_dir.exists()
    assert new_dir.exists()



def _with_response(exc: Exception, status_code: int) -> Exception:
    class _Response:
        pass

    response = _Response()
    response.status_code = status_code
    exc.response = response
    return exc


@pytest.mark.parametrize(
    "exc, code",
    (
        (GarminConnectTooManyRequestsError("Mobile login returned 429 — IP rate limited by Garmin"), "rate_limited"),
        (GarminConnectTooManyRequestsError("Portal login: 429 in JSON body"), "rate_limited"),
        (GarminConnectAuthenticationError("Not authenticated"), "auth_failed"),
        (GarminConnectAuthenticationError("Login failed: bad credentials"), "auth_failed"),
        (GarminConnectConnectionError("API Error 429"), "rate_limited"),
        (GarminConnectConnectionError("API Error 401 - Unauthorized"), "auth_failed"),
        (GarminConnectConnectionError("API Error 403 - Forbidden"), "auth_failed"),
        (GarminConnectConnectionError("Mobile login: HTTP 403 (Cloudflare bot challenge) — next"), "auth_failed"),
        (GarminConnectConnectionError("Mobile login failed (non-JSON): HTTP 502"), "network"),
        (GarminConnectConnectionError("Widget embed returned 503"), "network"),
        (GarminConnectConnectionError("curl_cffi not available"), "network"),
        (_with_response(GarminConnectConnectionError("wrapped"), 429), "rate_limited"),
        (GarminConnectNotFoundError("API Error 404 - no data"), "unknown"),
        (TimeoutError("read timed out"), "network"),
        (RuntimeError("HTTP 401 mentioned by an unrelated error"), "unknown"),
    ),
)
def test_error_code_classifies_real_garminconnect_exceptions(exc, code):
    assert garmin_registry._error_code(exc) == code


async def test_rate_limited_operation_keeps_the_cached_client_and_status(initialized_db):
    person_id = await get_primary_person_id()
    await _link(person_id)
    cached = _FakeClient(1)
    garmin_client._clients[(person_id, 1)] = cached
    await garmin_registry._record_auth_success(person_id, 1)

    def throttled_op(_client):
        raise GarminConnectTooManyRequestsError("Portal login POST returned 429 — Cloudflare blocking this request.")

    with pytest.raises(garmin_registry.GarminOperationError) as exc_info:
        await garmin_registry.call(person_id, throttled_op)

    assert exc_info.value.code == "rate_limited"
    assert "Cloudflare" not in str(exc_info.value)
    assert garmin_client._clients[(person_id, 1)] is cached
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT last_auth_error FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["last_auth_error"] is None


async def test_unbounded_failure_is_logged_by_type_name_only(initialized_db, monkeypatch, caplog):
    sentinel_path = "/private/garth/person-1"

    def cannot_prepare_lock(_person_id):
        raise OSError(f"permission denied: {sentinel_path}")

    monkeypatch.setattr(garmin_registry_locks, "_person_lock_path", cannot_prepare_lock)
    caplog.set_level(logging.WARNING, logger="shared.garmin_registry")

    with pytest.raises(garmin_registry.GarminOperationError):
        await garmin_registry.call(1, lambda _client: pytest.fail("must not run"))

    assert "OSError" in caplog.text
    assert sentinel_path not in caplog.text
