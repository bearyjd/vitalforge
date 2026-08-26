"""Parse a Garmin FIT activity file into an ActivityRecord.

FIT-only for this first slice -- TCX/GPX are explicitly deferred to a
follow-on PR (see CLAUDE.md's activity-import scope note). Kept as a
standalone module (not folded into app.py) so a later TCX/GPX parser can
sit next to this one and produce the same ActivityRecord shape.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from fitparse import FitFile, FitParseError

logger = logging.getLogger(__name__)

# FIT files start with a 12- or 14-byte header; bytes 8-11 are always the
# ASCII signature ".FIT". Checked directly against the bytes, never against
# a client-supplied filename extension or Content-Type header.
_FIT_MAGIC = b".FIT"
_FIT_HEADER_MIN_SIZE = 12
_FIT_VALID_HEADER_SIZES = (12, 14)

# Upload size cap. FIT activity summaries are tiny; even a long activity
# with dense per-record GPS/HR streams is normally a few MB -- 20 MB is
# generous headroom without letting an arbitrarily large upload buffer
# in memory. Env-overridable per this repo's convention (DB_PATH,
# GARTH_TOKEN_DIR) for ops flexibility without a code change.
MAX_UPLOAD_BYTES = int(os.getenv("FIT_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))


class FitImportError(Exception):
    """Any FIT upload that cannot be safely imported -- caught at the route
    layer and turned into a 400, never allowed to surface as a 500."""


@dataclass(frozen=True)
class ActivityRecord:
    """Parsed activity summary, ready to insert into the `activities` table
    (minus `id`/`imported_at`, which the route layer assigns). `raw_summary`
    is a plain dict of every field this parser read off the FIT session
    message, for the `raw_summary_json` column -- kept as a dict rather than
    pre-serialized JSON so the route layer owns the actual `json.dumps` call
    and can choose its own encoding fallback."""

    start_time_utc: str
    sport: str | None
    duration_seconds: int | None
    distance_m: float | None
    calories: int | None
    avg_hr: int | None
    max_hr: int | None
    elevation_gain_m: float | None
    source_format: str
    raw_summary: dict


def compute_file_hash(data: bytes) -> str:
    """SHA-256 of the raw upload -- the exact-duplicate key (`activities`'s
    UNIQUE `file_sha256` column)."""
    return hashlib.sha256(data).hexdigest()


def sniff_fit_magic(data: bytes) -> bool:
    """True if `data` starts with a structurally plausible FIT header.
    Deliberately does not attempt CRC validation here -- that's `fitparse`'s
    job once we're past this cheap up-front sniff."""
    if len(data) < _FIT_HEADER_MIN_SIZE:
        return False
    header_size = data[0]
    if header_size not in _FIT_VALID_HEADER_SIZES:
        return False
    if len(data) < header_size:
        return False
    return data[8:12] == _FIT_MAGIC


def _round_or_none(value: float | None, ndigits: int = 1) -> float | None:
    return None if value is None else round(value, ndigits)


def _int_or_none(value) -> int | None:
    return None if value is None else int(value)


def parse_fit_bytes(data: bytes) -> ActivityRecord:
    """Parse FIT bytes into an ActivityRecord using the file's `session`
    summary message. Never lets a `fitparse` exception (or any other parse
    failure) escape -- every failure surfaces as FitImportError so the route
    layer has exactly one exception type to catch."""
    if not sniff_fit_magic(data):
        raise FitImportError("file does not look like a FIT file (magic bytes missing)")

    try:
        fit_file = FitFile(BytesIO(data))
        fit_file.parse()
    except FitParseError as e:
        raise FitImportError(f"could not parse FIT file: {e}") from e
    except Exception as e:
        # fitparse can raise plain struct/EOF errors on malformed input that
        # passed the magic-byte sniff (e.g. truncated mid-record) -- these
        # are not FitParseError subclasses but are just as much a bad
        # upload, not a server bug.
        raise FitImportError(f"could not parse FIT file: {e}") from e

    sessions = list(fit_file.get_messages("session"))
    if not sessions:
        raise FitImportError("FIT file has no session message (not an activity file)")

    fields = {f.name: f.value for f in sessions[0]}

    start_time = fields.get("start_time") or fields.get("timestamp")
    if not isinstance(start_time, datetime):
        raise FitImportError("FIT session message has no usable start_time")
    if start_time.tzinfo is None:
        # fitparse returns naive datetimes for FIT's date_time fields, which
        # are always UTC per the FIT spec -- not "assume UTC", the spec
        # defines them that way.
        start_time = start_time.replace(tzinfo=timezone.utc)
    else:
        start_time = start_time.astimezone(timezone.utc)

    duration = fields.get("total_elapsed_time")
    if duration is None:
        duration = fields.get("total_timer_time")

    sport = fields.get("sport")

    raw_summary = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fields.items()}

    return ActivityRecord(
        start_time_utc=start_time.isoformat(),
        sport=str(sport) if sport is not None else None,
        duration_seconds=_int_or_none(round(duration)) if duration is not None else None,
        distance_m=_round_or_none(fields.get("total_distance")),
        calories=_int_or_none(fields.get("total_calories")),
        avg_hr=_int_or_none(fields.get("avg_heart_rate")),
        max_hr=_int_or_none(fields.get("max_heart_rate")),
        elevation_gain_m=_round_or_none(fields.get("total_ascent")),
        source_format="fit",
        raw_summary=raw_summary,
    )
