"""Agentix runtime server.

Runs remote calls through one runtime worker subprocess.

Endpoints:

- `POST /_remote` — typed unary calls (one request -> one response)
- Socket.IO at `/socket.io/` — server-streaming + bidi, correlated by
  `call_id`
- `GET /health`

Remote requests carry a stdlib pickle-serialized callable. Module-level
functions/classes and pickleable callable objects are the supported
boundary.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from agentix import __version__
from agentix.runtime.server.sio import make_sio
from agentix.runtime.server.worker_client import RuntimeWorkerClient
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.models import HealthResponse, RemoteRequest

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker: RuntimeWorkerClient = app.state.worker
    try:
        yield
    finally:
        await worker.shutdown()


# Worker client is constructed here so tests can replace it via app.state
# before the lifespan kicks in.
_worker = RuntimeWorkerClient()

_fastapi_app = FastAPI(title="agentix", version=__version__, lifespan=lifespan)
_fastapi_app.state.worker = _worker


# ── Health & inventory ──────────────────────────────────────────


@_fastapi_app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


# ── Remote calls ────────────────────────────────────────────────


@_fastapi_app.post("/_remote")
async def remote_call(request: Request) -> Response:
    """Unary remote-call endpoint. Spawns the worker on first call.

    Body: msgpack-encoded `{"callable_payload", "display_name", "shape", "args", "kwargs", "call_id"}`.
    Response: msgpack-encoded `RemoteResponse` dict. Always 200 — error
    info lives in the response body (`{"ok": false, "error": {...}}`).
    Streaming + bidi methods live on the Socket.IO connection instead.
    """
    body = await request.body()
    raw = unpack(body)
    req = RemoteRequest.model_validate(raw)
    worker: RuntimeWorkerClient = _fastapi_app.state.worker
    resp = await worker.call_unary(req)
    return Response(content=pack(resp.model_dump(mode="python")),
                    media_type="application/msgpack")


# ── Compose ASGI app: FastAPI for HTTP, Socket.IO for streams ──
#
# The combined ASGI app is what uvicorn runs as
# `agentix.runtime.server:app`. `socketio.ASGIApp` routes `/socket.io/*`
# to the Socket.IO server and everything else to FastAPI.

import socketio as _socketio  # noqa: E402

_sio, _ = make_sio(_worker)
app = _socketio.ASGIApp(_sio, _fastapi_app, socketio_path="/socket.io")
app.fastapi = _fastapi_app  # type: ignore[attr-defined]
app.state = _fastapi_app.state  # type: ignore[attr-defined]
app.sio = _sio  # type: ignore[attr-defined]


# ── Entry point (the bundle image's server command) ─────────


def main() -> None:
    """Entry point exposed as the `agentix-server` console script. Port
    via AGENTIX_BIND_PORT (env, default 8000); dev shell can override via
    --port.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="agentix runtime server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENTIX_BIND_PORT", "8000")),
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-port", type=int, default=5678)
    parser.add_argument("--debug-wait", action="store_true")
    args = parser.parse_args()

    if args.debug:
        import debugpy  # type: ignore[reportMissingImports]

        debugpy.listen(("0.0.0.0", args.debug_port))
        print(f"debugpy listening on 0.0.0.0:{args.debug_port}")
        if args.debug_wait:
            print("Waiting for debugger to attach...")
            debugpy.wait_for_client()

    uvicorn.run("agentix.runtime.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
