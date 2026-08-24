"""Shared pytest fixtures for VitalForge.

Isolates every test from real infrastructure:
- SQLite DB lives in a per-test tmp_path, never `/app/data/fitness.db`.
- Garmin Connect is never contacted; `shared.garmin_client` is monkeypatched
  to a FakeGarminClient returning canned, synthetic responses.
- `vitalforge-weight` and `vitalforge-dashboard` are hyphenated directory
  names, so they're loaded via `importlib.import_module` (the same mechanism
  uvicorn uses for the documented `uvicorn vitalforge-weight.app:app` command)
  rather than a normal `import` statement.
"""

import hashlib
import importlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "garmin"
PRODUCTION_SCHEMA_SQL = Path(__file__).resolve().parent / "fixtures" / "production_schema.sql"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_fixture(name: str):
    """Load a synthetic Garmin response fixture by file name (no extension)."""
    with open(FIXTURES_DIR / f"{name}.json") as f:
        return json.load(f)


class FakeGarminClient:
    """Stand-in for `garminconnect.Garmin`, returning synthetic fixture data.

    Every pull method ignores its date/range arguments and returns the same
    canned shape — sufficient for exercising the parsing logic in `sync.py`
    and the app routes without ever touching a real Garmin account.
    """

    def __init__(self):
        self.pushed_weights = []

    def add_body_composition(self, timestamp, weight, **kwargs):
        self.pushed_weights.append({"timestamp": timestamp, "weight": weight, **kwargs})
        return {"success": True}

    def get_sleep_data(self, date):
        return load_fixture("sleep_data")

    def get_user_summary(self, date):
        return load_fixture("user_summary")

    def get_hrv_data(self, date):
        return load_fixture("hrv_data")

    def get_body_battery(self, date):
        return load_fixture("body_battery")

    def get_stress_data(self, date):
        return load_fixture("stress_data")

    def get_max_metrics(self, date):
        return load_fixture("max_metrics")

    def get_weigh_ins(self, start_date, end_date):
        return load_fixture("weigh_ins")

    def get_training_status(self, date):
        return load_fixture("training_status")


@pytest.fixture
def fake_garmin_client(monkeypatch):
    """Patch `shared.garmin_client` so no real Garmin call can happen.

    Returns the FakeGarminClient instance so tests can assert on pushed data
    (e.g. `fake_garmin_client.pushed_weights`).
    """
    from shared import garmin_client

    fake = FakeGarminClient()
    monkeypatch.setattr(garmin_client, "_client", fake)
    monkeypatch.setattr(garmin_client, "authenticate", lambda: None)
    yield fake


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Point `shared.database.DB_PATH` at an isolated tmp SQLite file.

    Never touches the real fitness.db. `shared.database.get_db()` re-reads
    the module-level `DB_PATH` global on every call, so patching it here
    (even after `shared.database` has already been imported elsewhere)
    is sufficient to isolate every DB access made during the test.
    """
    from shared import database

    db_path = tmp_path / "vf-test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("GARTH_TOKEN_DIR", str(tmp_path / "garth"))
    return db_path


@pytest_asyncio.fixture
async def production_schema_db(tmp_path, monkeypatch):
    """A tmp DB loaded from the real production schema dump, pre-migration,
    seeded to production's actual row counts for the two tables Track B
    touches (`weight_log`=17, `weight_history`=34 -- see
    tests/fixtures/production_schema.sql). Never touches the real fitness.db.

    The dump's `CREATE TABLE sqlite_sequence` statement is filtered out --
    SQLite refuses to create that table directly (it's reserved, recreated
    automatically from `weight_log`'s AUTOINCREMENT) -- see
    docs/prp/01-plan.md SS4.1.
    """
    from shared import database

    db_path = tmp_path / "production-schema.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))

    statements = [
        stmt.strip()
        for stmt in PRODUCTION_SCHEMA_SQL.read_text().split(";")
        if stmt.strip() and "sqlite_sequence" not in stmt
    ]

    db = await aiosqlite.connect(str(db_path))
    try:
        for stmt in statements:
            await db.execute(stmt)

        now = datetime.now(timezone.utc)
        for i in range(17):
            ts = (now - timedelta(minutes=i)).isoformat()
            await db.execute(
                "INSERT INTO weight_log (weight_lbs, weight_kg, weight_grams, timestamp, synced_to_garmin) "
                "VALUES (?, ?, ?, ?, 1)",
                (180.0 + i, 81.6 + i * 0.1, 81600 + i * 100, ts),
            )
        for i in range(34):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            await db.execute(
                "INSERT INTO weight_history (date, weight_grams, bmi, body_fat) VALUES (?, ?, ?, ?)",
                (date, 81600 + i * 50, 24.0, 18.0),
            )
        await db.commit()
    finally:
        await db.close()

    return db_path


@pytest_asyncio.fixture
async def initialized_db(tmp_db_path):
    """`tmp_db_path` plus a freshly created (empty) schema."""
    from shared.database import init_db

    await init_db()
    return tmp_db_path


def import_service_module(dotted_path: str):
    """Import a module from a hyphenated service directory, e.g.

    `import_service_module("vitalforge-weight.app")`.
    """
    return importlib.import_module(dotted_path)


async def seed_user(username: str, password: str = "irrelevant-for-this-test", role: str = "user") -> int:
    """Insert a user row directly via SQL, bypassing the route layer --
    mirrors test_dedup.py's seed_row for auth-related tests that need a
    real, DB-backed user for get_current_user's live re-check (users table
    membership, not just a validly-signed cookie) to pass."""
    from shared import auth as shared_auth
    from shared.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, shared_auth._hash_password(password), role, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def seed_token(user_id: int, label: str = "test-token", raw_token: str | None = None) -> tuple[int, str]:
    """Insert a hash-only API token and return (row id, raw token)."""
    from shared.database import get_db

    raw = raw_token or secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO api_tokens (user_id, label, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, label, token_hash, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid, raw
    finally:
        await db.close()


@pytest.fixture
def weight_app_module(initialized_db, fake_garmin_client, monkeypatch):
    """The `vitalforge-weight` FastAPI app module, Garmin/DB fully faked."""
    module = import_service_module("vitalforge-weight.app")
    # `authenticate`/`push_weight` were bound into app.py's namespace via
    # `from shared.garmin_client import ...`, so patching the shared module
    # alone doesn't reach them — patch the names the route handlers actually call.
    monkeypatch.setattr(module, "authenticate", lambda: None)

    def fake_push_weight(weight_grams, timestamp=None, **kwargs):
        fake_garmin_client.pushed_weights.append(
            {"weight_grams": weight_grams, "timestamp": timestamp, **kwargs}
        )

    monkeypatch.setattr(module, "push_weight", fake_push_weight)
    return module


@pytest.fixture
def dashboard_app_module(initialized_db, fake_garmin_client, monkeypatch):
    """The `vitalforge-dashboard` FastAPI app module, Garmin/DB fully faked."""
    module = import_service_module("vitalforge-dashboard.app")
    # Same direct-import situation as vitalforge-weight/app.py.
    monkeypatch.setattr(module, "authenticate", lambda: None)
    return module


@pytest.fixture
def weight_live_server(tmp_db_path, fake_garmin_client, monkeypatch):
    """The `vitalforge-weight` app, served for real over HTTP for Playwright.

    Deliberately does NOT depend on `initialized_db`/`weight_app_module`:
    both pull in an async fixture, and Playwright's sync API keeps its own
    event loop running in this (main) test thread for the whole session, so
    any `pytest-asyncio` fixture setup here collides with it (`RuntimeError:
    Runner.run() cannot be called from a running event loop`). Instead,
    `DB_PATH` is patched (via `tmp_db_path`, a plain sync fixture) and left
    for the live server's own `lifespan` to call `init_db()` inside its
    dedicated server thread, where no such conflict exists.
    """
    module = import_service_module("vitalforge-weight.app")
    monkeypatch.setattr(module, "authenticate", lambda: None)

    def fake_push_weight(weight_grams, timestamp=None, **kwargs):
        fake_garmin_client.pushed_weights.append({"weight_grams": weight_grams, "timestamp": timestamp, **kwargs})

    monkeypatch.setattr(module, "push_weight", fake_push_weight)

    from tests.live_server import LiveServer

    server = LiveServer(module.app)
    server.start()
    yield server.base_url
    server.stop()


@pytest.fixture
def dashboard_live_server(tmp_db_path, fake_garmin_client, monkeypatch):
    """The `vitalforge-dashboard` app, served for real over HTTP for Playwright.

    See `weight_live_server` for why this avoids `initialized_db`/
    `dashboard_app_module`. The real lifespan also kicks off `scheduled_sync()`
    (a 90-day backfill against the fake Garmin client) as a background task —
    stubbed out here since it's irrelevant to a UI smoke test and only adds
    noise/latency.
    """
    module = import_service_module("vitalforge-dashboard.app")
    monkeypatch.setattr(module, "authenticate", lambda: None)

    async def _noop_scheduled_sync(lock):
        return None

    monkeypatch.setattr(module, "scheduled_sync", _noop_scheduled_sync)

    from tests.live_server import LiveServer

    server = LiveServer(module.app)
    server.start()
    yield server.base_url
    server.stop()
