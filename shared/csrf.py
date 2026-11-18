"""CSRF guard for cookie-authenticated admin mutations.

Same fixed-header scheme as vitalforge_dashboard/context_routes.py: a custom
header forces a cross-origin browser request through a CORS preflight, and no
VitalForge app enables CORS. Sec-Fetch-Site: cross-site is refused as well.
Bearer clients are exempt -- they carry no ambient credential a third-party
page could ride. Do not compare Origin with proxy-derived URLs.
"""

from fastapi import HTTPException, Request

ADMIN_WRITE_HEADER = "X-VitalForge-Write"
ADMIN_WRITE_VALUE = "admin"


def require_admin_write_header(request: Request, identity) -> None:
    """403 a cookie-session mutation lacking the header or flagged cross-site."""
    if identity.source != "cookie":
        return
    if (
        request.headers.get(ADMIN_WRITE_HEADER) != ADMIN_WRITE_VALUE
        or request.headers.get("Sec-Fetch-Site") == "cross-site"
    ):
        raise HTTPException(status_code=403, detail="Admin write header required")
