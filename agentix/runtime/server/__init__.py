"""Sandbox-side runtime server.

Composes FastAPI (for HTTP unary `/_remote`) and Socket.IO (for streams
+ bidi) into the ASGI app uvicorn runs. Every dispatch routes to a
per-namespace worker subprocess via the `NamespaceMultiplexer`.

Submodules:
  - `app`         — FastAPI app, lifespan, /_remote unary dispatch
  - `sio`         — Socket.IO server + stream/bidi event handlers
  - `multiplexer` — package→worker routing, on-demand registration
  - `worker`      — per-namespace subprocess entry point
"""

from agentix.runtime.server.app import (
    _multiplexer,
    app,
    main,
)

# `multiplexer` alias for tests that want to introspect or register
# in-process namespaces against the live runtime.
multiplexer = _multiplexer

__all__ = [
    "app",
    "main",
    "multiplexer",
]
