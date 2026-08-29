"""Coverage for scripts/seed_db.py.

This file exists because its absence had a cost. Migration 001 re-keyed every
metric table on (person_id, date) NOT NULL, and `shared/`, both services and
`tests/` were all updated -- but scripts/seed_db.py still built its INSERT as
`["date"] + columns`, so it raised

    IntegrityError: NOT NULL constraint failed: steps.person_id

on the first row and seeded nothing. Two rounds of code review missed it
because nothing in the suite ever executed that file. seed_db.py is the
documented way to get dashboard data without a live Garmin account, so it
being broken is not cosmetic.

Run as a SUBPROCESS, not imported: seed() sets os.environ["DB_PATH"] and
rebinds shared.database.DB_PATH at module scope, which would leak into every
test that ran afterward. The subprocess also exercises what actually broke --
the CLI, as a person would invoke it.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_db.py"


def _run_seed(*args: str) -> subprocess.CompletedProcess:
    """Run the seeder with a sanitized environment.

    VITALFORGE_PRIMARY_PERSON must be cleared: shared/migrations.py reads it
    when naming the primary person, and README documents it as a .env setting.
    A developer who has it exported (direnv, `set -a; source .env`) would
    otherwise see these tests fail against a perfectly working seeder -- the
    primary person's slug would be their value instead of "primary", and if it
    happened to be "bryn" the second-person test would find the existing
    primary by slug and create nobody. CI never has a .env, so this would bite
    locally only, which is the worst place for a spurious failure.
    """
    env = {k: v for k, v in os.environ.items() if k != "VITALFORGE_PRIMARY_PERSON"}
    return subprocess.run(
        [sys.executable, str(SEED_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        env=env,
    )


def test_seed_script_runs_against_the_person_scoped_schema(tmp_path):
    db_path = tmp_path / "seeded.db"
    result = _run_seed("--db-path", str(db_path), "--days", "3")

    assert result.returncode == 0, (
        f"seed_db.py failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM sleep WHERE person_id IS NULL").fetchone()[0] == 0
        person_id = conn.execute("SELECT id FROM persons WHERE is_primary = 1").fetchone()[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM steps WHERE person_id = ?", (person_id,)
        ).fetchone()[0] == 3, "rows were not attributed to the primary person"
    finally:
        conn.close()


def test_seed_script_can_seed_a_second_person(tmp_path):
    """Phase 2 cannot test cross-person isolation at all without this."""
    db_path = tmp_path / "seeded.db"
    assert _run_seed("--db-path", str(db_path), "--days", "2").returncode == 0
    result = _run_seed("--db-path", str(db_path), "--days", "2", "--person", "bryn")
    assert result.returncode == 0, f"stderr: {result.stderr}"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        people = conn.execute("SELECT id, slug, is_primary FROM persons ORDER BY id").fetchall()
        assert len(people) == 2
        assert [p["slug"] for p in people] == ["primary", "bryn"]
        assert [p["is_primary"] for p in people] == [1, 0], "the second person claimed is_primary"

        for p in people:
            n = conn.execute("SELECT COUNT(*) FROM sleep WHERE person_id = ?", (p["id"],)).fetchone()[0]
            assert n == 2, f"person {p['slug']} got {n} rows, expected 2"
        assert conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 4, (
            "two persons' same-date rows collided instead of coexisting"
        )
    finally:
        conn.close()


def test_seed_script_normalizes_a_display_name_into_a_slug(tmp_path):
    db_path = tmp_path / "seeded.db"
    assert _run_seed("--db-path", str(db_path), "--days", "1").returncode == 0
    assert _run_seed("--db-path", str(db_path), "--days", "1", "--person", "Bryn Jones").returncode == 0

    conn = sqlite3.connect(db_path)
    try:
        slugs = [r[0] for r in conn.execute("SELECT slug FROM persons ORDER BY id")]
        assert "bryn-jones" in slugs, f"slug not normalized, got {slugs}"
    finally:
        conn.close()


@pytest.mark.parametrize("reserved", ["api", "auth", "health", "admin"])
def test_seed_script_refuses_a_reserved_slug(tmp_path, reserved):
    """A person at /p/api/ would shadow a real path segment."""
    db_path = tmp_path / "seeded.db"
    assert _run_seed("--db-path", str(db_path), "--days", "1").returncode == 0
    result = _run_seed("--db-path", str(db_path), "--days", "1", "--person", reserved)

    assert result.returncode != 0, f"reserved slug {reserved!r} was accepted"
    assert "not a usable slug" in (result.stdout + result.stderr)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM persons WHERE slug = ?", (reserved,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_seed_script_still_refuses_a_real_looking_data_path(tmp_path):
    """Pre-existing guard, pinned here because this file now has the only
    coverage of the script's CLI."""
    result = _run_seed("--db-path", str(tmp_path / "fitness.db"), "--days", "1")
    assert result.returncode != 0
    assert "looks like a real data path" in (result.stdout + result.stderr)


def test_seeding_the_same_person_twice_is_idempotent(tmp_path):
    """INSERT OR REPLACE on (person_id, date) -- a re-run must refresh rows,
    not duplicate them or create a second person."""
    db_path = tmp_path / "seeded.db"
    assert _run_seed("--db-path", str(db_path), "--days", "4", "--person", "bryn").returncode == 0
    assert _run_seed("--db-path", str(db_path), "--days", "4", "--person", "bryn").returncode == 0

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM persons WHERE slug = 'bryn'").fetchone()[0] == 1
        person_id = conn.execute("SELECT id FROM persons WHERE slug = 'bryn'").fetchone()[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM sleep WHERE person_id = ?", (person_id,)
        ).fetchone()[0] == 4
    finally:
        conn.close()
