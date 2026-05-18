"""Runtime subpackage — split into three sides.

  * `agentix.runtime.shared`  — wire types, framing, codec, event-name
    constants. Both client and server depend on this; nothing here
    depends on `client/` or `server/`.
  * `agentix.runtime.client`  — orchestrator-side `RuntimeClient`
    (HTTP for unary; Socket.IO for stream / bidi).
  * `agentix.runtime.server`  — sandbox-side: FastAPI app, Socket.IO
    server, the `RuntimeWorkerClient`, and the `worker` subprocess
    (`python -m agentix.runtime.server.worker`).

Importing this top-level package does NOT eagerly import `client` or
`server` — that would create a circular path through callable-shape
helpers
when other modules pull wire types from `agentix.runtime.shared.models`.
Reach for the leaf you need explicitly, e.g.
`from agentix.runtime.client import RuntimeClient`, or use the
top-level re-exports on `agentix`.
"""
