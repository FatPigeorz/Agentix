"""Branded string identifiers.

`NewType` aliases for the `str`s that appear in the wire layer and are
easy to confuse: a namespace's import path, a method name bound in a
Dispatcher, and an RPC call correlation key. They're plain `str` at
runtime (zero cost) but pyright treats them as distinct, so swapping one
for another becomes a type error.

Pydantic v2 understands `NewType` — fields annotated `CallId` validate
as `str` and round-trip cleanly through JSON.
"""

from __future__ import annotations

from typing import NewType

CallId = NewType("CallId", str)
"""RPC call correlation key carried on `RemoteRequest.call_id` and
Socket.IO stream/bidi frames."""

PackageName = NewType("PackageName", str)
"""A namespace's Python import path (e.g. `agentix.bash`). The
identity used for routing. Equal to `NamespaceManifest.package`."""

MethodName = NewType("MethodName", str)
"""A method bound on a Dispatcher. Travels on `RemoteRequest.method`
and on stream/bidi frames."""

__all__ = ["CallId", "MethodName", "PackageName"]
