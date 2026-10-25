"""Additive context tables, safe for simultaneous service boots and old images."""


async def ensure_context_schema() -> None:
    # Import lazily: database.init_db invokes this helper after existing migrations.
    from shared.database import get_db

    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_context (
                person_id INTEGER NOT NULL REFERENCES persons(id),
                date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 1000),
                tags_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (person_id, date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS context_experiments (
                person_id INTEGER PRIMARY KEY REFERENCES persons(id),
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 100),
                tag TEXT NOT NULL CHECK(length(tag) BETWEEN 1 AND 40),
                metric TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        await db.close()
