"""agentix.sio — Socket.IO namespace API, sandbox side.

Mirrors `socketio.AsyncClientNamespace` but pipe-bridged: the worker
subprocess can't speak SIO directly, so emit/on go through frames on
the worker → server stdout pipe and the server replays them on its
real SIO server.

Three reserved namespace paths are owned by agentix-core:

  - `/`       — RPC (call / cancel / call:result / call:error)
  - `/trace`  — Trace/Span lifecycle
  - `/log`    — stdlib `logging` records

Plugins MUST use their own namespace path (convention: `/<package-name>`),
typically registered via `agentix.register_namespace(MyNs())`. Two
plugins on the same namespace path conflict and the second registration
raises.

User shape:

    class MyService(agentix.Namespace):
        namespace = "/my-plugin"

        async def on_hello(self, data):
            await self.emit("hello:result", {"echo": data})

        async def fetch_remote(self, payload):
            return await self.request("fetch", payload)

    agentix.register_namespace(MyService())
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("agentix.sio")

# Handlers may be sync or async, and may return anything (we just check
# `iscoroutine` on the return value to decide whether to await). Bound
# methods picked up by `_auto_register` slot in here too — narrowing
# this further would require introspecting every user's `on_<event>`.
Handler = Callable[[Any], Any]


RESERVED_NAMESPACES = frozenset({"/", "/trace", "/log"})


class RemoteSioError(RuntimeError):
    """Raised by `Namespace.request()` when the reply carries an `:error`."""

    def __init__(self, type_: str, message: str) -> None:
        super().__init__(f"{type_}: {message}")
        self.type = type_
        self.message = message


# ── module-level bridge state ──────────────────────────────────────


class _Bridge:
    """Holds the pipe write callback + the registry of Namespace instances.

    The runtime worker boot calls `_install(send)`; from then on,
    Namespace instances route their emits through `send`.
    """

    def __init__(self) -> None:
        self._send: Callable[[dict[str, Any]], None] | None = None
        self._namespaces: dict[str, Namespace] = {}

    def install(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send = send

    def is_installed(self) -> bool:
        return self._send is not None

    def send_frame(self, frame: dict[str, Any]) -> None:
        if self._send is None:
            raise RuntimeError(
                "agentix.sio is not installed — only callable from inside a sandbox runtime worker.",
            )
        self._send(frame)

    def register(self, ns: Namespace) -> None:
        path = ns.namespace
        if path in self._namespaces:
            raise ValueError(f"namespace {path!r} already registered")
        self._namespaces[path] = ns
        # Tell the server this namespace exists so it forwards inbound
        # events on this path back to the worker.
        if self.is_installed():
            self.send_frame({"type": "sio_open", "namespace": path})

    def lookup(self, path: str) -> Namespace | None:
        return self._namespaces.get(path)

    def dispatch_inbound(self, namespace: str, event: str, data: Any) -> None:
        ns = self._namespaces.get(namespace)
        if ns is None:
            return
        ns._dispatch(event, data)


_bridge = _Bridge()


# ── Namespace base class ──────────────────────────────────────────


class Namespace:
    """Sandbox-side namespace handler.

    Subclass and override `namespace = "/path"` plus any `on_<event>`
    methods. Then call `agentix.register_namespace(instance)`.

    Auto-registration of `on_<event>` handlers happens at construction
    time for any method whose name starts with `on_`. Events with names
    that aren't valid Python identifiers (e.g. `"fetch:result"`) must
    be registered explicitly via `self.on("fetch:result", handler)`.
    """

    namespace: str = "/"  # subclass MUST override

    def __init__(self, namespace: str | None = None) -> None:
        if namespace is not None:
            self.namespace = namespace
        if not isinstance(self.namespace, str) or not self.namespace.startswith("/"):
            raise ValueError(f"namespace must start with '/' (got {self.namespace!r})")
        if self.namespace in RESERVED_NAMESPACES and type(self) is Namespace:
            raise ValueError(
                f"namespace {self.namespace!r} is reserved by agentix-core",
            )
        self._handlers: dict[str, list[Handler]] = {}
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._auto_register()

    def _auto_register(self) -> None:
        for attr_name in dir(self):
            if not attr_name.startswith("on_"):
                continue
            attr = getattr(self, attr_name, None)
            if not callable(attr):
                continue
            event = attr_name[3:]
            if event:
                self._handlers.setdefault(event, []).append(attr)

    # ── public API ───────────────────────────────────────────────

    async def emit(self, event: str, data: Any = None) -> None:
        """Emit `event` on this namespace to all connected hosts."""
        _bridge.send_frame(
            {
                "type": "sio_emit",
                "namespace": self.namespace,
                "event": event,
                "data": data,
            }
        )

    def on(self, event: str, handler: Handler) -> None:
        """Register an additional handler for `event`."""
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Handler) -> None:
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def request(
        self,
        event: str,
        data: Any = None,
        *,
        timeout: float = 300.0,
    ) -> Any:
        """Round-trip helper: emit `event` with an auto-generated
        request_id, await reply on `event:result` (success) or
        `event:error` (failure) with matching id.

        The host-side handler MUST emit one of those two reply events
        carrying `request_id` and either `value` or `error`.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_requests[req_id] = fut

        result_event = f"{event}:result"
        error_event = f"{event}:error"
        self._ensure_reply_handlers(result_event, error_event)

        try:
            await self.emit(event, {"request_id": req_id, "data": data})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_requests.pop(req_id, None)

    def _ensure_reply_handlers(self, result_event: str, error_event: str) -> None:
        if result_event not in self._handlers:
            self.on(result_event, self._on_reply_success)
        if error_event not in self._handlers:
            self.on(error_event, self._on_reply_error)

    async def _on_reply_success(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        req_id = payload.get("request_id")
        fut = self._pending_requests.get(req_id) if isinstance(req_id, str) else None
        if fut is not None and not fut.done():
            fut.set_result(payload.get("value"))

    async def _on_reply_error(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        req_id = payload.get("request_id")
        fut = self._pending_requests.get(req_id) if isinstance(req_id, str) else None
        if fut is not None and not fut.done():
            err = payload.get("error") or {"type": "Unknown", "message": ""}
            fut.set_exception(
                RemoteSioError(
                    err.get("type", "Unknown"),
                    err.get("message", ""),
                )
            )

    # ── inbound dispatch (called by the bridge) ─────────────────

    def _dispatch(self, event: str, data: Any) -> None:
        handlers = list(self._handlers.get(event, ()))
        if not handlers:
            return
        for h in handlers:
            try:
                result = h(data)
            except Exception:
                logger.exception(
                    "namespace %s handler for %r raised",
                    self.namespace,
                    event,
                )
                continue
            if asyncio.iscoroutine(result):
                asyncio.create_task(_swallow_exc(result, self.namespace, event))


async def _swallow_exc(coro: Awaitable[None], namespace: str, event: str) -> None:
    try:
        await coro
    except Exception:
        logger.exception(
            "namespace %s coroutine handler for %r raised",
            namespace,
            event,
        )


# ── public surface ────────────────────────────────────────────────


def register_namespace(ns: Namespace) -> None:
    """Register a Namespace instance with the sandbox-side bridge.

    After registration, the namespace's inbound events are routed to
    this instance, and `ns.emit(...)` flows to all connected hosts.
    """
    _bridge.register(ns)


# ── runtime-internal hooks (not part of public surface) ───────────


def _install(send: Callable[[dict[str, Any]], None]) -> None:
    _bridge.install(send)
    # Replay any namespaces that were registered before install (rare —
    # happens if module-level code registers at import time).
    for ns in list(_bridge._namespaces.values()):
        _bridge.send_frame({"type": "sio_open", "namespace": ns.namespace})


def _dispatch_inbound(namespace: str, event: str, data: Any) -> None:
    _bridge.dispatch_inbound(namespace, event, data)


def _is_installed() -> bool:
    return _bridge.is_installed()


__all__ = [
    "RESERVED_NAMESPACES",
    "Namespace",
    "RemoteSioError",
    "register_namespace",
]
