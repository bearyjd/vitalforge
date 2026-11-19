"""The first-admin grant helper lives in its own module so that
shared.database and shared.migrations no longer import each other's
underscore-private names."""

import ast
from pathlib import Path

import pytest

from shared.database import get_db, get_primary_person_id, init_db

SHARED = Path(__file__).resolve().parent.parent / "shared"


def _private_names_imported_from(module_file: str, source_module: str) -> list[str]:
    tree = ast.parse((SHARED / module_file).read_text())
    return [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == source_module
        for alias in node.names
        if alias.name.startswith("_")
    ]


def test_migrations_imports_no_private_names_from_database():
    assert _private_names_imported_from("migrations.py", "shared.database") == []


def test_database_helper_not_redefined_in_database():
    tree = ast.parse((SHARED / "database.py").read_text())
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_grant_primary_person_to_first_admin" not in defined


@pytest.mark.asyncio
async def test_grant_is_idempotent_and_sets_default_person(tmp_path, monkeypatch):
    import shared.database as database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    await init_db()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES ('a', 'x', 'admin', '2026-01-01')"
        )
        await db.commit()
    finally:
        await db.close()

    from shared.person_grants import grant_primary_person_to_first_admin

    person_id = await get_primary_person_id()
    db = await get_db()
    try:
        await grant_primary_person_to_first_admin(db, person_id)
        await grant_primary_person_to_first_admin(db, person_id)
        await db.commit()
        rows = await (await db.execute(
            "SELECT access FROM person_grants WHERE person_id = ?", (person_id,)
        )).fetchall()
        default = await (await db.execute(
            "SELECT default_person_id FROM users WHERE username = 'a'"
        )).fetchone()
    finally:
        await db.close()
    assert [r["access"] for r in rows] == ["own"]
    assert default["default_person_id"] == person_id
