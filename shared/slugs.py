"""URL-safe slug derivation for person identifiers.

Lives here rather than in shared/auth.py because slug safety is a routing
concern, not an authentication one: _RESERVED_USERNAMES governs "safe as a
username", a different and smaller rule than "safe as a path segment under
/p/{slug}/". Keeping them apart also lets shared/migrations.py stop importing
shared/auth.py entirely -- migrations needed nothing else from it.
"""

import re
import unicodedata

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

# Anything that would shadow a real path segment under /p/{slug}/ or collide
# with the sentinels shared/auth.py reserves for usernames.
RESERVED_SLUGS = {
    "api", "auth", "static", "health", "p", "new", "admin", "persons",
    "anonymous", "api-token",
}


def slugify(raw: str) -> str:
    """Derive a URL-safe slug from a display name or username.

    Slugify, do not copy verbatim. Returns "" when nothing usable survives --
    callers must handle that rather than persisting an empty slug into a NOT
    NULL UNIQUE column (see shared/migrations.py's _ensure_primary_person).
    """
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32].strip("-")
    return s if SLUG_RE.match(s) else ""
