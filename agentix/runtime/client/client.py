"""Async client for the agentix runtime server.

Wraps:
  - typed remote-call dispatch: `RuntimeClient.remote(fn, *args, **kwargs)`,
    where `fn` is a stub function imported from a namespace's Python package.
    Routing key is `fn.__module__`; result is decoded into `fn`'s return type.
    Shell exec / file I/O live in the `bash` and `files` primitive namespaces —
    call them via `c.remote(bash.Bash.run, ...)` and `c.remote(files.Files.upload, ...)`.
  - `/namespaces` introspection and `/health`.
  - log subscription: `RuntimeClient.logs()` is an `AsyncIterator[LogRecord]`
    fed by a Socket.IO `log` event stream; same for `RuntimeClient.traces()`.

Two transports underneath:
  - HTTP for unary RPC (`POST /_remote`).
  - Socket.IO for server-streaming, bidirectional, and log/trace subscription.

The Socket.IO connection is lazy and shared across all stream/bidi/log calls
on the same client. Per-`call_id` queue routing demultiplexes concurrent calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from typing import (
    Any,
    ParamSpec,
    TypeVar,
    get_args,
    get_origin,
    overload,
)

import httpx
import socketio
from pydantic import TypeAdapter

from agentix.runtime.events import (
    BIDI_END,
    BIDI_END_IN,
    BIDI_ERROR,
    BIDI_IN,
    BIDI_OUT,
    BIDI_START,
    CANCEL,
    LOG,
    LOGS_SUBSCRIBE,
    LOGS_UNSUBSCRIBE,
    STREAM,
    STREAM_END,
    STREAM_ERROR,
    STREAM_ITEM,
    TRACE,
    TRACES_SUBSCRIBE,
    TRACES_UNSUBSCRIBE,
)
from agentix.runtime.models import (
    STREAM_ORIGINS,
    HealthResponse,
    LogRecord,
    NamespaceInfo,
    RemoteError,
    RemoteRequest,
    RemoteResponse,
    TraceEvent,
)
from agentix.wire import select_pattern

logger = logging.getLogger("agentix.client")

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")


class RemoteCallError(RuntimeError):
    """Raised when a remote namespace impl returns a non-ok RemoteResponse,
    or when a stream/bidi call surfaces an `error` event from the wire."""

    def __init__(self, package: str, method: str, error: RemoteError):
        super().__init__(f"{package}.{method}: {error.type}: {error.message}")
        self.package = package
        self.method = method
        self.error = error


class RuntimeClient:
    """Async client for the agentix runtime server."""

    def __init__(self, base_url: str, timeout: float = 300):
        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        # Socket.IO bookkeeping — created lazily on first stream/bidi/log call.
        self._sio: socketio.AsyncClient | None = None
        self._sio_lock = asyncio.Lock()
        # call_id -> event queue. Stream and bidi share the same machinery.
        self._pending: dict[str, asyncio.Queue] = {}
        # log + trace subscribers — each subscriber has its own queue.
        self._log_subscribers: set[asyncio.Queue] = set()
        self._trace_subscribers: set[asyncio.Queue] = set()

    # ── lifecycle ────────────────────────────────────────────────

    async def close(self):
        if self._sio is not None and self._sio.connected:
            with contextlib.suppress(BaseException):
                await self._sio.disconnect()
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── runtime server endpoints ─────────────────────────────────

    async def health(self) -> HealthResponse:
        r = await self._client.get("/health")
        r.raise_for_status()
        return HealthResponse.model_validate(r.json())

    async def namespaces(self) -> list[NamespaceInfo]:
        r = await self._client.get("/namespaces")
        r.raise_for_status()
        return [NamespaceInfo.model_validate(x) for x in r.json()]

    # ── typed remote call ────────────────────────────────────────

    @overload
    def remote(
        self,
        fn: Callable[P, AsyncIterator[T]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> AsyncIterator[T]: ...

    @overload
    def remote(
        self,
        fn: Callable[P, AsyncGenerator[T, Any]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> AsyncIterator[T]: ...

    @overload
    def remote(
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Coroutine[Any, Any, R]: ...

    def remote(self, fn, *args, **kwargs):
        """Execute `fn` in the sandbox and return its typed result.

        Dispatch is polymorphic on the stub's signature — a `WirePattern`
        is selected via `agentix.wire.select_pattern(sig)` and that
        pattern owns the wire framing (HTTP for unary, Socket.IO for
        stream/bidi, or anything a third-party pattern registers).

        The three built-in patterns:
          * Output `AsyncIterator[T]` + one `AsyncIterator[U]` parameter
            → `BidiPattern`; returns `AsyncIterator[T]`.
          * Output `AsyncIterator[T]`, no streaming parameters →
            `StreamPattern`; returns `AsyncIterator[T]`.
          * Otherwise → `UnaryPattern`; returns `Coroutine[..., R]`;
            caller `await`s it.
        """
        # eval_str=True: stubs declared with `from __future__ import
        # annotations` would otherwise expose string annotations here,
        # mis-routing stream/bidi shapes to UnaryPattern.
        sig = inspect.signature(fn, eval_str=True)
        pattern = select_pattern(sig)()
        pattern.bind(sig)
        return pattern.client_invoke(self, fn, sig, args, kwargs)

    async def _remote_unary(self, fn, return_ann, *args, **kwargs):
        package = fn.__module__
        method = fn.__name__
        sig = inspect.signature(fn)
        body = RemoteRequest(
            package=package, method=method,
            args=_encode_args(sig, args), kwargs=_encode_kwargs(sig, kwargs),
        )
        r = await self._client.post("/_remote", json=body.model_dump())
        r.raise_for_status()
        resp = RemoteResponse.model_validate(r.json())
        if not resp.ok:
            assert resp.error is not None
            raise RemoteCallError(package=package, method=method, error=resp.error)
        if return_ann is inspect.Signature.empty:
            return resp.value
        return TypeAdapter(return_ann).validate_python(resp.value)

    async def _remote_stream(self, fn, sig, *args, **kwargs):
        package = fn.__module__
        method = fn.__name__
        sio = await self._ensure_sio()
        call_id = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._pending[call_id] = q

        ret_args = get_args(sig.return_annotation)
        item_adapter = TypeAdapter(ret_args[0] if ret_args else Any)
        try:
            await sio.emit(STREAM, {
                "call_id": call_id,
                "package": package,
                "method": method,
                "args": _encode_args(sig, args),
                "kwargs": _encode_kwargs(sig, kwargs),
            })
            while True:
                kind, data = await q.get()
                if kind == "end":
                    return
                if kind == "error":
                    err = RemoteError.model_validate(data["error"])
                    raise RemoteCallError(package=package, method=method, error=err)
                if kind == "item":
                    yield item_adapter.validate_python(data["value"])
        finally:
            self._pending.pop(call_id, None)
            with contextlib.suppress(BaseException):
                await sio.emit(CANCEL, {"call_id": call_id})

    async def _remote_bidi(self, fn, sig, *args, **kwargs):
        """Bidi over Socket.IO: client emits `bidi:start`, streams inputs as
        `bidi:in`, signals end-of-input with `bidi:end_in`. Server replies via
        `bidi:out` / `bidi:end` / `bidi:error` correlated by `call_id`.
        """
        package = fn.__module__
        method = fn.__name__

        # Identify the input-stream param.
        stream_param: str | None = None
        in_item_type: Any = Any
        for pname, param in sig.parameters.items():
            if get_origin(param.annotation) in STREAM_ORIGINS:
                stream_param = pname
                in_args = get_args(param.annotation)
                in_item_type = in_args[0] if in_args else Any
                break
        if stream_param is None:
            raise TypeError(f"{package}.{method}: signature has no AsyncIterator parameter")

        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        input_iter = bound.arguments.pop(stream_param, None)
        if input_iter is None or not hasattr(input_iter, "__aiter__"):
            raise TypeError(
                f"{package}.{method}: argument '{stream_param}' must be an "
                f"AsyncIterator (got {type(input_iter).__name__})"
            )

        non_stream_kwargs = _encode_kwargs(sig, dict(bound.arguments))
        out_args = get_args(sig.return_annotation)
        out_adapter = TypeAdapter(out_args[0] if out_args else Any)
        in_adapter = TypeAdapter(in_item_type)

        sio = await self._ensure_sio()
        call_id = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._pending[call_id] = q

        await sio.emit(BIDI_START, {
            "call_id": call_id, "package": package, "method": method,
            "args": [], "kwargs": non_stream_kwargs,
        })

        async def _sender() -> None:
            try:
                async for item in input_iter:
                    encoded = in_adapter.dump_python(item, mode="json")
                    await sio.emit(BIDI_IN, {"call_id": call_id, "item": encoded})
                await sio.emit(BIDI_END_IN, {"call_id": call_id})
            except Exception:
                # outer loop will see an error event or close
                pass

        sender = asyncio.create_task(_sender())
        try:
            while True:
                kind, data = await q.get()
                if kind == "end":
                    return
                if kind == "error":
                    err = RemoteError.model_validate(data["error"])
                    raise RemoteCallError(package=package, method=method, error=err)
                if kind == "item":
                    yield out_adapter.validate_python(data["value"])
        finally:
            sender.cancel()
            with contextlib.suppress(BaseException):
                await sender
            self._pending.pop(call_id, None)
            with contextlib.suppress(BaseException):
                await sio.emit(CANCEL, {"call_id": call_id})

    # ── log subscription ────────────────────────────────────────

    async def logs(self, *, filter: str | None = None) -> AsyncIterator[LogRecord]:
        """Subscribe to the runtime's log stream.

        Yields a `LogRecord` for every `logging` record emitted under
        the `agentix.*` logger tree (or the `filter` prefix if given).
        Iteration ends when the connection closes or the caller breaks.
        """
        sio = await self._ensure_sio()
        sub_q: asyncio.Queue = asyncio.Queue()
        self._log_subscribers.add(sub_q)
        first_sub = len(self._log_subscribers) == 1
        try:
            if first_sub:
                payload = {"filter": filter} if filter else {}
                await sio.emit(LOGS_SUBSCRIBE, payload)
            while True:
                data = await sub_q.get()
                yield LogRecord.model_validate(data)
        finally:
            self._log_subscribers.discard(sub_q)
            if not self._log_subscribers:
                with contextlib.suppress(BaseException):
                    await sio.emit(LOGS_UNSUBSCRIBE, {})

    async def traces(
        self,
        *,
        kind: str | None = None,
        call_id: str | None = None,
    ) -> AsyncIterator[TraceEvent]:
        """Subscribe to the runtime's trace stream.

        Yields a `TraceEvent` for every `agentix.trace.emit(...)` from any
        namespace. Optional `kind` and `call_id` filters are applied
        client-side; the server broadcasts all events to subscribers.
        Iteration ends when the connection closes or the caller breaks.
        """
        sio = await self._ensure_sio()
        sub_q: asyncio.Queue = asyncio.Queue()
        self._trace_subscribers.add(sub_q)
        first_sub = len(self._trace_subscribers) == 1
        try:
            if first_sub:
                await sio.emit(TRACES_SUBSCRIBE, {})
            while True:
                data = await sub_q.get()
                if kind is not None and data.get("kind") != kind:
                    continue
                if call_id is not None and data.get("call_id") != call_id:
                    continue
                yield TraceEvent.model_validate(data)
        finally:
            self._trace_subscribers.discard(sub_q)
            if not self._trace_subscribers:
                with contextlib.suppress(BaseException):
                    await sio.emit(TRACES_UNSUBSCRIBE, {})

    # ── Socket.IO connection management ─────────────────────────

    async def _ensure_sio(self) -> socketio.AsyncClient:
        if self._sio is not None and self._sio.connected:
            return self._sio
        async with self._sio_lock:
            if self._sio is not None and self._sio.connected:
                return self._sio
            sio = socketio.AsyncClient()

            async def _on_stream_item(data): await self._dispatch_event("item", data)
            async def _on_stream_end(data):  await self._dispatch_event("end", data)
            async def _on_stream_error(data): await self._dispatch_event("error", data)
            async def _on_bidi_out(data):   await self._dispatch_event("item", data)
            async def _on_bidi_end(data):   await self._dispatch_event("end", data)
            async def _on_bidi_error(data): await self._dispatch_event("error", data)

            sio.on(STREAM_ITEM, _on_stream_item)
            sio.on(STREAM_END, _on_stream_end)
            sio.on(STREAM_ERROR, _on_stream_error)
            sio.on(BIDI_OUT, _on_bidi_out)
            sio.on(BIDI_END, _on_bidi_end)
            sio.on(BIDI_ERROR, _on_bidi_error)
            sio.on(LOG, self._on_log)
            sio.on(TRACE, self._on_trace)

            await sio.connect(self._base_url)
            self._sio = sio
            return sio

    async def _dispatch_event(self, kind: str, data: dict[str, Any]) -> None:
        call_id = data.get("call_id")
        q = self._pending.get(call_id) if isinstance(call_id, str) else None
        if q is not None:
            await q.put((kind, data))

    async def _on_log(self, data: dict[str, Any]) -> None:
        for q in list(self._log_subscribers):
            q.put_nowait(data)

    async def _on_trace(self, data: dict[str, Any]) -> None:
        for q in list(self._trace_subscribers):
            q.put_nowait(data)

    # Shell exec / file I/O are not in the runtime core. Mount the `bash`
    # and `files` primitive namespaces and dispatch through `c.remote(...)`.


def _encode_args(sig: inspect.Signature, args: tuple) -> list[Any]:
    """Encode positional args via each parameter's TypeAdapter so dataclasses,
    BaseModels and other pydantic-known types serialise to JSON-compatible
    structures. Falls back to the raw value if no annotation is available.
    """
    out: list[Any] = []
    params = list(sig.parameters.values())
    for i, v in enumerate(args):
        ann = (
            params[i].annotation
            if i < len(params) and params[i].annotation is not inspect.Parameter.empty
            else Any
        )
        try:
            out.append(TypeAdapter(ann).dump_python(v, mode="json"))
        except Exception:
            out.append(v)
    return out


def _encode_kwargs(sig: inspect.Signature, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Like `_encode_args` but for keyword args, looked up by parameter name."""
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        param = sig.parameters.get(k)
        ann = (
            param.annotation
            if param is not None and param.annotation is not inspect.Parameter.empty
            else Any
        )
        try:
            out[k] = TypeAdapter(ann).dump_python(v, mode="json")
        except Exception:
            out[k] = v
    return out


