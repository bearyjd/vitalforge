"""The person/grant test fixtures Phase 2 is built on.

These exist one PR ahead of require_person because the whole suite depends on
them: tests/conftest.py's seed_user() creates a user with NO person_grants row,
so once require_person lands, every seeded user 404s on every person-scoped
route until each test grants access explicitly. Getting the fixtures wrong is
therefore not a local failure -- it is a suite-wide one, and it fails in the
direction that looks like a product bug.

They are tested rather than trusted for one specific reason: require_person
returns 404, not 403, for a missing grant (spec f.1:1634, so a 403 cannot
confirm a person exists). A fixture that silently over-granted would turn every
negative isolation test green while proving nothing.
"""

import pytest

from tests.conftest import grant_person, primary_person_id, seed_person, seed_user


async def test_seed_person_creates_a_distinct_non_primary_person(initialized_db):
    primary = await primary_person_id()
    other = await seed_person("bryn")

    assert other != primary, "seed_person handed back the primary person"

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT slug, display_name, is_primary FROM persons WHERE id = ?", (other,))
        ).fetchone()
        assert row["slug"] == "bryn"
        assert row["display_name"] == "bryn", "display_name should default to the slug"
        assert row["is_primary"] == 0, "a seeded person must never claim is_primary"

        count = await (await db.execute("SELECT COUNT(*) FROM persons WHERE is_primary = 1")).fetchone()
        assert count[0] == 1, "there must still be exactly one primary person"
    finally:
        await db.close()


async def test_seed_person_takes_an_explicit_display_name(initialized_db):
    person_id = await seed_person("jd", display_name="JD")

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT display_name FROM persons WHERE id = ?", (person_id,))
        ).fetchone()
        assert row["display_name"] == "JD"
    finally:
        await db.close()


async def test_seed_person_grants_nothing_by_default(initialized_db):
    """The load-bearing property. An implicit grant here would make every
    'user without access gets 404' test pass for the wrong reason."""
    user_id = await seed_user("nobody")
    person_id = await seed_person("bryn")

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM person_grants WHERE person_id = ? AND user_id = ?",
                (person_id, user_id),
            )
        ).fetchone()
        assert row[0] == 0, "seed_person created a grant it was not asked for"
    finally:
        await db.close()


@pytest.mark.parametrize("access", ["view", "manage", "own"])
async def test_grant_person_records_each_access_level(initialized_db, access):
    user_id = await seed_user(f"user-{access}")
    person_id = await seed_person(f"person-{access}")
    await grant_person(person_id, user_id, access=access)

    from shared.database import get_db

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT access FROM person_grants WHERE person_id = ? AND user_id = ?",
                (person_id, user_id),
            )
        ).fetchone()
        assert row["access"] == access
    finally:
        await db.close()


async def test_grant_person_rejects_an_invalid_access_level(initialized_db):
    """The CHECK constraint must fail loudly. A typo'd level that slipped
    through would under-grant and surface as a confusing 404 in the test body
    rather than an error in the fixture."""
    import aiosqlite

    user_id = await seed_user("typo")
    person_id = await seed_person("bryn")
    with pytest.raises(aiosqlite.IntegrityError):
        await grant_person(person_id, user_id, access="admin")


async def test_grant_person_is_idempotent(initialized_db):
    """Both services' lifespans race the same grant logic at startup, and
    tests re-grant freely; a second call must update, not raise."""
    user_id = await seed_user("repeat")
    person_id = await seed_person("bryn")
    await grant_person(person_id, user_id, access="view")
    await grant_person(person_id, user_id, access="own")

    from shared.database import get_db

    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT access FROM person_grants WHERE person_id = ? AND user_id = ?",
                (person_id, user_id),
            )
        ).fetchall()
        assert len(rows) == 1, "re-granting created a duplicate row"
        assert rows[0]["access"] == "own", "re-granting did not upgrade the level"
    finally:
        await db.close()


async def test_two_seeded_persons_hold_independent_metric_rows(initialized_db):
    """The property every Phase 2 isolation test rests on: same date, two
    persons, two rows -- which only works because migration 001 re-keyed these
    tables on (person_id, date) rather than date alone."""
    primary = await primary_person_id()
    other = await seed_person("bryn")

    from shared.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO steps (person_id, date, value) VALUES (?, '2026-03-01', 1111)", (primary,)
        )
        await db.execute(
            "INSERT INTO steps (person_id, date, value) VALUES (?, '2026-03-01', 2222)", (other,)
        )
        await db.commit()

        a = await (
            await db.execute("SELECT value FROM steps WHERE person_id = ? AND date = '2026-03-01'", (primary,))
        ).fetchone()
        b = await (
            await db.execute("SELECT value FROM steps WHERE person_id = ? AND date = '2026-03-01'", (other,))
        ).fetchone()
        assert (a["value"], b["value"]) == (1111, 2222), "same-date rows collided across persons"
    finally:
        await db.close()
