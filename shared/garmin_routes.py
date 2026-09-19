"""Person-scoped, browser-only Garmin credential lifecycle routes.

Both services expose these routes, so their transport models and authorization
rules live in ``shared`` beside the registry rather than drifting between the
weight and dashboard apps. Garmin credentials are deliberately kept local to
each handler: do not log, serialize, or return these input models.
"""

import os
import re

from fastapi import Depends, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from shared import auth, garmin_registry
from shared.auth import Identity, require_cookie_session_identity, require_person
from shared.database import get_db
from shared.garmin_registry_errors import REGISTRY_ERROR_CODES as _SAFE_AUTH_ERRORS

_CREDENTIAL_PATH_RE = re.compile(r"^/p/[^/]+/api/garmin/(?:link|relink|unlink)$")
_ALLOW_INSECURE_LINKS_ENV = "VITALFORGE_ALLOW_INSECURE_GARMIN_LINKS"
_TRUSTED_PROXY_IPS_ENV = "VITALFORGE_TRUSTED_PROXY_IPS"
# A credential login Garmin throttled carries no provider wait of its own;
# advertise one long enough to outlast a typical login-endpoint cooldown.
_LOGIN_RATE_LIMIT_RETRY_AFTER_SECONDS = 60


def is_garmin_credential_path(path: str) -> bool:
    """Whether a validation failure could otherwise reflect a secret body."""
    return _CREDENTIAL_PATH_RE.fullmatch(path) is not None


def _insecure_links_are_explicitly_allowed() -> bool:
    """Whether a local developer deliberately enabled plaintext linking.

    Garmin and local-account passwords share the link request body.  These
    routes therefore fail closed on HTTP even though the rest of the local
    development app is intentionally usable over HTTP.  The narrowly named
    opt-in keeps a scratch, non-production setup possible without making a
    deployment's transport policy depend on an implicit debug setting.
    """
    return os.environ.get(_ALLOW_INSECURE_LINKS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _trusted_proxy_https(request: Request) -> bool:
    """Whether a configured reverse proxy, not a caller, asserted HTTPS.

    ``X-Forwarded-Proto`` is an ordinary client-controlled header until the
    request comes from a proxy explicitly placed in the deployment's trust
    boundary.  Do not reuse the cookie helper here: that helper intentionally
    predates this password-bearing route and accepts the header unconditionally.
    """
    configured_ips = {
        value.strip()
        for value in os.environ.get(_TRUSTED_PROXY_IPS_ENV, "").split(",")
        if value.strip()
    }
    return (
        request.client is not None
        and request.client.host in configured_ips
        and request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def require_secure_garmin_credential_transport(request: Request) -> None:
    """Reject credential submissions unless their browser transport is HTTPS."""
    if request.url.scheme == "https" or _trusted_proxy_https(request) or _insecure_links_are_explicitly_allowed():
        return
    raise HTTPException(
        status_code=400,
        detail="Garmin credential changes require HTTPS",
    )


class GarminCredentialsIn(BaseModel):
    """Transient Garmin and VitalForge passwords for link/relink only."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: SecretStr
    current_password: SecretStr


class GarminStepUpIn(BaseModel):
    """The local-account password required to remove a Garmin link."""

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr


def _account_values(identity: Identity) -> tuple[int, int]:
    """Extract the cookie-bound values required by the lifecycle transaction."""
    if identity.user_id is None or identity.session_version is None:
        # require_cookie_session_identity establishes both values. Keep this
        # fail-closed guard in case a future identity source changes shape.
        raise HTTPException(status_code=401, detail="Account changed; authenticate again")
    return identity.user_id, identity.session_version


def _redacted_status(row) -> dict[str, str | bool | None]:
    """Return only non-secret, bounded link health metadata."""
    if row is None:
        return {
            "linked": False,
            "last_auth_ok": None,
            "last_auth_error": None,
            "last_auth_error_at": None,
        }
    error = row["last_auth_error"]
    return {
        "linked": row["state"] == "linked",
        "last_auth_ok": row["last_auth_ok"],
        "last_auth_error": error if error in _SAFE_AUTH_ERRORS else "unknown" if error is not None else None,
        "last_auth_error_at": row["last_auth_error_at"],
    }


async def _status_for_person(person_id: int) -> dict[str, str | bool | None]:
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT state, last_auth_ok, last_auth_error, last_auth_error_at "
                "FROM garmin_links WHERE person_id = ?",
                (person_id,),
            )
        ).fetchone()
    finally:
        await db.close()
    return _redacted_status(row)


def _registry_http_error(exc: garmin_registry.GarminRegistryError) -> HTTPException:
    """Translate only bounded registry failures; never expose exception text."""
    if isinstance(exc, (garmin_registry.GarminRateLimited, garmin_registry.GarminLinkAttemptRateLimited)):
        return HTTPException(
            status_code=429,
            detail="Garmin is temporarily rate limited",
            headers={"Retry-After": str(exc.retry_after)},
        )
    if isinstance(exc, garmin_registry.GarminLinkConflict):
        return HTTPException(status_code=409, detail="Garmin account is already linked")
    if isinstance(exc, garmin_registry.GarminNotLinked):
        # A target can disappear after require_person() resolves it but before
        # the lifecycle transaction acquires its lock. Match require_person's
        # non-enumerating answer rather than presenting this as retryable I/O.
        return HTTPException(status_code=404, detail="Person not found")
    if isinstance(exc, garmin_registry.GarminSessionExpired):
        return HTTPException(status_code=401, detail="Account changed; authenticate again")
    if isinstance(exc, garmin_registry.GarminLinkInputError):
        return HTTPException(status_code=422, detail="Garmin link details are invalid")
    if isinstance(exc, garmin_registry.GarminAuthenticationError):
        # The login's bounded code, not its type, decides the answer: only a
        # genuine credential rejection is a 401 that asks for new credentials.
        # A throttled login has no retry_after of its own, so it advertises
        # a fixed wait; a transient failure is the same 502 as any other.
        if exc.code == "auth_failed":
            return HTTPException(status_code=401, detail="Garmin authentication failed")
        if exc.code == "rate_limited":
            return HTTPException(
                status_code=429,
                detail="Garmin is temporarily rate limited",
                headers={"Retry-After": str(_LOGIN_RATE_LIMIT_RETRY_AFTER_SECONDS)},
            )
    return HTTPException(status_code=502, detail="Garmin operation failed")


async def _link_response(
    *, person_id: int, identity: Identity, data: GarminCredentialsIn, relink: bool
) -> dict[str, str | bool | None]:
    """Perform one transient credential submission and return redacted state."""
    actor_id, session_version = _account_values(identity)
    # SecretStr keeps accidental repr/model_dump logging masked. Its plaintext
    # is extracted only at this call boundary and never copied into a response,
    # database statement, or log record.
    password = data.password.get_secret_value()
    current_password = data.current_password.get_secret_value()
    await auth._require_step_up(identity, current_password)
    try:
        operation = garmin_registry.relink if relink else garmin_registry.link
        await operation(person_id, actor_id, session_version, data.email, password)
    except garmin_registry.GarminRegistryError as exc:
        raise _registry_http_error(exc) from None
    return await _status_for_person(person_id)


def add_garmin_routes(app) -> None:
    """Register the identical person-scoped lifecycle surface on one app."""

    # FastAPI's stock RequestValidationError response includes the rejected
    # ``input`` value. That is useful for ordinary DTOs, but it would echo a
    # Garmin or local password on these three credential routes. Preserve any
    # handler already registered for every other route; the weight app installs
    # a later global handler, and it repeats this same narrow path check.
    previous_validation_handler = app.exception_handlers.get(RequestValidationError)

    @app.exception_handler(RequestValidationError)
    async def garmin_credential_validation_handler(request: Request, exc: RequestValidationError):
        if is_garmin_credential_path(request.url.path):
            return JSONResponse(status_code=422, content={"detail": "Invalid Garmin link request"})
        if previous_validation_handler is not None:
            return await previous_validation_handler(request, exc)
        return await request_validation_exception_handler(request, exc)

    @app.get("/p/{slug}/api/garmin/status")
    async def garmin_status(
        person_id: int = Depends(require_person("manage")),
        _identity: Identity = Depends(require_cookie_session_identity),
    ):
        # Cookie-only + manage even for status: a bearer token must not learn
        # whether a household member has a linked third-party account.
        return await _status_for_person(person_id)

    @app.post(
        "/p/{slug}/api/garmin/link",
        dependencies=[Depends(require_secure_garmin_credential_transport)],
    )
    async def link_garmin(
        data: GarminCredentialsIn,
        person_id: int = Depends(require_person("manage")),
        identity: Identity = Depends(require_cookie_session_identity),
    ):
        return await _link_response(person_id=person_id, identity=identity, data=data, relink=False)

    @app.post(
        "/p/{slug}/api/garmin/relink",
        dependencies=[Depends(require_secure_garmin_credential_transport)],
    )
    async def relink_garmin(
        data: GarminCredentialsIn,
        person_id: int = Depends(require_person("manage")),
        identity: Identity = Depends(require_cookie_session_identity),
    ):
        return await _link_response(person_id=person_id, identity=identity, data=data, relink=True)

    @app.post(
        "/p/{slug}/api/garmin/unlink",
        dependencies=[Depends(require_secure_garmin_credential_transport)],
    )
    async def unlink_garmin(
        data: GarminStepUpIn,
        person_id: int = Depends(require_person("manage")),
        identity: Identity = Depends(require_cookie_session_identity),
    ):
        actor_id, session_version = _account_values(identity)
        await auth._require_step_up(identity, data.current_password.get_secret_value())
        try:
            await garmin_registry.unlink(person_id, actor_id, session_version)
        except garmin_registry.GarminRegistryError as exc:
            raise _registry_http_error(exc) from None
        return await _status_for_person(person_id)
