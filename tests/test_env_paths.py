"""DB_PATH refuses a leading ``~`` at import (#70).

``DB_PATH=~/fitness.db`` used to create a literal ``./~/`` directory under the
cwd.  Expanding it instead would put the database in the container image's
ephemeral layer (HOME is /app there), which is worse than a loud failure, so
the value is refused at import with an error that names the variable but
never echoes its value.  ``shared.database.DB_PATH`` is read at import and the
suite patches the module attribute, so the import-time behaviour is checked in
a fresh interpreter with an allowlisted environment.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.env_paths import path_from_env, refuse_leading_tilde

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("value", ["~", "~/fitness.db", "~root/fitness.db", "~/data/../fitness.db"])
def test_a_leading_tilde_is_refused_naming_the_variable_not_the_value(value):
    with pytest.raises(ValueError) as refused:
        refuse_leading_tilde("DB_PATH", value)

    message = str(refused.value)
    assert "DB_PATH" in message
    assert value not in message.replace("'~'", "")


@pytest.mark.parametrize("value", ["/app/data/fitness.db", "data/fitness.db", "./fitness.db", "/srv/~/fitness.db"])
def test_other_values_pass_through_unchanged(value):
    assert refuse_leading_tilde("DB_PATH", value) == value


def test_the_tilde_message_does_not_claim_relative_paths_are_refused():
    """Relative paths are still accepted, so the message must not demand an
    absolute one."""
    with pytest.raises(ValueError) as refused:
        refuse_leading_tilde("DB_PATH", "~/fitness.db")

    assert "absolute" not in str(refused.value)


_DEFAULT = "/app/data/fitness.db"


@pytest.mark.parametrize("blank", ["", " ", "\t\n "])
def test_a_blank_value_means_unset(blank, monkeypatch):
    """compose's ``DB_PATH=${DB_PATH:-}`` passes an empty string, which used to
    crash-loop on an opaque "unable to open database file"."""
    monkeypatch.setenv("DB_PATH", blank)

    assert path_from_env("DB_PATH", _DEFAULT) == _DEFAULT


def test_path_from_env_reads_unset_set_and_refuses_tilde(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert path_from_env("DB_PATH", _DEFAULT) == _DEFAULT
    monkeypatch.setenv("DB_PATH", " /srv/vf.db")
    assert path_from_env("DB_PATH", _DEFAULT) == " /srv/vf.db", "a non-blank value is not stripped"
    monkeypatch.setenv("DB_PATH", "~/vf.db")
    with pytest.raises(ValueError, match="DB_PATH"):
        path_from_env("DB_PATH", _DEFAULT)


def _import_database(tmp_path: Path, db_path: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DB_PATH": db_path,
        "GARTH_TOKEN_DIR": str(tmp_path / "garth"),
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    (tmp_path / "home").mkdir()
    code = "import shared.database as d; print(d.DB_PATH)"
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_importing_the_database_with_a_tilde_db_path_fails_loudly(tmp_path):
    completed = _import_database(tmp_path, "~/vf-secret-name.db")

    assert completed.returncode != 0
    assert "DB_PATH" in completed.stderr
    assert "vf-secret-name" not in completed.stderr
    assert not (tmp_path / "~").exists(), "a literal ./~ directory was created"
    assert sorted(p.name for p in (tmp_path / "home").iterdir()) == []


def test_importing_the_database_with_an_ordinary_db_path_still_works(tmp_path):
    completed = _import_database(tmp_path, str(tmp_path / "vf-test.db"))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(tmp_path / "vf-test.db")


@pytest.mark.parametrize("blank", ["", "   "])
def test_importing_the_database_with_a_blank_db_path_uses_the_default(blank, tmp_path):
    """Import only reads the variable; nothing is opened, so the default
    (/app/data/...) is reported without being created."""
    completed = _import_database(tmp_path, blank)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "/app/data/fitness.db"
