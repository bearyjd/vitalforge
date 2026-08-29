#!/usr/bin/env python3
"""Seed a VitalForge SQLite DB with synthetic data — no Garmin account needed.

Dashboard read endpoints (`/api/metrics/*`, `/api/recommendations/rules-only`)
only ever read from local SQLite tables (see `.agent_native/agent_roadmap.md`
item 2 and `CLAUDE.md`), so this script lets an agent reproduce a reported
data pattern by seeding the DB directly and then starting the dashboard
against it — zero Garmin credentials required.

All values are invented; never point --db-path at a real fitness.db.

Usage:
    python scripts/seed_db.py --days 90 --db-path /tmp/vf.db
    python scripts/seed_db.py --days 30 --db-path /tmp/vf.db --pattern declining-hrv
    python scripts/seed_db.py --days 30 --db-path /tmp/vf.db --pattern overtraining
"""

import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PATTERNS = ["normal", "declining-hrv", "declining-sleep", "overtraining"]


def build_day(i: int, days: int, pattern: str, rng: random.Random) -> dict:
    """Compute synthetic metric values for day index `i` (0 = oldest).

    `progress` runs 0.0 -> 1.0 from the oldest to the most recent day, used
    to drive linear trends for the "declining"/"overtraining" patterns.
    """
    progress = i / max(days - 1, 1)

    hrv_baseline = 45.0
    sleep_baseline_sec = 27000  # 7.5h
    rhr_baseline = 55.0
    acute_load_baseline = 80.0
    chronic_load_baseline = 80.0

    if pattern == "declining-hrv":
        hrv = hrv_baseline - progress * 18 + rng.uniform(-2, 2)
        sleep_sec = sleep_baseline_sec + rng.uniform(-1200, 1200)
        rhr = rhr_baseline + progress * 4 + rng.uniform(-1, 1)
        acute_load = acute_load_baseline + rng.uniform(-10, 10)
        chronic_load = chronic_load_baseline + rng.uniform(-5, 5)
    elif pattern == "declining-sleep":
        hrv = hrv_baseline + rng.uniform(-3, 3)
        sleep_sec = sleep_baseline_sec - progress * 9000 + rng.uniform(-900, 900)
        rhr = rhr_baseline + progress * 2 + rng.uniform(-1, 1)
        acute_load = acute_load_baseline + rng.uniform(-10, 10)
        chronic_load = chronic_load_baseline + rng.uniform(-5, 5)
    elif pattern == "overtraining":
        hrv = hrv_baseline - progress * 15 + rng.uniform(-2, 2)
        sleep_sec = sleep_baseline_sec - progress * 5000 + rng.uniform(-900, 900)
        rhr = rhr_baseline + progress * 6 + rng.uniform(-1, 1)
        acute_load = acute_load_baseline + progress * 120 + rng.uniform(-10, 10)
        chronic_load = chronic_load_baseline + progress * 20 + rng.uniform(-5, 5)
    else:  # normal
        hrv = hrv_baseline + rng.uniform(-4, 4)
        sleep_sec = sleep_baseline_sec + rng.uniform(-1500, 1500)
        rhr = rhr_baseline + rng.uniform(-2, 2)
        acute_load = acute_load_baseline + rng.uniform(-15, 15)
        chronic_load = chronic_load_baseline + rng.uniform(-10, 10)

    sleep_sec = max(sleep_sec, 3600)
    deep = int(sleep_sec * 0.20)
    light = int(sleep_sec * 0.55)
    rem = int(sleep_sec * 0.20)
    awake = int(sleep_sec * 0.05)
    sleep_score = max(20, min(100, round(85 - progress * 25 if "declin" in pattern or pattern == "overtraining" else 82 + rng.uniform(-8, 8))))

    return {
        "sleep": {
            "duration_seconds": int(sleep_sec),
            "deep_seconds": deep,
            "light_seconds": light,
            "rem_seconds": rem,
            "awake_seconds": awake,
            "sleep_score": sleep_score,
            "avg_spo2": round(96 + rng.uniform(-1.5, 1.5), 1),
            "avg_respiration": round(14 + rng.uniform(-1, 1), 1),
        },
        "resting_hr": {"value": round(rhr)},
        "hrv": {
            "last_night_avg": round(max(hrv, 10), 1),
            "last_night_5min_high": round(max(hrv, 10) + 12, 1),
            "weekly_avg": round(max(hrv, 10) + rng.uniform(-2, 2), 1),
            "status": "BALANCED",
        },
        "body_battery": {
            "charged": round(70 + rng.uniform(-10, 10)),
            "drained": round(60 + rng.uniform(-10, 10)),
            "highest": round(90 + rng.uniform(-8, 8)),
            "lowest": round(20 + rng.uniform(-8, 8)),
        },
        "stress": {
            "avg_level": round(28 + rng.uniform(-8, 8)),
            "max_level": round(75 + rng.uniform(-10, 10)),
            "rest_duration": 20000,
            "low_duration": 30000,
            "medium_duration": 9000,
            "high_duration": 1800,
        },
        "vo2max": {
            "vo2max_value": round(46 + rng.uniform(-1.5, 1.5), 1),
            "fitness_age": 30,
        },
        "weight_history": {
            "weight_grams": round(81000 + rng.uniform(-500, 500)),
            "bmi": round(24.0 + rng.uniform(-0.3, 0.3), 1),
            "body_fat": round(18.0 + rng.uniform(-1, 1), 1),
        },
        "training_load": {
            "acute_load": round(max(acute_load, 0), 1),
            "chronic_load": round(max(chronic_load, 0), 1),
            "load_ratio": round(acute_load / chronic_load, 2) if chronic_load else None,
        },
        "steps": {"value": round(8000 + rng.uniform(-2000, 2000))},
        "active_calories": {"value": round(550 + rng.uniform(-150, 150))},
    }


async def _resolve_person(database, slug: str | None) -> tuple[int, str, bool]:
    """Return (person_id, slug, created) for the person to seed.

    With no --person, this is the primary person the migration created, which
    keeps the pre-multi-tenancy behavior of this script intact. With --person,
    a missing person is CREATED rather than erroring: seeding a second person
    is the whole point of the flag, and requiring the operator to first create
    one through an admin route that does not exist until Phase 2's PR 3 would
    make the flag unusable in exactly the phase that needs it.

    A created person gets NO person_grants row, deliberately. Once Phase 2's
    require_person lands, that person's data will 404 for every user until
    someone grants access -- which is exactly the state a cross-person
    isolation test wants, and the reason this script does not guess at one.
    The caller is told so on stdout rather than discovering it as a bug.

    `created` is returned rather than inferred from `slug is not None` so that
    note is only printed when it is TRUE. `--person <an existing slug>` (e.g.
    the primary person on a database that already has an admin and a grant)
    resolves to that row and creates nothing, and claiming it has no grant
    would be a plain falsehood.
    """
    from shared.slugs import RESERVED_SLUGS, slugify

    if slug is None:
        person_id = await database.get_primary_person_id()
        db = await database.get_db()
        try:
            row = await (
                await db.execute("SELECT slug FROM persons WHERE id = ?", (person_id,))
            ).fetchone()
        finally:
            await db.close()
        return person_id, row["slug"], False

    normalized = slugify(slug)
    if not normalized or normalized in RESERVED_SLUGS:
        raise SystemExit(
            f"--person '{slug}' is not a usable slug "
            f"(normalizes to '{normalized}'; reserved: {sorted(RESERVED_SLUGS)})"
        )

    db = await database.get_db()
    try:
        # Idempotent by constraint, not by pre-check -- the convention this
        # codebase states in shared/database.py's _grant_primary_person_to_
        # first_admin and _add_columns. A check-then-insert here would let two
        # concurrent runs both miss the SELECT and one die on persons.slug
        # UNIQUE with a raw traceback.
        cursor = await db.execute(
            "INSERT INTO persons (slug, display_name, created_at, is_primary) "
            "VALUES (?, ?, ?, 0) ON CONFLICT (slug) DO NOTHING",
            (normalized, normalized, datetime.now(timezone.utc).isoformat()),
        )
        # rowcount distinguishes insert from conflict without a second read,
        # and without the check-then-insert race the pre-check version had.
        created = cursor.rowcount == 1
        await db.commit()
        row = await (
            await db.execute("SELECT id FROM persons WHERE slug = ?", (normalized,))
        ).fetchone()
        if created:
            print(f"Created person '{normalized}' (id={row['id']})")
        return row["id"], normalized, created
    finally:
        await db.close()


async def seed(db_path: Path, days: int, pattern: str, seed_value: int, person: str | None = None):
    os.environ["DB_PATH"] = str(db_path)

    from shared import database

    database.DB_PATH = db_path  # module-level override, mirrors tests/conftest.py
    await database.init_db()

    person_id, person_slug, person_created = await _resolve_person(database, person)

    rng = random.Random(seed_value)
    today = datetime.now(timezone.utc).date()

    db = await database.get_db()
    try:
        for i in range(days):
            date_offset = days - 1 - i  # oldest first, i=0 -> oldest
            date_str = (today - timedelta(days=date_offset)).isoformat()
            day = build_day(i, days, pattern, rng)

            for table, columns in day.items():
                # person_id is not optional: every metric table is keyed
                # (person_id, date) NOT NULL since migration 001, so omitting
                # it does not write rows nobody can read -- it raises
                # IntegrityError on the first insert and seeds nothing.
                cols = ["person_id", "date"] + list(columns.keys())
                placeholders = ", ".join(["?"] * len(cols))
                col_names = ", ".join(cols)
                values = [person_id, date_str] + list(columns.values())
                await db.execute(
                    f"INSERT OR REPLACE INTO [{table}] ({col_names}) VALUES ({placeholders})",
                    values,
                )
        await db.commit()
    finally:
        await db.close()

    print(
        f"Seeded {days} days of synthetic '{pattern}' data for person "
        f"'{person_slug}' (id={person_id}) into {db_path}"
    )
    if person_created:
        print(
            f"Note: '{person_slug}' has no person_grants row. Once access control "
            "lands, this person's data is reachable only by an admin or by a user "
            "you grant explicitly -- which is the intended starting state for a "
            "cross-person isolation test."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="Number of days of history to generate (default: 30)")
    parser.add_argument("--db-path", type=Path, required=True, help="Path to the SQLite DB to seed (never the real fitness.db)")
    parser.add_argument("--pattern", choices=PATTERNS, default="normal", help="Synthetic trend pattern to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument(
        "--person",
        default=None,
        help="Slug of the person to seed. Created if absent. Defaults to the primary person.",
    )
    args = parser.parse_args()

    resolved = args.db_path.resolve()
    if resolved.name in ("fitness.db",) or ".garth" in resolved.parts:
        parser.error(
            f"refusing to write to '{resolved}' — looks like a real data path. "
            "Use a scratch path (e.g. /tmp/vf.db)."
        )

    asyncio.run(seed(args.db_path, args.days, args.pattern, args.seed, args.person))


if __name__ == "__main__":
    main()
