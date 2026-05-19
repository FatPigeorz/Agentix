"""Host-side namespace helpers.

`AsyncClientNamespace` is a thin subclass of `socketio.AsyncClientNamespace`
that msgpack-wraps event payloads — so plugin authors write
`await self.emit("x", {"a": 1})` and `async def on_x(self, data)`, and
the bytes/msgpack wire format stays internal.
"""

from __future__ import annotations

from typing import Any

import socketio

from agentix.runtime.shared.codec import pack, unpack


def _decode(raw: Any) -> Any:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif isinstance(raw, bytearray):
        raw = bytes(raw)
    if isinstance(raw, bytes):
        return unpack(raw)
    return raw


class AsyncClientNamespace(socketio.AsyncClientNamespace):
    """`socketio.AsyncClientNamespace` with msgpack at the boundary.

    Override `on_<event>` for inbound; call `await self.emit(...)` for
    outbound. Data is plain Python — packing happens automatically.
    """

    async def emit(self, event: str, data: Any = None, **kwargs: Any) -> Any:
        return await super().emit(event, pack(data), **kwargs)

    async def trigger_event(self, event: str, *args: Any) -> Any:
        # Unpack the single data payload (socketio always emits one arg
        # for a bytes event). Lifecycle events (`connect`, `disconnect`)
        # come with no data and we let them pass through untouched.
        if args and isinstance(args[0], (bytes, bytearray, memoryview)):
            args = (_decode(args[0]),) + args[1:]
        return await super().trigger_event(event, *args)


__all__ = ["AsyncClientNamespace"]
