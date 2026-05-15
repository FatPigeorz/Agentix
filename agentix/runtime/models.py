"""Runtime transport wire types.

Lives under `agentix.runtime.models` because every type here is part of the
HTTP / Socket.IO surface between `RuntimeClient` (orchestrator side) and the
runtime server (sandbox side). Both client and server import from here;
sibling subpackages (`runtime/client/`, `runtime/server/`) depend on this
module but not on each other.

Cross-cutting concepts that aren't wire types — `NamespaceManifest`,
`SandboxConfig`, `SandboxInfo`, `AGENTIX_CLOSURE_ABI` — stay in
`agentix.models` since they're consumed by namespace authors / deployment
code that doesn't touch the runtime transport.
"""

from __future__ import annotations

import collections.abc as cabc
from typing import Any

from pydantic import BaseModel, Field

from agentix.idents import CallId, MethodName, PackageName
from agentix.models import NamespaceManifest

#: Type-system origins that mark a parameter or return annotation as
#: streaming (`AsyncIterator[T]` / `AsyncGenerator[T, ...]`). Used by the
#: dispatcher to detect server-streaming / bidi impls at bind time AND by
#: the client to pick the transport path at call time — single rule for
#: both sides of the wire.
STREAM_ORIGINS: tuple[type, ...] = (cabc.AsyncIterator, cabc.AsyncGenerator)

# ── Runtime introspection (GET /namespaces, /health) ──────────────────


class NamespaceInfo(BaseModel):
    """One entry in GET /namespaces. `manifest.package` is the namespace identity."""

    manifest: NamespaceManifest


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


# ── Remote-call wire (POST /_remote, plus Socket.IO stream/bidi events) ─


class RemoteRequest(BaseModel):
    """POST /_remote body. `package` is the namespace's Python import path
    (e.g. 'agentix.bash'); `method` is a stub name bound
    by that namespace's Dispatcher.

    `call_id` is an optional rollout correlation key; the dispatcher pins
    it into a contextvar so trace events emitted from inside the impl
    inherit it automatically. Stream / bidi calls carry their own
    `call_id` on the Socket.IO wire — same semantic.
    """

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


# ── Log + trace Socket.IO events ────────────────────────────────────


class LogRecord(BaseModel):
    """One log line forwarded over the Socket.IO `log` event.

    Subscribers receive these whenever the runtime (or any namespace logger
    under the `agentix.*` tree) emits a logging record.
    """

    level: str          # e.g. "INFO", "WARNING"
    name: str           # logger name
    message: str        # formatted message
    timestamp: float    # record.created — seconds since epoch


class TraceEvent(BaseModel):
    """One semantic event from a rollout, broadcast via the `trace` Socket.IO
    event. Namespaces emit these through `agentix.trace.emit(...)` to record
    LLM calls, tool invocations, rewards, or arbitrary checkpoint markers.

    The `call_id` correlates events to a specific rollout — for unary HTTP
    calls it can be set on `RemoteRequest.call_id`; for stream/bidi it's the
    `call_id` already on the Socket.IO wire frame. The dispatcher pins it
    into a contextvar before invoking the impl so `trace.emit()` picks it
    up automatically.
    """

    kind: str                       # e.g. "llm_request", "llm_response", "tool_call", "reward"
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float                # emit-time seconds since epoch
    call_id: CallId | None = None   # rollout / call correlation key
    source: PackageName | None = None  # namespace package or "runtime" that emitted this


# Shell exec and file I/O used to live at this layer too (ExecRequest /
# ExecResponse, UploadResponse). They moved to the `bash` and `files`
# primitive namespaces under `primitives/`. Their request/response shapes
# live in those namespace packages (`BashResult`, `UploadResult` etc.) and
# travel as ordinary namespace dispatches over /_remote.
