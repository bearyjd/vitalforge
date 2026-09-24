"""GARTH_TOKEN_DIR must name the directory garminconnect itself uses (#73).

garminconnect's ``token_file_path`` expands ``~`` and refuses a token path with
any symlinked ancestor, while the registry used the configured root literally.
A ``~`` root therefore split the registry's tree (a literal ``./~``) from
garth's (``$HOME``) and published an empty durable store; a symlinked ancestor
(macOS ``/tmp``, Silverblue ``/home``) failed every login, every content check
and the one-time adoption.  pytest's ``tmp_path`` is realpath'd, so the rest of
the suite cannot see either shape: these tests build them on purpose.

The fake here replaces ``garminconnect.Garmin`` only, so the real
``garmin_client.authenticate`` stays in the path, and it persists and resumes
THROUGH ``token_file_path`` exactly as garth does.  A fake that writes
``token_dir / "garmin_tokens.json"`` literally passes on the broken code.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from garminconnect import GarminConnectAuthenticationError
from garminconnect.client import token_file_path

from shared import garmin_client, garmin_registry, garmin_registry_legacy
from shared.database import get_db, get_primary_person_id
from tests.conftest import seed_user

REPO = Path(__file__).resolve().parent.parent
_PROBE_LOGGER = "shared.garmin_registry"


class _GarthFaithfulGarmin:
    """Stand-in for ``garminconnect.Garmin`` that touches disk the way garth does.

    A credential login dumps through ``token_file_path`` (``~`` expanded, a
    symlinked ancestor refused); a resume (no password) reads through it and
    fails like the real client on a missing or empty store.
    """

    def __init__(self, email=None, password=None):
        self.email = email
        self.password = password

    def login(self, tokenstore=None):
        target = token_file_path(tokenstore)
        if self.password is not None:
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.write_text("{}", encoding="ascii")
        elif not target.is_file() or target.stat().st_size == 0:
            raise GarminConnectAuthenticationError("Username and password are required")
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
    garmin_client._clients.clear()
    yield
    garmin_client._clients.clear()


def _symlinked_root(tmp_path: Path) -> tuple[Path, Path]:
    """``<tmp>/link -> <tmp>/real``: returns (configured root, the real root)."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    return tmp_path / "link" / "garth", real / "garth"


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


def _probe_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _PROBE_LOGGER and r.levelno == logging.WARNING]


def _assert_one_path_free_warning(caplog, tmp_path: Path) -> None:
    warnings = _probe_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert str(tmp_path) not in warnings[0].getMessage(), "the probe warning must not name the path"
    assert str(tmp_path) not in caplog.text, "no log line may carry the token path"


def _refuse(path):
    raise ValueError(f"Token path must not be a symlink: {path!r}")


def test_tilde_root_is_expanded_to_the_directory_garth_uses(monkeypatch, tmp_path):
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", Path("~/garth"))
    home_root = tmp_path / "home" / "garth"

    assert garmin_registry._ensure_token_root() == home_root
    token_dir = garmin_registry.resolve_token_dir(1, 1)
    assert token_dir == home_root / "person-1" / "generation-1"
    assert token_file_path(str(token_dir)).parent == token_dir
    assert not (tmp_path / "cwd" / "~").exists(), "the registry built a literal ~ tree in the cwd"


def test_symlinked_ancestor_root_is_resolved(monkeypatch, tmp_path):
    configured, real_root = _symlinked_root(tmp_path)
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", configured)
    # Built from registry output, never from the patched attribute:
    # _remove_token_dir compares against the normalized root.
    token_dir = garmin_registry.resolve_token_dir(1, 1)
    garmin_client._ensure_token_dir(token_dir)
    (token_dir / "garmin_tokens.json").write_text("{}", encoding="ascii")

    assert garmin_registry._token_store_has_content(token_dir) is True
    assert token_file_path(str(token_dir)) == token_dir / "garmin_tokens.json"
    assert garmin_registry._ensure_token_root() == real_root


async def _assert_link_publishes_a_resumable_store(monkeypatch, configured_root: Path) -> Path:
    """Link through the real adapter; return the durable token file garth resumes from."""
    person_id = await get_primary_person_id()
    actor_id = await seed_user("token-root-actor", role="admin")
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", configured_root)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)

    result = await garmin_registry.link(person_id, actor_id, 1, "owner@example.test", "transient-password")

    assert result == garmin_registry.GarminLink(person_id, 1, "linked")
    assert tuple(await _link_row(person_id)) == ("linked", "owner@example.test", 1)
    durable_token = token_file_path(str(garmin_registry.resolve_token_dir(person_id, 1)))
    assert durable_token.is_file() and durable_token.stat().st_size > 0, (
        "the link was published on an empty durable store; the first cold call() cannot resume it"
    )
    return durable_token


async def test_link_through_a_tilde_root_publishes_the_store_garth_dumped(initialized_db, monkeypatch, tmp_path):
    home_root = tmp_path / "home" / "garth"

    durable_token = await _assert_link_publishes_a_resumable_store(monkeypatch, Path("~/garth"))

    assert durable_token.is_relative_to(home_root)
    assert not list(home_root.glob("*.staging")), "a garth-dumped token was left in a tree nothing sweeps"
    assert not (tmp_path / "cwd" / "~").exists(), "the registry built a literal ~ tree in the cwd"


async def test_link_through_a_symlinked_ancestor_root_publishes_a_resumable_store(
    initialized_db, monkeypatch, tmp_path
):
    configured, real_root = _symlinked_root(tmp_path)

    durable_token = await _assert_link_publishes_a_resumable_store(monkeypatch, configured)

    assert durable_token.is_relative_to(real_root)
    assert not list(real_root.glob("*.staging"))


async def test_legacy_adoption_through_a_symlinked_ancestor_root(initialized_db, monkeypatch, tmp_path, caplog):
    configured, real_root = _symlinked_root(tmp_path)
    configured.mkdir(mode=0o700)
    (configured / "garmin_tokens.json").write_text("{}", encoding="ascii")
    person_id = await get_primary_person_id()
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", configured)
    monkeypatch.setattr(garmin_registry.time, "time", lambda: 100.0)
    monkeypatch.setenv("GARMIN_EMAIL", f"person-{person_id}@example.test")
    caplog.set_level(logging.INFO, logger="shared.garmin_registry_legacy")

    assert await garmin_registry_legacy.bootstrap_legacy_token_store() is True, caplog.text

    durable_token = token_file_path(str(garmin_registry.resolve_token_dir(person_id, 1)))
    assert durable_token.read_text(encoding="ascii") == "{}"
    assert durable_token.is_relative_to(real_root)
    assert not (real_root / "garmin_tokens.json").exists(), "the flat store must be moved, not copied"
    assert tuple(await _link_row(person_id)) == ("linked", f"person-{person_id}@example.test", 1)


def test_a_symlink_below_the_root_is_still_refused(monkeypatch, tmp_path):
    """Only the configured root is resolved.  Everything under it is
    registry-created, so a symlink there is a planted redirect and must stay
    under garminconnect's refusal -- resolving deeper would follow it."""
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "garmin_tokens.json").write_text("{}", encoding="ascii")
    person_root = garmin_registry._person_token_root(1)
    person_root.mkdir(mode=0o700)
    (person_root / "generation-1").symlink_to(elsewhere, target_is_directory=True)

    redirected = garmin_registry.resolve_token_dir(1, 1)
    assert garmin_registry._token_store_has_content(redirected) is False
    assert garmin_registry_legacy._looks_like_legacy_token_store(redirected) is False

    real_generation = garmin_registry.resolve_token_dir(2, 1)
    garmin_client._ensure_token_dir(real_generation)
    (real_generation / "garmin_tokens.json").symlink_to(elsewhere / "garmin_tokens.json")
    assert garmin_registry._token_store_has_content(real_generation) is False


def _tree_listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if "__pycache__" not in p.parts)


def test_import_normalizes_a_tilde_root_from_the_environment(tmp_path):
    """The env boundary itself: a fresh interpreter with ``GARTH_TOKEN_DIR=~/garth``
    (what docker-compose's ``env_file`` passes through unexpanded) must hold the
    expanded root, and importing must still write nothing anywhere."""
    home = tmp_path / "fresh-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DB_PATH": str(home / "vf-test.db"),
        "GARTH_TOKEN_DIR": "~/garth",
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    shared_before = _tree_listing(REPO / "shared")
    top_before = sorted(p.name for p in REPO.iterdir())

    completed = subprocess.run(
        [sys.executable, "-c", "from shared import garmin_registry; print(garmin_registry.GARTH_TOKEN_DIR)"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(home / "garth")
    assert sorted(home.iterdir()) == [], "importing the registry created files under HOME"
    assert _tree_listing(REPO / "shared") == shared_before, "importing the registry wrote into shared/"
    assert sorted(p.name for p in REPO.iterdir()) == top_before, "importing the registry wrote into the repo root"


def test_probe_warns_path_free_when_garminconnect_refuses_the_root(monkeypatch, tmp_path, caplog):
    probe = garmin_registry.check_token_root
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    monkeypatch.setattr(garmin_registry, "token_file_path", _refuse, raising=False)
    caplog.set_level(logging.WARNING)

    assert probe() is False
    _assert_one_path_free_warning(caplog, tmp_path)


def test_probe_warns_path_free_when_garminconnect_would_use_another_directory(monkeypatch, tmp_path, caplog):
    """The drift a future garminconnect could introduce without raising."""
    probe = garmin_registry.check_token_root
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    monkeypatch.setattr(
        garmin_registry,
        "token_file_path",
        lambda path: Path(path).parent / "elsewhere" / "garmin_tokens.json",
        raising=False,
    )
    caplog.set_level(logging.WARNING)

    assert probe() is False
    _assert_one_path_free_warning(caplog, tmp_path)


@pytest.mark.parametrize("shape", ["plain", "tilde", "symlinked-ancestor"])
def test_probe_accepts_a_root_garminconnect_will_use(shape, monkeypatch, tmp_path, caplog):
    probe = garmin_registry.check_token_root
    if shape == "plain":
        configured = tmp_path / "garth"
    elif shape == "tilde":
        configured = Path("~/garth")
    else:
        configured, _real_root = _symlinked_root(tmp_path)
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", configured)
    caplog.set_level(logging.WARNING)

    assert probe() is True
    assert _probe_warnings(caplog) == []


async def test_boot_runs_the_probe_before_the_garmin_email_early_return(
    initialized_db, monkeypatch, tmp_path, caplog
):
    """Every boot of both services must probe, including the ordinary one with
    no ``GARMIN_EMAIL`` -- that early return is where adoption usually stops."""
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", tmp_path / "garth")
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.setattr(garmin_registry, "token_file_path", _refuse, raising=False)
    caplog.set_level(logging.INFO)

    assert await garmin_registry_legacy.bootstrap_legacy_token_store() is False

    assert "GARMIN_EMAIL is not set" in caplog.text
    _assert_one_path_free_warning(caplog, tmp_path)
