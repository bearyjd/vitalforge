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

from shared.env_paths import refuse_leading_tilde

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
