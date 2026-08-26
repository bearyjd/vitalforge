"""Minimal, dependency-free FIT (.fit) binary builder for tests.

Hand-encodes just enough of the Garmin FIT binary protocol (12-byte header,
one `file_id` definition+data message, one `session` definition+data
message, trailing file CRC) to produce a file `fitparse.FitFile` accepts
with CRC checking enabled -- there is no upstream FIT *writer* library in
this repo's dependency set, and pulling one in just to generate test bytes
would be a heavier dependency than the feature itself needs.

Field numbers, base-type byte values, and the CRC-16 table below are taken
from `fitparse.profile.MESSAGE_TYPES` / `fitparse.records.BASE_TYPES` /
`fitparse.records.Crc` (this repo's actual FIT parser) rather than
reimplemented independently, so a builder/parser mismatch cannot silently
pass a test that a real device's FIT file would fail.
"""

import struct
from datetime import datetime, timezone

from fitparse.records import Crc

# FIT timestamps are uint32 seconds since 1989-12-31T00:00:00Z UTC, not the
# Unix epoch.
_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

_ENUM = 0x00
_UINT8 = 0x02
_UINT16 = 0x84
_UINT32 = 0x86


def _to_fit_timestamp(dt: datetime) -> int:
    return int((dt - _FIT_EPOCH).total_seconds())


def _definition_message(local_type: int, global_mesg_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    """`fields` is a list of (field_def_num, size_bytes, base_type_byte)."""
    header = bytes([0x40 | local_type])
    body = struct.pack("<BBHB", 0, 0, global_mesg_num, len(fields))  # reserved, arch(LE), global#, num_fields
    for field_def_num, size, base_type in fields:
        body += struct.pack("<BBB", field_def_num, size, base_type)
    return header + body


def _data_message(local_type: int, packed_fields: bytes) -> bytes:
    return bytes([0x00 | local_type]) + packed_fields


def build_minimal_fit_file(
    *,
    start_time: datetime,
    sport: int = 1,  # 1 = running, per fitparse's Sport enum
    elapsed_seconds: float = 1800.0,
    distance_m: float = 5000.0,
    calories: int = 400,
    avg_hr: int = 140,
    max_hr: int = 175,
    ascent_m: int = 50,
) -> bytes:
    """Build a minimal but structurally valid FIT activity file: a `file_id`
    message followed by one `session` message, matching the subset of
    fields `fit_import.py` reads. CRC-correct against `fitparse`'s own
    check_crc=True default.
    """
    fit_ts = _to_fit_timestamp(start_time)

    file_id_def = _definition_message(
        local_type=0,
        global_mesg_num=0,  # file_id
        fields=[
            (0, 1, _ENUM),  # type
            (1, 2, _UINT16),  # manufacturer
            (2, 2, _UINT16),  # product
            (4, 4, _UINT32),  # time_created
        ],
    )
    file_id_data = _data_message(
        0,
        struct.pack("<BHHI", 4, 1, 1, fit_ts),  # type=4 (activity), manufacturer=1, product=1
    )

    session_def = _definition_message(
        local_type=1,
        global_mesg_num=18,  # session
        fields=[
            (253, 4, _UINT32),  # timestamp
            (2, 4, _UINT32),  # start_time
            (5, 1, _ENUM),  # sport
            (7, 4, _UINT32),  # total_elapsed_time (scale 1000)
            (9, 4, _UINT32),  # total_distance (scale 100)
            (11, 2, _UINT16),  # total_calories
            (16, 1, _UINT8),  # avg_heart_rate
            (17, 1, _UINT8),  # max_heart_rate
            (22, 2, _UINT16),  # total_ascent
        ],
    )
    session_data = _data_message(
        1,
        struct.pack(
            "<IIBIIHBBH",
            fit_ts,  # timestamp
            fit_ts,  # start_time
            sport,
            round(elapsed_seconds * 1000),
            round(distance_m * 100),
            calories,
            avg_hr,
            max_hr,
            ascent_m,
        ),
    )

    records = file_id_def + file_id_data + session_def + session_data

    header = struct.pack("<BBHI4s", 12, 0x10, 2078, len(records), b".FIT")

    crc = Crc.calculate(header + records)
    return header + records + struct.pack("<H", crc)


_STRING = 0x07


def build_fit_file_with_non_numeric_calories(*, start_time: datetime) -> bytes:
    """Structurally valid FIT file (correct CRC, passes the magic-byte
    sniff and `fitparse`'s own parse()) whose `session` message declares
    `total_calories` (field 11) with the `string` base type instead of the
    real device's `uint16`, carrying the non-numeric value `"bad"`.
    `fitparse` decodes this without error -- the base type is only ever
    read off the file's own definition message, not fixed by the FIT spec
    -- so this exercises `fit_import.parse_fit_bytes`'s session-field
    extraction step (the `int(fields["total_calories"])` conversion) failing
    *after* a successful parse, rather than the parse call itself."""
    fit_ts = _to_fit_timestamp(start_time)

    file_id_def = _definition_message(
        local_type=0,
        global_mesg_num=0,
        fields=[(0, 1, _ENUM), (1, 2, _UINT16), (2, 2, _UINT16), (4, 4, _UINT32)],
    )
    file_id_data = _data_message(0, struct.pack("<BHHI", 4, 1, 1, fit_ts))

    session_def = _definition_message(
        local_type=1,
        global_mesg_num=18,
        fields=[
            (253, 4, _UINT32),  # timestamp
            (2, 4, _UINT32),  # start_time
            (5, 1, _ENUM),  # sport
            (7, 4, _UINT32),  # total_elapsed_time (scale 1000)
            (9, 4, _UINT32),  # total_distance (scale 100)
            (11, 4, _STRING),  # total_calories -- non-numeric, malformed
            (16, 1, _UINT8),  # avg_heart_rate
            (17, 1, _UINT8),  # max_heart_rate
            (22, 2, _UINT16),  # total_ascent
        ],
    )
    session_data = _data_message(
        1,
        struct.pack("<IIBII", fit_ts, fit_ts, 1, round(1800 * 1000), round(5000 * 100))
        + b"bad\x00"
        + struct.pack("<BBH", 140, 175, 50),
    )

    records = file_id_def + file_id_data + session_def + session_data
    header = struct.pack("<BBHI4s", 12, 0x10, 2078, len(records), b".FIT")
    crc = Crc.calculate(header + records)
    return header + records + struct.pack("<H", crc)
