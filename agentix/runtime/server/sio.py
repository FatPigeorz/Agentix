"""Socket.IO transport for the agentix runtime.

Two responsibilities:

1. The RPC protocol on the default `/` namespace — `call` / `cancel`
   / `call:result` / `call:error`.

2. Dynamic namespace forwarding. When a worker-side `agentix.Namespace`
   registers via the pipe (`sio_open` frame), this layer registers a
   matching SIO server namespace that forwards inbound events back to
   the worker. Outbound `sio_emit` frames become real SIO broadcasts on
   the corresponding namespace.

Reserved namespace paths (claimed by agentix-core): `/`, `/trace`,
`/log`. Plugins use their own paths (typically `/<package-name>`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import socketio
from pydantic import ValidationError

from agentix.runtime.server.worker import RuntimeWorkerClient
from agentix.runtime.shared.callables import RemoteCallable
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.idents import CallId
from agentix.runtime.shared.models import RemoteError, RemoteRequest

logger = logging.getLogger("agentix.runtime.sio")


def _u(data: Any) -> dict:
    if not data:
        return {}
    return unpack(bytes(data)) or {}


def _decode(raw: Any) -> Any:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif isinstance(raw, bytearray):
        raw = bytes(raw)
    if isinstance(raw, bytes):
        return unpack(raw)
    return raw


@dataclass
class _CallState:
    task: asyncio.Task


@dataclass
class _SessionState:
    calls: dict[str, _CallState] = field(default_factory=dict)


def make_sio(
    worker: RuntimeWorkerClient,
) -> tuple[socketio.AsyncServer, socketio.ASGIApp]:
    # `namespaces='*'` accepts connects on any namespace path. Plugin
    # namespaces are registered lazily by the worker (`sio_open` frame
    # in response to `agentix.register_namespace(...)`); the host may
    # connect to them before the forwarder is in place. Inbound events
    # are dropped until the forwarder registers, which is what we want.
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        namespaces="*",
    )
    sessions: dict[str, _SessionState] = {}
    opened_namespaces: set[str] = set()  # paths the worker has opened

    @sio.event
    async def connect(sid: str, environ: dict, auth: Any = None) -> None:
        sessions[sid] = _SessionState()
        logger.debug("sio connect %s", sid)

    @sio.event
    async def disconnect(sid: str) -> None:
        sess = sessions.pop(sid, None)
        if sess is None:
            return
        for call in sess.calls.values():
            call.task.cancel()
        await _drain_tasks([c.task for c in sess.calls.values()])
        logger.debug("sio disconnect %s", sid)

    # ── RPC on `/` ───────────────────────────────────────────────

    async def on_call(sid: str, data: Any) -> None:
        sess = sessions.get(sid)
        if sess is None:
            return
        payload = _u(data)
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            await sio.emit(
                "call:error",
                pack(
                    {
                        "call_id": "",
                        "error": {"type": "BadRequest", "message": "missing call_id"},
                    }
                ),
                to=sid,
            )
            return

        async def _drive() -> None:
            try:
                request = RemoteRequest(
                    callable=RemoteCallable(payload["callable"]),
                    arguments=payload["arguments"],
                    call_id=CallId(call_id),
                )
            except (KeyError, ValidationError) as exc:
                await sio.emit(
                    "call:error",
                    pack(
                        {
                            "call_id": call_id,
                            "error": RemoteError(type=type(exc).__name__, message=str(exc)).model_dump(),
                        }
                    ),
                    to=sid,
                )
                return
            resp = await worker.call(request)
            if resp.ok:
                await sio.emit("call:result", pack({"call_id": call_id, "value": resp.value}), to=sid)
            else:
                error = (resp.error or RemoteError(type="Unknown", message="")).model_dump()
                await sio.emit("call:error", pack({"call_id": call_id, "error": error}), to=sid)

        task = asyncio.create_task(_drive())
        sess.calls[call_id] = _CallState(task=task)
        task.add_done_callback(lambda _t: sess.calls.pop(call_id, None))

    async def on_cancel(sid: str, data: Any) -> None:
        sess = sessions.get(sid)
        if sess is None:
            return
        payload = _u(data)
        call_id = payload.get("call_id")
        call = sess.calls.pop(call_id, None) if isinstance(call_id, str) else None
        if call is not None:
            call.task.cancel()
            await sio.emit(
                "call:error",
                pack(
                    {
                        "call_id": call_id,
                        "error": RemoteError(
                            type="Cancelled",
                            message="remote call cancelled",
                            cancelled=True,
                        ).model_dump(),
                    }
                ),
                to=sid,
            )

    # ── dynamic namespace forwarding ─────────────────────────────
    #
    # `sio_open`  — worker tells us a namespace exists; we register a
    #               catch-all SIO handler that forwards every inbound
    #               event on that namespace back to the worker.
    # `sio_emit`  — worker wants to broadcast an event on a namespace;
    #               we pack the payload and call sio.emit there.

    _broadcast_tasks: set[asyncio.Task] = set()

    def _on_worker_sio_frame(frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        namespace = frame.get("namespace")
        if not isinstance(namespace, str) or not namespace.startswith("/"):
            return
        if kind == "sio_emit":
            event = frame.get("event")
            if not isinstance(event, str):
                return
            task = asyncio.create_task(
                sio.emit(event, pack(frame.get("data")), namespace=namespace),
            )
            _broadcast_tasks.add(task)
            task.add_done_callback(_broadcast_tasks.discard)
        elif kind == "sio_open":
            if namespace in opened_namespaces or namespace == "/":
                return
            opened_namespaces.add(namespace)
            _register_namespace(namespace)

    def _register_namespace(namespace: str) -> None:
        """Register a SIO server namespace that forwards every inbound
        event back to the worker via the pipe."""

        class _Forwarder(socketio.AsyncNamespace):
            async def trigger_event(self, event: str, *args: Any) -> Any:
                # Skip lifecycle events (connect/disconnect/connect_error)
                # — those are SIO-internal, not user-emitted data.
                if event in ("connect", "disconnect", "connect_error"):
                    return
                # args = (sid, data?)  — server namespaces pass sid first.
                data = _decode(args[1]) if len(args) >= 2 else None
                await worker.send_inbound(namespace, event, data)

        sio.register_namespace(_Forwarder(namespace))

    worker.set_sio_handler(_on_worker_sio_frame)

    # Register RPC handlers on `/` non-decorator-style — `@sio.on(name)`
    # decorates by side effect and pyright can't tell that the wrapped
    # function is still usable.
    sio.on("call", on_call)
    sio.on("cancel", on_cancel)

    # Pre-register core namespaces so the host can connect to them
    # immediately — the worker subscribes lazily, but the SIO server
    # must already accept the connection.
    for core_ns in ("/trace", "/log"):
        opened_namespaces.add(core_ns)
        _register_namespace(core_ns)

    asgi_app = socketio.ASGIApp(sio, socketio_path="/socket.io")
    return sio, asgi_app


async def _drain_tasks(tasks: list[asyncio.Task]) -> None:
    if not tasks:
        return
    for t in tasks:
        if not t.done():
            t.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.gather(*tasks, return_exceptions=True)
