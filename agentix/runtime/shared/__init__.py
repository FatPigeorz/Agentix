"""Wire pieces shared by both runtime client and server.

The runtime is split three ways:

  * `agentix.runtime.shared`  — wire types, framing, codec, event-name
    constants. Imported by both sides. No imports back into `client/`
    or `server/`.
  * `agentix.runtime.client`  — orchestrator-side `RuntimeClient`.
  * `agentix.runtime.server`  — sandbox-side FastAPI app, Socket.IO
    server, and worker subprocess.

Submodules in this package:

  - `idents`   — branded NewType ids on the wire (CallId)
  - `rpc`      — caller-side variants (`Channel`, `Unary`, `Stream`, `Bidi`)
  - `codec`    — msgpack pack/unpack + ext types (numpy, pydantic)
  - `events`   — Socket.IO event-name constants
  - `frames`   — stdio frame `type` / `kind` tag constants
  - `framing`  — length-prefixed msgpack framing for worker stdio
  - `models`   — pydantic wire types (RemoteRequest, RemoteResponse, …)
  - `pump`     — per-bidi-call queue plumbing (used by both worker + SIO)
"""
