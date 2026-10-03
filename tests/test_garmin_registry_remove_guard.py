"""`_remove_token_dir` is the last-line guard before an `rmtree` under the token
root: a stage it accepts must be a `.person-...staging` directory, never the
`.person-<id>.lock` file that sits beside it."""

import pytest

from shared import garmin_registry


@pytest.fixture
def root(monkeypatch, tmp_path):
    root = tmp_path / "garth"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(garmin_registry, "GARTH_TOKEN_DIR", root)
    return root


def test_a_person_lock_file_is_refused_as_a_stage(root):
    lock_file = root / ".person-1.lock"
    lock_file.touch()

    with pytest.raises(RuntimeError, match="refusing unsafe Garmin token cleanup"):
        garmin_registry._remove_token_dir(lock_file)
    assert lock_file.is_file()


def test_a_real_staging_directory_is_removed(root):
    staging = garmin_registry._staging_token_dir(1, 3)
    (staging / "garmin_tokens.json").write_text("{}", encoding="ascii")

    garmin_registry._remove_token_dir(staging)
    assert not staging.exists()
