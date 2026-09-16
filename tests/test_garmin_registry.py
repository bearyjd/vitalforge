"""Focused contract tests for the Garmin registry's security boundary."""

import asyncio
import threading

import pytest

from shared import garmin_client, garmin_registry
from shared.database import get_db, get_primary_person_id
from tests.conftest import seed_person, seed_user


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


def _fake_auth(calls: list[tuple[int, int]]):
    def authenticate(person_id, generation, token_dir, email, password):
        assert password is None
        assert email == f"person-{person_id}@example.test"
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        calls.append((person_id, generation))
        return client

    return authenticate


def _lifecycle_auth(calls: list[tuple[int, int]]):
    """Fake login that leaves an inert token artifact in the staging dir."""
    def authenticate(person_id, generation, token_dir, email, password):
        assert password == "transient-password"
        token_dir.mkdir(mode=0o700, exist_ok=True)
        (token_dir / "garmin_tokens.json").touch()
        client = _FakeClient(generation)
        garmin_client._clients[(person_id, generation)] = client
        calls.append((person_id, generation))
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


async def test_call_permit_retry_after_stays_within_configured_bounds(initialized_db, monkeypatch):
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    db = await get_db()
    try:
        await db.execute("UPDATE garmin_call_budget SET next_allowed_at = ?", (10000.0,))
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(garmin_registry.GarminRateLimited) as exc_info:
        await garmin_registry.reserve_call_permit()
    assert exc_info.value.retry_after == 60


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


async def test_bootstrap_adopts_only_a_verified_flat_token_store_once(
    initialized_db, monkeypatch, tmp_path
):
    """A verified flat store binds once with canonical metadata, never copied."""
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    (root / "garmin_tokens.json").touch()
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f" Person-{person_id}@Example.Test ")

    assert await garmin_registry.bootstrap_legacy_token_store() is True
    # The second service startup observes the durable legacy-bound row under
    # the parent flock and must neither authenticate nor create another link.
    assert await garmin_registry.bootstrap_legacy_token_store() is False
    assert calls == [(person_id, 1)]
    assert (root / "garmin_tokens.json").is_file()

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT state, garmin_email, generation FROM garmin_links WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == ("legacy_bound", f"person-{person_id}@example.test", 1)


async def test_concurrent_startups_adopt_a_verified_flat_store_exactly_once(
    initialized_db, monkeypatch, tmp_path
):
    """The GARTH parent flock makes two service lifespans one adoption."""
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    (root / "garmin_tokens.json").touch()
    person_id = await get_primary_person_id()
    calls: list[tuple[int, int]] = []
    auth_started = threading.Event()
    permit_publish = threading.Event()

    def gated_authenticate(person, generation, token_dir, email, password):
        assert (person, generation, email, password) == (
            person_id,
            1,
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
    assert [tuple(row) for row in rows] == [
        ("legacy_bound", f"person-{person_id}@example.test", 1)
    ]


async def test_bootstrap_does_not_bind_a_flat_store_that_fails_verification(
    initialized_db, monkeypatch, tmp_path, caplog
):
    """A provider failure leaves no durable link and never logs token detail."""
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    (root / "garmin_tokens.json").touch()
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

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT 1 FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row is None


async def test_token_root_or_flock_setup_error_is_sanitized(initialized_db, monkeypatch):
    sentinel_path = "/private/garth/person-1"

    def cannot_prepare_lock(_person_id):
        raise OSError(f"permission denied: {sentinel_path}")

    monkeypatch.setattr(garmin_registry, "_person_lock_path", cannot_prepare_lock)

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

    first = await garmin_registry.resolve_token_dir(1, "linked", 1)
    second = await garmin_registry.resolve_token_dir(2, "linked", 1)
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
    with pytest.raises(garmin_registry.GarminAuthenticationError) as exc_info:
        await garmin_registry.relink(person_id, actor_id, 1, "new@example.test", "transient-password")
    assert "password-and-token" not in str(exc_info.value)
    assert marker.exists()
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT garmin_email, generation FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == (f"person-{person_id}@example.test", 1)


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
        token_dir.mkdir(mode=0o700, exist_ok=True)
        (token_dir / "garmin_tokens.json").touch()
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
        token_dir.mkdir(mode=0o700, exist_ok=True)
        (token_dir / "garmin_tokens.json").touch()
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


async def test_unlink_tombstones_legacy_bound_link_without_removing_generation(initialized_db):
    person_id = await get_primary_person_id()
    actor_id = await seed_user("legacy-unlink-actor", role="admin")
    await _link(person_id, generation=3, state="legacy_bound")
    db = await get_db()
    try:
        await db.execute("INSERT INTO garmin_link_generations VALUES (?, 3)", (person_id,))
        await db.commit()
    finally:
        await db.close()

    assert await garmin_registry.unlink(person_id, actor_id, 1)
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT state, garmin_email, generation, linked_at FROM garmin_links WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
    finally:
        await db.close()
    assert tuple(row) == ("legacy_disabled", None, 3, None)


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
