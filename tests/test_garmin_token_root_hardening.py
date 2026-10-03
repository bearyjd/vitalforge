"""Token-root hardening deferred from #80 (issue #70, batch 4).

``tests/test_garmin_token_root.py`` pins the normalizer #80 shipped; this file
pins what was layered on it afterwards: roots that would mutate a shared
directory, the probe of the names garth actually receives, the archive route
with Garmin disabled, ``DB_PATH``'s ``~`` refusal and the advisory ancestor
warning.  Like the sibling file, HOME and the cwd live in ``tmp_path`` so a
``~`` or ``.`` root never lands anywhere real.
"""

import logging
from pathlib import Path

import pytest

from shared import garmin_registry, garmin_registry_common

_REGISTRY_LOGGER = "shared.garmin_registry"
_UNUSABLE = "GARTH_TOKEN_DIR is unusable ({}); Garmin features are disabled"
_PROD_ROOT = Path("/app/data/.garth")


@pytest.fixture(autouse=True)
def _isolated_home_and_cwd(tmp_db_path, monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "cwd")


def _normalize(raw) -> Path:
    return garmin_registry_common.normalize_token_root(raw)


def _registry_records(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _REGISTRY_LOGGER and r.levelno == level]


# -- item 1: surprising roots ---------------------------------------------------


def test_prod_default_root_is_unchanged(monkeypatch, caplog):
    """Prod leaves GARTH_TOKEN_DIR unset; under compose HOME and the cwd are
    both /app.  None of the new refusals may touch that root."""
    monkeypatch.delenv("GARTH_TOKEN_DIR", raising=False)
    monkeypatch.setenv("HOME", "/app")
    caplog.set_level(logging.INFO)

    assert garmin_registry._configured_token_root() == _PROD_ROOT
    assert _registry_records(caplog, logging.ERROR) == []
    assert _normalize("/app/data/.garth") == _PROD_ROOT
    assert _normalize("/app/data/garth-tokens") == Path("/app/data/garth-tokens")


@pytest.mark.parametrize("blank", ["", " ", "\t\n "])
def test_a_blank_root_is_treated_as_unset(blank, monkeypatch, caplog):
    """``GARTH_TOKEN_DIR=${GARTH_TOKEN_DIR:-}`` in compose passes an empty
    string, which used to mean the cwd: it now means the default, quietly."""
    monkeypatch.setenv("GARTH_TOKEN_DIR", blank)
    caplog.set_level(logging.INFO)

    assert garmin_registry._configured_token_root() == _PROD_ROOT
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def _shared_directory(shape: str, tmp_path: Path) -> tuple[str, str]:
    """(configured value, the fixed reason it is refused for)."""
    if shape == "filesystem-root":
        return "/", "the filesystem root"
    if shape == "bare-tilde":
        return "~", "the home directory"
    if shape == "home-by-absolute-path":
        return str(tmp_path / "home"), "the home directory"
    if shape == "dot":
        return ".", "the working directory"
    return str(tmp_path / "cwd"), "the working directory"


_SHARED_SHAPES = ["filesystem-root", "bare-tilde", "home-by-absolute-path", "dot", "cwd-by-absolute-path"]


@pytest.mark.parametrize("shape", _SHARED_SHAPES)
def test_a_root_that_is_a_shared_directory_is_refused(shape, tmp_path):
    """The registry chmods its root 0700 and keeps its lock files there:
    ``/``, $HOME or the cwd (the image's /app) must never be that root."""
    configured, reason = _shared_directory(shape, tmp_path)

    with pytest.raises(garmin_registry_common.TokenRootRefused, match=f"^{reason}$"):
        _normalize(configured)


@pytest.mark.parametrize("shape", _SHARED_SHAPES)
def test_a_shared_directory_root_fails_closed_at_boot(shape, monkeypatch, tmp_path, caplog):
    configured, reason = _shared_directory(shape, tmp_path)
    monkeypatch.setenv("GARTH_TOKEN_DIR", configured)
    caplog.set_level(logging.ERROR)

    assert garmin_registry._configured_token_root() is None
    assert _registry_records(caplog, logging.ERROR) == [_UNUSABLE.format(reason)]
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("/vitalforge-token-root-test-not-created", Path("/vitalforge-token-root-test-not-created")),
        ("~/garth", "home/garth"),
        ("./garth", "cwd/garth"),
        ("garth", "cwd/garth"),
    ],
)
def test_a_subdirectory_of_a_shared_directory_is_accepted(configured, expected, tmp_path):
    expected = expected if isinstance(expected, Path) else tmp_path / expected

    assert _normalize(configured) == expected
    assert not expected.exists(), "normalizing must not create the root"


def test_the_home_refusal_compares_resolved_paths(monkeypatch, tmp_path):
    """Silverblue's /home -> /var/home: a root naming home's real path must be
    refused even though HOME spells it through a symlink, and vice versa."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (tmp_path / "linked-home").symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path / "linked-home"))

    for configured in (real_home, tmp_path / "linked-home", "~"):
        with pytest.raises(garmin_registry_common.TokenRootRefused, match="^the home directory$"):
            _normalize(configured)
    assert _normalize("~/garth") == real_home / "garth"


def test_an_unresolvable_home_or_cwd_is_not_a_refusal(monkeypatch, tmp_path):
    """A container with no resolvable HOME, or a deleted cwd, must not newly
    disable Garmin for an ordinary absolute root."""
    monkeypatch.delenv("HOME")
    monkeypatch.setattr(garmin_registry_common.Path, "home", _raise(RuntimeError("no home")))
    monkeypatch.setattr(garmin_registry_common.os, "getcwd", _raise(FileNotFoundError("cwd gone")))

    assert _normalize(tmp_path / "garth") == tmp_path / "garth"


def _raise(exc: BaseException):
    def raiser(*_args, **_kwargs):
        raise exc

    return raiser
