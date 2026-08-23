"""B5: `sync_weight_history()` reading Garmin's composition fields
(bodyWater/boneMass/muscleMass) off `latestWeight` into `weight_history`.
"""

from shared.database import get_db
from tests.conftest import import_service_module


async def test_sync_populates_composition_from_weigh_ins_fixture(initialized_db, fake_garmin_client):
    sync = import_service_module("vitalforge-dashboard.sync")

    await sync.sync_weight_history("2020-05-01", "2020-06-30")

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT weight_grams, bmi, body_fat, body_water, bone_mass_g, muscle_mass_g "
                "FROM weight_history WHERE date = ?",
                ("2020-06-01",),
            )
        ).fetchone()
    finally:
        await db.close()

    assert row is not None
    assert row["weight_grams"] == 81200
    assert row["bmi"] == 24.1
    assert row["body_fat"] == 18.4
    assert row["body_water"] == 55.2
    assert row["bone_mass_g"] == 3200
    assert row["muscle_mass_g"] == 34000
