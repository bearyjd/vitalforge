"""Shared pytest fixtures for VitalForge.

Isolates every test from real infrastructure:
- SQLite DB lives in a per-test tmp_path, never `/app/data/fitness.db`.
- Garmin Connect is never contacted; `shared.garmin_client` is monkeypatched
  to a FakeGarminClient returning canned, synthetic responses.
- `vitalforge_weight` and `vitalforge_dashboard` resolve as namespace packages
  off the repo root (`pythonpath = ["."]` in pyproject); only `shared/` is
  pip-installed. Plain imports work. Fixtures that need a module *object* to
  patch import it inside the fixture body (`from vitalforge_weight import
  weight_routes`) and return it.
"""

import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ReadTimeout

from shared.garmin_client import STRENGTH_ACTIVITY_TYPE_KEY

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "garmin"
PRODUCTION_SCHEMA_SQL = Path(__file__).resolve().parent / "fixtures" / "production_schema.sql"

# The slug `init_db()` gives the primary person on a fresh database. It derives
# from VITALFORGE_PRIMARY_PERSON, then the first admin's username, then this
# literal (shared/migrations.py) -- and every test DB is created empty, before
# any user is seeded, so it is always this one.
PRIMARY_SLUG = "primary"
# Prefix for every person-scoped route: Phase 2 moved /api/... to
# /p/{slug}/api/.... Named rather than inlined so a slug change is one edit,
# and so a test that means "the primary person" says so.
PERSON_PREFIX = f"/p/{PRIMARY_SLUG}"

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
        self.created_activities = []
        self.pushed_exercise_sets = []
        self.activities_by_date = []
        self.activity_lookups = []

    def add_body_composition(self, timestamp, weight, **kwargs):
        self.pushed_weights.append({"timestamp": timestamp, "weight": weight, **kwargs})
        return {"success": True}

    def create_manual_activity(self, **kwargs):
        self.created_activities.append(kwargs)
        activity_id = 19283746501 + len(self.created_activities) - 1
        # A created activity becomes FINDABLE, the way a real one does. This
        # is what makes reconciliation testable at all: a fake that creates
        # activities but never returns them from a lookup would report "not
        # on Garmin" for something it just created, and every reconciliation
        # test would pass by pushing a duplicate.
        self.activities_by_date.append(
            {"activityId": activity_id, "activityName": kwargs.get("activity_name")}
        )
        return {"activityId": activity_id}

    def set_activity_exercise_sets(self, activity_id, payload):
        self.pushed_exercise_sets.append({"activity_id": activity_id, "payload": payload})
        return {"success": True}

    def get_activities_by_date(self, startdate, enddate=None, activitytype=None, sortorder=None):
        """Reconciliation lookup for an ambiguous push.

        Returns whatever a test put in `activities_by_date`, defaulting to
        empty -- "Garmin has no such activity", the answer that makes a
        re-push safe. A test proving reconciliation FINDS something appends
        a {"activityId": ..., "activityName": ...} dict to that list.
        """
        self.activity_lookups.append(
            {"startdate": startdate, "enddate": enddate, "activitytype": activitytype}
        )
        return list(self.activities_by_date)

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


async def seed_person(slug: str, display_name: str | None = None, is_primary: bool = False) -> int:
    """Insert a person row directly and return its id.

    `init_db()` already creates the primary person, so this is for the SECOND
    person onward -- the one that makes cross-person isolation testable at all.
    Pass is_primary=True only on a database that has none; the partial unique
    index idx_persons_primary allows exactly one.

    Deliberately does NOT create a person_grants row. Grant it explicitly with
    grant_person() so each test states the access level it is exercising --
    Phase 2's require_person returns 404, not 403, for a missing grant, so an
    implicit grant here would quietly turn every negative test positive.
    """
    from shared.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) VALUES (?, ?, ?, ?)",
            (
                slug,
                display_name or slug,
                datetime.now(timezone.utc).isoformat(),
                1 if is_primary else 0,
            ),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def grant_person(
    person_id: int, user_id: int, access: str = "own", granted_by: int | None = None
) -> None:
    """Give `user_id` `access` on `person_id`.

    access is one of 'view' | 'manage' | 'own' -- the CHECK constraint on
    person_grants rejects anything else, which is intentional: a typo'd level
    should fail loudly in the fixture rather than silently under-granting and
    producing a confusing 404 in the test body.
    """
    from shared.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO person_grants "
            "(person_id, user_id, access, granted_at, granted_by) VALUES (?, ?, ?, ?, ?)",
            (person_id, user_id, access, datetime.now(timezone.utc).isoformat(), granted_by),
        )
        await db.commit()
    finally:
        await db.close()


async def primary_person_id() -> int:
    """The person init_db() created. Thin wrapper so tests that only need the
    id do not each import from shared.database."""
    from shared.database import get_primary_person_id

    return await get_primary_person_id()


@pytest.fixture
def weight_app_module(initialized_db, fake_garmin_client, monkeypatch):
    """The `vitalforge_weight` FastAPI app module, Garmin/DB fully faked."""
    from vitalforge_weight import activity_garmin, weight_routes
    from vitalforge_weight import app as module

    # The Garmin helpers are bound into each module's own namespace via
    # `from shared.garmin_client import ...`, so patching the shared module alone
    # doesn't reach them -- patch the names the handlers actually call. Since the
    # app.py split those bindings live in three places, and patching only app.py
    # would leave the routes calling the real client while every test still passed.
    for m in (module, weight_routes, activity_garmin):
        if hasattr(m, "authenticate"):
            monkeypatch.setattr(m, "authenticate", lambda: None)

    def fake_push_weight(weight_grams, timestamp=None, **kwargs):
        fake_garmin_client.pushed_weights.append(
            {"weight_grams": weight_grams, "timestamp": timestamp, **kwargs}
        )

    monkeypatch.setattr(weight_routes, "push_weight", fake_push_weight)

    # Same direct-import situation for the activity push helpers: app.py does
    # `from shared.garmin_client import push_activity, push_activity_sets`, so
    # patching shared.garmin_client alone would leave the route calling the
    # real client. Both forward into the same FakeGarminClient the weight
    # fixture records against, so a test asserts on
    # `fake_garmin_client.created_activities` / `.pushed_exercise_sets`.
    def fake_push_activity(**kwargs):
        return fake_garmin_client.create_manual_activity(**kwargs)

    def fake_push_activity_sets(activity_id, payload):
        return fake_garmin_client.set_activity_exercise_sets(activity_id, payload)

    def fake_find_activities_by_date(start_date, end_date, activity_type=STRENGTH_ACTIVITY_TYPE_KEY):
        # Mirrors shared.garmin_client.find_activities_by_date's own default,
        # so a test can assert on the activity type the route actually asks
        # Garmin for rather than on this fake's signature.
        return fake_garmin_client.get_activities_by_date(start_date, end_date, activitytype=activity_type)

    monkeypatch.setattr(activity_garmin, "push_activity", fake_push_activity)
    monkeypatch.setattr(activity_garmin, "push_activity_sets", fake_push_activity_sets)
    monkeypatch.setattr(activity_garmin, "find_activities_by_date", fake_find_activities_by_date)
    return module


@pytest.fixture
def no_real_garmin_client(weight_app_module):
    """Fail loudly if a test could reach the real Garmin client.

    NOT autouse: opted into per module with
    `pytestmark = pytest.mark.usefixtures("no_real_garmin_client")`, so it
    costs nothing on the several hundred tests that never touch a push path.

    The failure it guards is silent by construction. app.py binds Garmin
    helpers into its own namespace with `from shared.garmin_client import
    ...`, so patching `shared.garmin_client` alone leaves the route calling
    the real function -- the test still passes, having quietly attempted a
    network call against a live account with the deployment's shared
    credential. Comparing identity against the real module attribute is what
    distinguishes "patched" from "patched somewhere that does not matter".
    """
    from shared import garmin_client
    from vitalforge_weight import activity_garmin, weight_routes

    # Checked per OWNING module. After the app.py split, `push_weight` lives in
    # weight_routes and the activity helpers in activity_garmin; asserting only
    # against app.py would pass while the routes called the real client.
    owners = {
        weight_routes: ("authenticate", "push_weight"),
        activity_garmin: ("authenticate", "push_activity", "push_activity_sets", "find_activities_by_date"),
    }
    for module, names in owners.items():
        dotted = module.__name__
        for name in names:
            assert hasattr(module, name), (
                f"{dotted} no longer binds {name} -- a Garmin helper moved and this guard "
                "was not updated with it, so nothing is checking that module any more"
            )
            assert getattr(module, name) is not getattr(garmin_client, name), (
                f"{dotted}.{name} is still the real shared.garmin_client function; "
                "patch the name in the owning module's namespace, not just the shared module"
            )
    assert isinstance(garmin_client._client, FakeGarminClient), (
        "shared.garmin_client._client is not a FakeGarminClient -- this test could reach real Garmin"
    )
    yield


@pytest.fixture
def dashboard_app_module(initialized_db, fake_garmin_client, monkeypatch):
    """The `vitalforge_dashboard` FastAPI app module, Garmin/DB fully faked."""
    from vitalforge_dashboard import app as module

    # Same direct-import situation as vitalforge_weight/app.py.
    monkeypatch.setattr(module, "authenticate", lambda: None)
    return module


@pytest.fixture
def weight_live_server(tmp_db_path, fake_garmin_client, monkeypatch):
    """The `vitalforge_weight` app, served for real over HTTP for Playwright.

    Deliberately does NOT depend on `initialized_db`/`weight_app_module`:
    both pull in an async fixture, and Playwright's sync API keeps its own
    event loop running in this (main) test thread for the whole session, so
    any `pytest-asyncio` fixture setup here collides with it (`RuntimeError:
    Runner.run() cannot be called from a running event loop`). Instead,
    `DB_PATH` is patched (via `tmp_db_path`, a plain sync fixture) and left
    for the live server's own `lifespan` to call `init_db()` inside its
    dedicated server thread, where no such conflict exists.
    """
    from vitalforge_weight import app as module
    from vitalforge_weight import weight_routes

    # Same owning-module rule as weight_app_module: after the app.py split the
    # Garmin bindings live in weight_routes / activity_garmin, so patching app.py
    # alone would leave the live server calling the real client.
    for m in (module, weight_routes):
        if hasattr(m, "authenticate"):
            monkeypatch.setattr(m, "authenticate", lambda: None)

    def fake_push_weight(weight_grams, timestamp=None, **kwargs):
        fake_garmin_client.pushed_weights.append({"weight_grams": weight_grams, "timestamp": timestamp, **kwargs})

    monkeypatch.setattr(weight_routes, "push_weight", fake_push_weight)

    from tests.live_server import LiveServer

    server = LiveServer(module.app)
    server.start()
    yield server.base_url
    server.stop()


@pytest.fixture
def dashboard_live_server(tmp_db_path, fake_garmin_client, monkeypatch):
    """The `vitalforge_dashboard` app, served for real over HTTP for Playwright.

    See `weight_live_server` for why this avoids `initialized_db`/
    `dashboard_app_module`. The real lifespan also kicks off `scheduled_sync()`
    (a 90-day backfill against the fake Garmin client) as a background task —
    stubbed out here since it's irrelevant to a UI smoke test and only adds
    noise/latency.
    """
    from vitalforge_dashboard import app as module

    monkeypatch.setattr(module, "authenticate", lambda: None)

    async def _noop_scheduled_sync(lock, registry):
        return None

    monkeypatch.setattr(module, "scheduled_sync", _noop_scheduled_sync)

    from tests.live_server import LiveServer

    server = LiveServer(module.app)
    server.start()
    yield server.base_url
    server.stop()


def timing_out_until(weight_app_module, monkeypatch):
    """Make the activity push time out, and return a switch that restores it.

    Deliberately NOT monkeypatch.undo(): that would also unwind the fixtures'
    patches of authenticate/push_activity/find_activities_by_date, leaving the
    route pointed at the REAL Garmin client for the rest of the test. A local
    toggle keeps the blast radius to this one behaviour.

    Lives here rather than in one test module because two modules need it --
    test_activity_unknown_outcome.py for the ambiguous-outcome matrix, and
    test_activity_garmin_guard.py to reach the same state before a rename.
    """
    from vitalforge_weight import activity_garmin

    state = {"failing": True}
    real = activity_garmin.push_activity

    def maybe_timing_out(**kwargs):
        if state["failing"]:
            raise ReadTimeout("timed out waiting for a response")
        return real(**kwargs)

    monkeypatch.setattr(activity_garmin, "push_activity", maybe_timing_out)
    return state


@pytest.fixture
def activity_garmin_module(weight_app_module):
    """The module that owns the activity Garmin bindings after the app.py split.

    Depends on weight_app_module so the fakes are already patched in. Tests that
    monkeypatch push_activity or _record_activity_garmin_outcome must target THIS
    module -- patching app.py would land on a binding no route reads.
    """
    from vitalforge_weight import activity_garmin

    return activity_garmin


@pytest.fixture
def weight_routes_module(weight_app_module):
    """The module that owns push_weight after the app.py split. See above."""
    from vitalforge_weight import weight_routes

    return weight_routes


@pytest.fixture
def garmin_claim_module(weight_app_module):
    """The module that owns the Garmin claim timeout after the app.py split."""
    from vitalforge_weight import garmin_claim

    return garmin_claim


@pytest.fixture
def activity_routes_module(weight_app_module):
    """The module whose route bodies CALL the activity helpers.

    Distinct from activity_garmin_module on purpose. activity_routes imports
    `_record_activity_garmin_outcome` and friends BY VALUE, so patching them on
    activity_garmin (where they are defined) does not reach the binding the route
    actually calls. Patch the caller, not the definer.
    """
    from vitalforge_weight import activity_routes

    return activity_routes


@pytest.fixture
def dashboard_export_module(dashboard_app_module):
    """The module that owns the export route's `get_db` binding after the split.

    Same owning-module rule as the weight service: export_routes imports get_db
    into its own namespace, so patching it on app.py reaches a binding the route
    does not read.
    """
    from vitalforge_dashboard import export_routes

    return export_routes


@pytest.fixture
def dashboard_fit_module(dashboard_app_module):
    """The module that owns the `fit_import` binding after the split."""
    from vitalforge_dashboard import fit_activity_routes

    return fit_activity_routes
