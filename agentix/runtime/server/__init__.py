"""Sandbox-side runtime server.

Composes FastAPI (for `/health`) and Socket.IO (for unary, stream, and
bidi calls) into the ASGI app uvicorn runs. Remote calls route to one
runtime worker subprocess.

Submodules:
  - `app`         — FastAPI app, lifespan, /health
  - `sio`         — Socket.IO server + remote-call event handlers
  - `worker`      — worker client, subprocess entry point, callable invocation
"""

from agentix.runtime.server.app import app, main

__all__ = [
    "app",
    "main",
]
