"""Runtime transport wire types.

Every type here is part of the HTTP / Socket.IO surface between
`RuntimeClient` (orchestrator side) and the runtime server (sandbox
side). Both client and server import from here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agentix.runtime.shared.idents import CallId

CallShape = Literal["unary", "stream", "bidi"]


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class RemoteRequest(BaseModel):
    """Remote call request.

    The callable itself is serialized with stdlib pickle. Module-level
    functions/classes and pickleable callable objects are the supported
    boundary; lambdas and local closures are intentionally out of scope.
    """

    callable_payload: bytes
    display_name: str
    shape: CallShape
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    call_id: CallId | None = None

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        if not value:
            raise ValueError("display_name must be non-empty")
        return value


class RemoteError(BaseModel):
    type: str
    message: str
    traceback: str | None = None
    cancelled: bool = False


class RemoteResponse(BaseModel):
    """POST /_remote response."""

    ok: bool
    value: Any = None
    error: RemoteError | None = None
