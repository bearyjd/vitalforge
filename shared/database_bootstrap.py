"""Post-migration database bootstrap repairs.

Kept separate from ``shared.database`` so the connection/schema module stays
focused on durable DDL and its public connection helpers.
"""

import logging

logger = logging.getLogger(__name__)


async def _attribute_orphaned_weight_log_rows() -> None:
    """Give any weight_log row with a NULL person_id to the primary person.

    Migration 001 runs this same backfill, but its marker commits in the same
    transaction -- so a row written with a NULL person_id AFTER 001 commits is
    never repaired by the migration, which skips itself forever. Nothing else
    repairs it either: weight_log.person_id cannot be NOT NULL (SQLite cannot
    add a NOT NULL column without a constant default, which is why it is an
    additive column rather than a rebuild), so the schema will not refuse such
    a row on the way in.

    That row is reachable by doing what README's Upgrading step 1 forbids:
    leaving an old weight-service container running against the newly rebuilt
    schema. Its INSERT predates person_id and simply omits it. Every read path
    now filters `person_id = ?`, so the result is worse than mis-attribution
    -- the entry is invisible in /recent, /trend and DELETE, and the user sees
    a weight they logged silently missing rather than merely misfiled.

    Running the backfill on every boot makes that self-healing instead of
    permanent. It is idempotent and matches zero rows on a healthy database.
    """
    # Imported at call time to avoid a module-import cycle while
    # shared.database re-exports this helper for existing callers.
    from shared.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE weight_log SET person_id = (SELECT id FROM persons WHERE is_primary = 1) "
            "WHERE person_id IS NULL"
        )
        await db.commit()
    finally:
        await db.close()
    if cursor.rowcount:
        # warning, not info: reaching this means an unsupported upgrade
        # happened and the operator should know their data was repaired.
        logger.warning(
            "Attributed %d unattributed weight_log row(s) to the primary person. "
            "This means a pre-multi-tenancy weight service wrote to this database "
            "after the person-id migration -- see README's Upgrading section.",
            cursor.rowcount,
        )
