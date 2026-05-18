"""Branded string identifiers used by the runtime wire layer."""

from __future__ import annotations

from typing import NewType

CallId = NewType("CallId", str)
"""RPC call correlation key carried on `RemoteRequest.call_id` and
Socket.IO stream/bidi frames."""

__all__ = ["CallId"]
