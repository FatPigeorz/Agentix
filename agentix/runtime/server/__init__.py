"""Sandbox-side runtime server.

Composes FastAPI (for HTTP unary `/_remote`) and Socket.IO (for streams
+ bidi) into the ASGI app uvicorn runs. Remote calls route to one
runtime worker subprocess.

Submodules:
  - `app`         — FastAPI app, lifespan, /_remote unary calls
  - `sio`         — Socket.IO server + stream/bidi event handlers
  - `worker_client` — server-side bridge to the worker process
  - `worker`        — worker subprocess entry point
"""

from agentix.runtime.server.app import (
    _worker,
    app,
    main,
)

# `worker` alias for tests that want to select the in-process backend.
worker = _worker

__all__ = [
    "app",
    "main",
    "worker",
]
