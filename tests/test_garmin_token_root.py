"""GARTH_TOKEN_DIR must name the directory garminconnect itself uses (#73).

garminconnect's ``token_file_path`` expands ``~``, refuses ``~name`` and refuses
a token path with any symlinked ancestor, while the registry used the configured
root literally.  A ``~`` root therefore split the registry's tree (a literal
``./~``) from garth's (``$HOME``) and published an empty durable store; a
symlinked ancestor (macOS ``/tmp``, Silverblue ``/home``) failed every login,
every content check and the one-time adoption.  The root is now normalized
ONCE, at import (``garmin_registry_common.normalize_token_root``), and a value
that cannot be used fails Garmin closed instead of crashing both services.
pytest's ``tmp_path`` is realpath'd, so the rest of the suite cannot see these
shapes: these tests build them on purpose.

The fake replaces ``garminconnect.Garmin`` only, so the real
``garmin_client.authenticate`` stays in the path.  It dumps and resumes THROUGH
``token_file_path`` the way garth does, and swallows a refused dump the way
``Garmin.login`` does -- a fake that writes ``token_dir / "garmin_tokens.json"``
literally passes on the broken code.  It is still a fake: it accepts ``"{}"``
and does not open with ``O_NOFOLLOW``.  The kernel's ``protected_symlinks``
needs a second uid, so the foreign-owner refusal is exercised by making this
process's own symlinks look foreign.
"""

import contextlib
import itertools
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from garminconnect import GarminConnectAuthenticationError
from garminconnect.client import token_file_path
from httpx import ASGITransport, AsyncClient

from shared import garmin_client, garmin_registry, garmin_registry_common, garmin_registry_legacy
from shared.database import get_db, get_primary_person_id
from tests.conftest import PERSON_PREFIX, seed_user

REPO = Path(__file__).resolve().parent.parent
_REGISTRY_LOGGER = "shared.garmin_registry"
_UNUSABLE = "GARTH_TOKEN_DIR is unusable ({}); Garmin features are disabled"
_PROBE_WARNING = (
    "Garmin token root is not a directory garminconnect will use ({}); "
    "every Garmin login will fail until GARTH_TOKEN_DIR is fixed"
)
# Captured at import, before any test's fake_garmin_client swaps it out.
_REAL_CALL = garmin_registry.call


class _GarthFaithfulGarmin:
    """Stand-in for ``garminconnect.Garmin`` that touches disk the way garth does."""

    logins: list[str] = []  # replaced per test by the autouse fixture

    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password

    def login(self, tokenstore=None):
        if self.password is None:
            try:
                target = token_file_path(tokenstore)
                resumable = target.is_file() and target.stat().st_size > 0
            except (OSError, ValueError):
                resumable = False
            if not resumable:
                raise GarminConnectAuthenticationError("Username and password are required")
            self.logins.append("resume")
            return None, None
        self.logins.append("credential")
        # Garmin.login wraps its dump in suppress(Exception): a refused path
        # is a "successful" login that persisted nothing.
        with contextlib.suppress(Exception):
            target = token_file_path(tokenstore)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.write_text("{}", encoding="ascii")
        return None, None


@pytest.fixture(autouse=True)
def _isolated_home_and_garmin(tmp_db_path, monkeypatch, tmp_path):
    """Any test here may expand ``~``: HOME and the cwd live in ``tmp_path``,
    so nothing lands under the developer's home or the repo."""
    (tmp_path / "home").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(garmin_client, "Garmin", _GarthFaithfulGarmin)
    monkeypatch.setattr(_GarthFaithfulGarmin, "logins", [])
    garmin_client._clients.clear()
    yield
    garmin_client._clients.clear()


def _normalize(raw) -> Path:
    return garmin_registry_common.normalize_token_root(raw)


def _root_as_imported(monkeypatch, configured) -> Path:
    """Patch in the root import would hold for this environment value, via the
    same function import runs."""
    monkeypatch.setenv("GARTH_TOKEN_DIR", str(configured))
    root = garmin_registry._configured_token_root()
    assert root is not None
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    return root


def _symlinked_root(tmp_path: Path) -> tuple[Path, Path]:
    """``<tmp>/link -> <tmp>/real``: returns (configured root, the real root)."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    return tmp_path / "link" / "garth", real / "garth"


def _leaf_loop(tmp_path: Path) -> Path:
    """A root that is itself a looping symlink.  The component walk accepts it
    (it is a symlink this process owns); only a stat after realpath sees the loop."""
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    return loop


def _refused_root(shape: str, tmp_path: Path, monkeypatch) -> tuple[str, str]:
    """(configured value, the reason the boot ERROR must name)."""
    if shape == "symlink-loop":
        return str(_leaf_loop(tmp_path)), "a symlink loop"
    if shape == "other-users-home":
        return "~nosuchuser/garth", "another user's home"
    if shape == "foreign-symlink":
        monkeypatch.setattr(garmin_registry_common, "_trusted_symlink_owner", lambda uid: False)
        return str(_symlinked_root(tmp_path)[0]), "a symlink owned by another user"
    # Not one of the normalizer's own refusals: an OSError whose text carries
    # the path, which must never reach the log.
    regular = tmp_path / "regular-file"
    regular.write_text("", encoding="ascii")
    return str(regular / "garth"), "NotADirectoryError"


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


def _assert_one_path_free_record(caplog, tmp_path: Path, level: int, expected: str) -> None:
    records = [r.getMessage() for r in caplog.records if r.name == _REGISTRY_LOGGER and r.levelno == level]
    assert records == [expected]
    assert str(tmp_path) not in caplog.text, "no log line may carry the token path"


def _tree_listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if "__pycache__" not in p.parts)


def _import_registry_in_a_fresh_interpreter(home: Path, garth_token_dir: str) -> subprocess.CompletedProcess:
    """Import the facade with an allowlisted environment (never the developer's)
    and print the root it holds; the import must write nothing to the repo."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DB_PATH": str(home / "vf-test.db"),
        "GARTH_TOKEN_DIR": garth_token_dir,
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    shared_before = _tree_listing(REPO / "shared")
    top_before = sorted(p.name for p in REPO.iterdir())
    code = "import logging; logging.basicConfig(); from shared import garmin_registry; print(garmin_registry.GARTH_TOKEN_DIR)"

    completed = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True)

    assert _tree_listing(REPO / "shared") == shared_before, "importing the registry wrote into shared/"
    assert sorted(p.name for p in REPO.iterdir()) == top_before, "importing the registry wrote into the repo root"
    return completed


# -- the normalizer -----------------------------------------------------------


def test_tilde_is_expanded_to_the_directory_garth_uses(monkeypatch, tmp_path):
    home = tmp_path / "home"
    assert _normalize("~") == home
    assert _normalize("~/garth") == home / "garth"
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", _normalize("~/garth"))

    assert garmin_registry._ensure_token_root() == home / "garth"
    token_dir = garmin_registry.resolve_token_dir(1, 1)
    assert token_dir == home / "garth" / "person-1" / "generation-1"
    assert token_file_path(str(token_dir)).parent == token_dir
    assert not (tmp_path / "cwd" / "~").exists(), "the registry built a literal ~ tree in the cwd"


@pytest.mark.parametrize("configured", ["~nosuchuser/garth", "~root/garth", "~root"])
def test_another_users_home_is_refused_before_expansion(configured):
    """garminconnect refuses ``~name``; expanding it first would hand garth an
    ordinary absolute path it accepts.  ``~root`` exists, so the refusal is
    the pattern's, not a failed home lookup's."""
    with pytest.raises(ValueError, match=r"^another user's home$"):
        _normalize(configured)


def test_symlinked_ancestor_root_is_resolved(monkeypatch, tmp_path):
    configured, real_root = _symlinked_root(tmp_path)
    assert _normalize(configured) == real_root
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", _normalize(configured))
    token_dir = garmin_registry.resolve_token_dir(1, 1)
    garmin_client._ensure_token_dir(token_dir)
    (token_dir / "garmin_tokens.json").write_text("{}", encoding="ascii")

    assert garmin_registry._token_store_has_content(token_dir) is True
    assert token_file_path(str(token_dir)) == token_dir / "garmin_tokens.json"
    assert garmin_registry._ensure_token_root() == real_root


@pytest.mark.parametrize("where", ["ancestor", "root itself"])
def test_a_symlink_owned_by_another_user_is_refused(where, monkeypatch, tmp_path):
    """Resolving would launder a symlink another user planted at or above the
    root, which the kernel's protected_symlinks and garth's guard refused."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    configured, expected = (
        (tmp_path / "link" / "garth", real / "garth") if where == "ancestor" else (tmp_path / "link", real)
    )
    assert _normalize(configured) == expected, "a symlink this process owns is trusted"

    monkeypatch.setattr(garmin_registry_common, "_trusted_symlink_owner", lambda uid: False)

    with pytest.raises(ValueError, match=r"^a symlink owned by another user$"):
        _normalize(configured)
    assert _normalize(tmp_path / "plain" / "garth") == tmp_path / "plain" / "garth", "no symlink, nothing to distrust"


def test_only_root_and_this_process_own_a_trusted_symlink():
    trusted = garmin_registry_common._trusted_symlink_owner
    other = next(uid for uid in itertools.count(1) if uid != os.geteuid())

    assert trusted(0) and trusted(os.geteuid())
    assert not trusted(other)


def test_a_symlink_loop_is_refused_by_name(tmp_path):
    """On 3.12 ``Path.resolve()`` raises RuntimeError on a loop, path in the
    message; realpath does not, so the loop is found and named here."""
    loop = _leaf_loop(tmp_path)

    with pytest.raises(ValueError, match=r"^a symlink loop$"):
        _normalize(loop)
    with pytest.raises(ValueError, match=r"^a symlink loop$"):
        _normalize(loop / "garth")


@pytest.mark.parametrize("shape", ["symlink-loop", "other-users-home", "foreign-symlink", "under-a-regular-file"])
def test_an_unusable_root_fails_closed_with_a_path_free_error(shape, monkeypatch, tmp_path, caplog):
    configured, reason = _refused_root(shape, tmp_path, monkeypatch)
    monkeypatch.setenv("GARTH_TOKEN_DIR", configured)
    caplog.set_level(logging.ERROR)

    assert garmin_registry._configured_token_root() is None
    _assert_one_path_free_record(caplog, tmp_path, logging.ERROR, _UNUSABLE.format(reason))


def test_the_root_is_resolved_once_so_garth_refuses_a_post_boot_swap(monkeypatch, tmp_path):
    """Re-resolving per call would hand garth the swapped-in target as an
    ordinary path; resolved once, garth's own ancestor check still refuses it."""
    volume = tmp_path / "volume"
    volume.mkdir()
    boot_root = _normalize(volume / "garth")
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", boot_root)
    token_dir = garmin_registry.resolve_token_dir(1, 1)
    garmin_client._ensure_token_dir(token_dir)
    (token_dir / "garmin_tokens.json").write_text("{}", encoding="ascii")
    assert garmin_registry._token_store_has_content(token_dir) is True

    volume.rename(tmp_path / "volume.moved")
    volume.symlink_to(tmp_path / "volume.moved", target_is_directory=True)

    assert garmin_registry._ensure_token_root() == boot_root
    assert garmin_registry._token_store_has_content(garmin_registry.resolve_token_dir(1, 1)) is False


# -- the import boundary --------------------------------------------------------


def test_import_normalizes_a_tilde_root_from_the_environment(tmp_path):
    """``GARTH_TOKEN_DIR=~/garth`` (what docker-compose's ``env_file`` passes
    through unexpanded) must reach the facade expanded, and importing must
    still write nothing anywhere."""
    home = tmp_path / "fresh-home"
    home.mkdir()

    completed = _import_registry_in_a_fresh_interpreter(home, "~/garth")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(home / "garth")
    assert sorted(home.iterdir()) == [], "importing the registry created files under HOME"


@pytest.mark.parametrize("shape", ["symlink-loop", "other-users-home"])
def test_import_fails_closed_on_an_unusable_root(shape, monkeypatch, tmp_path):
    """Both services import the registry at startup: a Garmin-only
    misconfiguration must disable Garmin, not crash weight entry and the
    dashboard with it."""
    home = tmp_path / "fresh-home"
    home.mkdir()
    configured, reason = _refused_root(shape, home, monkeypatch)
    before = _tree_listing(home)

    completed = _import_registry_in_a_fresh_interpreter(home, configured)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "None"
    assert _UNUSABLE.format(reason) in completed.stderr
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert _tree_listing(home) == before


# -- end to end -----------------------------------------------------------------


async def _assert_link_publishes_a_resumable_store(monkeypatch, configured) -> Path:
    """Link through the real adapter, then cold-call; return the durable token file."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("token-root-actor", role="admin")
    _root_as_imported(monkeypatch, configured)
    clock = itertools.count(100.0, 10.0)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: next(clock))

    result = await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")

    assert result == garmin_registry.GarminLink(person_id, 1, "linked")
    assert tuple(await _link_row(person_id)) == ("linked", "owner@example.test", 1)
    durable_token = token_file_path(str(garmin_registry.resolve_token_dir(person_id, 1)))
    assert durable_token.is_file() and durable_token.stat().st_size > 0, (
        "the link was published on an empty durable store; the first cold call() cannot resume it"
    )
    # The issue's reported symptom: the first cold call() after the link.
    garmin_client._clients.clear()
    assert await garmin_registry.call(person_id, lambda client: client.email) == "owner@example.test"
    assert _GarthFaithfulGarmin.logins == ["credential", "resume"], "the cold call must resume, not log in again"
    return durable_token


async def test_link_through_a_tilde_root_resumes_from_the_store_garth_dumped(initialized_db, monkeypatch, tmp_path):
    home_root = tmp_path / "home" / "garth"

    durable_token = await _assert_link_publishes_a_resumable_store(monkeypatch, "~/garth")

    assert durable_token.is_relative_to(home_root)
    assert not list(home_root.glob("*.staging")), "a garth-dumped token was left in a tree nothing sweeps"
    assert not (tmp_path / "cwd" / "~").exists(), "the registry built a literal ~ tree in the cwd"


async def test_link_through_a_symlinked_ancestor_root_resumes_from_its_store(initialized_db, monkeypatch, tmp_path):
    configured, real_root = _symlinked_root(tmp_path)

    durable_token = await _assert_link_publishes_a_resumable_store(monkeypatch, configured)

    assert durable_token.is_relative_to(real_root)
    assert not list(real_root.glob("*.staging"))


async def test_legacy_adoption_through_a_symlinked_ancestor_root(initialized_db, monkeypatch, tmp_path, caplog):
    configured, real_root = _symlinked_root(tmp_path)
    configured.mkdir(mode=0o700)
    (configured / "garmin_tokens.json").write_text("{}", encoding="ascii")
    person_id = await get_primary_person_id()
    _root_as_imported(monkeypatch, configured)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")
    caplog.set_level(logging.INFO, logger="shared.garmin_registry_legacy")

    assert await garmin_registry_legacy.bootstrap_legacy_token_store() is True, caplog.text

    durable_token = token_file_path(str(garmin_registry.resolve_token_dir(person_id, 1)))
    assert durable_token.read_text(encoding="ascii") == "{}"
    assert durable_token.is_relative_to(real_root)
    assert not (real_root / "garmin_tokens.json").exists(), "the flat store must be moved, not copied"
    assert tuple(await _link_row(person_id)) == ("linked", f"person-{person_id}@example.test", 1)
    assert _GarthFaithfulGarmin.logins == ["resume"], "adoption verifies by resuming, never by a credential login"


async def test_an_unusable_root_degrades_a_weigh_in_to_a_local_save(weight_app_module, monkeypatch):
    """The fail-closed root reaches the routes as a bounded ``unknown``: the
    person flock's OSError is wrapped by call(), never a 500."""
    monkeypatch.setattr(garmin_registry, "call", _REAL_CALL)
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", None)
    transport = ASGITransport(app=weight_app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"{PERSON_PREFIX}/api/weight", json={"weight": 170.0, "unit": "lbs"})
        recent = await client.get(f"{PERSON_PREFIX}/api/weight/recent")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["synced_to_garmin"] is False
    assert body["garmin_error"] == "unknown"
    assert [entry["synced_to_garmin"] for entry in recent.json()] == [False]
    assert _GarthFaithfulGarmin.logins == []


# -- below the root: left to garminconnect ----------------------------------------


def test_a_symlink_below_the_root_is_still_refused(monkeypatch, tmp_path):
    """Only the configured root is resolved.  Everything under it is
    registry-created, so a symlink there is a planted redirect and must stay
    under garminconnect's refusal -- resolving deeper would follow it."""
    root = tmp_path / "garth"
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    elsewhere = tmp_path / "elsewhere" / "generation-1"
    elsewhere.mkdir(parents=True)
    (elsewhere / "garmin_tokens.json").write_text("{}", encoding="ascii")
    # A planted generation directory.
    garmin_registry._person_token_root(1).mkdir(mode=0o700)
    garmin_registry.resolve_token_dir(1, 1).symlink_to(elsewhere, target_is_directory=True)
    # A planted person directory holding a real-looking generation-1.
    garmin_registry._person_token_root(3).symlink_to(elsewhere.parent, target_is_directory=True)
    # A planted token file inside a real generation directory.
    real_generation = garmin_registry.resolve_token_dir(2, 1)
    garmin_client._ensure_token_dir(real_generation)
    (real_generation / "garmin_tokens.json").symlink_to(elsewhere / "garmin_tokens.json")

    for person_id in (1, 2, 3):
        planted = garmin_registry.resolve_token_dir(person_id, 1)
        assert garmin_registry._token_store_has_content(planted) is False, person_id
        assert garmin_registry_legacy._looks_like_legacy_token_store(planted) is False, person_id


# -- the boot probe -------------------------------------------------------------


@pytest.mark.parametrize("error", [ValueError, RuntimeError, PermissionError])
def test_probe_warns_path_free_when_garminconnect_refuses_the_root(error, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")

    def refuse(path):
        raise error(f"Token path must not be a symlink: {path!r}")

    monkeypatch.setattr(garmin_registry, "token_file_path", refuse)
    caplog.set_level(logging.WARNING)

    assert garmin_registry.check_token_root() is False
    _assert_one_path_free_record(caplog, tmp_path, logging.WARNING, _PROBE_WARNING.format(error.__name__))


def test_probe_warns_path_free_when_garminconnect_would_use_another_directory(monkeypatch, tmp_path, caplog):
    """The drift a future garminconnect could introduce without raising."""
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    monkeypatch.setattr(
        garmin_registry, "token_file_path", lambda path: Path(path).parent / "elsewhere" / "garmin_tokens.json"
    )
    caplog.set_level(logging.WARNING)

    assert garmin_registry.check_token_root() is False
    _assert_one_path_free_record(caplog, tmp_path, logging.WARNING, _PROBE_WARNING.format("another directory"))


def test_probe_warns_when_the_root_was_refused_at_import(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", None)
    caplog.set_level(logging.WARNING)

    assert garmin_registry.check_token_root() is False
    _assert_one_path_free_record(caplog, tmp_path, logging.WARNING, _PROBE_WARNING.format("OSError"))


@pytest.mark.parametrize("shape", ["plain", "tilde", "symlinked-ancestor"])
def test_probe_accepts_a_root_garminconnect_will_use(shape, monkeypatch, tmp_path, caplog):
    if shape == "plain":
        configured = tmp_path / "garth"
    elif shape == "tilde":
        configured = "~/garth"
    else:
        configured, _real_root = _symlinked_root(tmp_path)
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", _normalize(configured))
    caplog.set_level(logging.WARNING)

    assert garmin_registry.check_token_root() is True
    assert [r for r in caplog.records if r.name == _REGISTRY_LOGGER] == []


async def test_boot_runs_the_probe_before_the_garmin_email_early_return(
    initialized_db, monkeypatch, tmp_path, caplog
):
    """Every boot of both services must probe, including the ordinary one with
    no ``GARMIN_EMAIL`` -- that early return is where adoption usually stops."""
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)

    def refuse(path):
        raise ValueError(f"Token path must not be a symlink: {path!r}")

    monkeypatch.setattr(garmin_registry, "token_file_path", refuse)
    caplog.set_level(logging.INFO)

    assert await garmin_registry_legacy.bootstrap_legacy_token_store() is False

    assert "GARMIN_EMAIL is not set" in caplog.text
    _assert_one_path_free_record(caplog, tmp_path, logging.WARNING, _PROBE_WARNING.format("ValueError"))
