"""Branded string identifiers.

`NewType` aliases for the four `str`s that appear in the wire layer and
that are easy to confuse: a namespace's import path, a method name bound
in a Dispatcher, the rollout/call correlation key, and the sandbox
handle returned by deployment. They're plain `str` at runtime (zero
cost) but pyright treats them as distinct, so swapping one for another
becomes a type error.

Pydantic v2 understands `NewType` — fields annotated `CallId` validate
as `str` and round-trip cleanly through JSON.
"""

from __future__ import annotations

from typing import NewType

CallId = NewType("CallId", str)
"""Rollout / call correlation key. Pinned into a contextvar by the
dispatcher before invoking an impl, so `trace.emit()` from inside the
namespace inherits it automatically. Travels on `RemoteRequest.call_id`
(unary) and on Socket.IO stream/bidi frames."""

PackageName = NewType("PackageName", str)
"""A namespace's Python import path (e.g. `agentix.bash`). The
identity used by `Registry` for routing — there are no caller-chosen
namespaces. Equal to `NamespaceManifest.package`."""

MethodName = NewType("MethodName", str)
"""A method bound on a Dispatcher. For a `Namespace` subclass this is
the method name; for legacy function-stub namespaces it's the function
name. Travels on `RemoteRequest.method` and on stream/bidi frames."""

SandboxId = NewType("SandboxId", str)
"""Deployment-side handle for a running sandbox container. Returned
by `Deployment.start(...)` as `SandboxInfo.sandbox_id`; threaded back
through `Deployment.stop(...)`, `RolloutPool` bookkeeping, etc."""

__all__ = ["CallId", "MethodName", "PackageName", "SandboxId"]
