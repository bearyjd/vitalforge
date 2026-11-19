"""Grant helper shared by shared.database and shared.migrations.

Lives in its own module so neither of those imports the other's private names.
"""

from datetime import datetime, timezone


async def grant_primary_person_to_first_admin(db, person_id: int) -> None:
    """Give the first admin an 'own' grant on `person_id` and make it their
    default. Runs on the caller's connection and does not commit.

    Idempotent by constraint rather than by pre-check: person_grants'
    PRIMARY KEY (person_id, user_id) turns a re-run into a no-op INSERT, and
    the UPDATE is guarded on `default_person_id IS NULL` so it can never
    overwrite a default chosen later. Both services run this concurrently at
    startup with no ordering between them -- the same race, answered the
    same constraint-based way, as bootstrap_first_admin().

    No-ops while no admin exists: a fresh install with VITALFORGE_PASS unset
    has an empty users table, and the grant lands on the first boot after an
    admin is finally seeded.
    """
    admin = await (await db.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).fetchone()
    if admin is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO person_grants (person_id, user_id, access, granted_at) "
        "VALUES (?, ?, 'own', ?)",
        (person_id, admin["id"], now),
    )
    await db.execute(
        "UPDATE users SET default_person_id = ? WHERE id = ? AND default_person_id IS NULL",
        (person_id, admin["id"]),
    )
