"""Runtime transport wire types.

Every type here is part of the HTTP / Socket.IO surface between
`RuntimeClient` (orchestrator side) and the runtime server (sandbox
side). Both client and server import from here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentix.idents import CallId, MethodName, PackageName


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class RemoteRequest(BaseModel):
    """POST /_remote body. `package` is the namespace's Python import
    path (e.g. 'agentix.bash'); `method` is the function name on that
    module. `call_id` is an optional correlation key that travels
    alongside the call on the wire."""

    package: PackageName
    method: MethodName
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    call_id: CallId | None = None


class RemoteError(BaseModel):
    type: str
    message: str
    traceback: str | None = None


class RemoteResponse(BaseModel):
    """POST /_remote response."""

    ok: bool
    value: Any = None
    error: RemoteError | None = None
