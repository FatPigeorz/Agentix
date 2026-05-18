"""Wire pieces shared by both runtime client and server.

The runtime is split three ways:

  * `agentix.runtime.shared`  — wire types, framing, codec, event-name
    constants. Imported by both sides. No imports back into `client/`
    or `server/`.
  * `agentix.runtime.client`  — orchestrator-side `RuntimeClient`.
  * `agentix.runtime.server`  — sandbox-side multiplexer, FastAPI
    app, Socket.IO server, namespace worker subprocess.

Submodules in this package:

  - `codec`   — msgpack pack/unpack + ext types (numpy, pydantic)
  - `events`  — Socket.IO event-name constants
  - `frames`  — stdio frame `type` / `kind` tag constants
  - `models`  — pydantic wire types (RemoteRequest, RemoteResponse, …)
  - `rpc`     — length-prefixed msgpack framing for worker stdio
  - `pump`    — per-bidi-call queue plumbing (used by both worker + SIO)
"""
