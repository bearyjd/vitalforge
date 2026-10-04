"""Request bodies used by the authentication route layer.

Keeping these schemas separate lets :mod:`shared.auth` remain focused on
identity and authorization while preserving its established re-exports for
route modules.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


class CreateUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: Literal["admin", "user"] = "user"


class UpdateUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user"] | None = None
    password: str | None = None


class CreateTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    current_password: str


class RevokeTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
