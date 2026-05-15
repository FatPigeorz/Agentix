"""Sandbox-side runtime server.

Composes FastAPI (for HTTP RPC + LLM proxy) and Socket.IO (for streams,
bidi, and log subscription) into the ASGI app uvicorn runs. Imports each
mounted namespace's Python package lazily on first call.

Submodules:
  - `app`         — FastAPI app, lifespan, Registry, /_remote unary dispatch
  - `sio`         — Socket.IO server + event handlers + log forwarding
  - `llm_proxy`   — reverse-proxy `/_llm/<provider>/<path>` to upstream LLM APIs
  - `trace_bridge` — pipes `agentix.trace.emit(...)` to the Socket.IO `trace` room

Shell exec and file I/O moved out of the core: they ship as the `bash`
and `files` primitive namespaces under `primitives/`. Invoke via
`c.remote(bash.Bash.run, ...)` / `c.remote(files.Files.upload, ...)`.

Public names re-exported here so legacy imports keep working:
  `from agentix.runtime.server import app, main, registry`
  `await agentix.runtime.server._auto_load()`  (used by tests)
"""

from agentix.runtime.server.app import (
    _auto_load,
    app,
    main,
    registry,
)

__all__ = [
    "_auto_load",
    "app",
    "main",
    "registry",
]
