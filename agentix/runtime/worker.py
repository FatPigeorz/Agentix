"""Namespace worker — `python -m agentix.runtime.worker --target <pkg>`.

A worker is a single-namespace dispatch process that the runtime
multiplexer spawns lazily on first call. It loads ONE namespace target
(typically a Python package, but a `module:attr` form is also accepted
for class-style or partial targets), binds it via `Dispatcher`, and
serves dispatch over stdin/stdout using the RPC frame protocol in
`agentix.runtime.rpc`.

The worker holds:

  - one `Dispatcher` (the loaded namespace's bound methods)
  - one asyncio task per in-flight call, keyed by `call_id`
  - one input queue per in-flight bidi call (for `bidi_in` chunks)

It forwards trace events via a subscriber that wraps each
`trace.emit()` into a frame on stdout. Logs go through Python's
logging to stderr, which the multiplexer captures separately.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import sys
import traceback
from typing import Any

from agentix import trace
from agentix.dispatch import Dispatcher
from agentix.runtime import frames as F
from agentix.runtime.models import RemoteError, RemoteRequest
from agentix.runtime.rpc import read_frame, write_frame

logger = logging.getLogger("agentix.runtime.worker")


def _load_target(target: str) -> Any:
    """Resolve `target` to a Python object.

    Two forms (setuptools entry-point grammar):
      * `module.path`        → the module itself
      * `module.path:attr`   → `getattr(module, attr)` — a class or any obj

    The dispatcher duck-types whatever comes back; the bare-module form
    is the recommended namespace shape.
    """
    if ":" in target:
        mod_name, attr_name = target.split(":", 1)
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr_name)
    return importlib.import_module(target)


def _err(exc: BaseException) -> dict[str, Any]:
    return RemoteError(
        type=type(exc).__name__,
        message=str(exc),
        traceback=traceback.format_exc(),
    ).model_dump()


class Worker:
    """One worker, one namespace. Owns stdio + a Dispatcher."""

    def __init__(self, dispatcher: Dispatcher, package: str) -> None:
        self._dispatcher = dispatcher
        self._package = package
        self._send_lock = asyncio.Lock()
        self._calls: dict[str, asyncio.Task] = {}
        self._bidi_queues: dict[str, asyncio.Queue] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
        )
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer,
        )
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        self._reader, self._writer = reader, writer

        # Subscribe trace forwarder before we say "ready" — any boot-time
        # trace events get captured.
        trace.subscribe(self._trace_handler)

        await self._send({"type": F.READY, "package": self._package})

        while not self._shutdown.is_set():
            try:
                frame = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break
            if frame is None:
                break
            await self._handle(frame)

        # Drain in-flight calls on shutdown.
        for task in list(self._calls.values()):
            task.cancel()
        if self._calls:
            await asyncio.gather(*self._calls.values(), return_exceptions=True)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._writer is not None
        async with self._send_lock:
            await write_frame(self._writer, payload)

    def _trace_handler(self, kind: str, payload: dict, call_id, source) -> None:
        # Sync handler; schedule the actual frame write on the loop. Per
        # the `agentix.trace` contract, handler errors are caught upstream
        # — we only need to avoid raising.
        frame = {"type": F.TRACE, "kind": kind, "payload": payload}
        if call_id is not None:
            frame["call_id"] = call_id
        if source is not None:
            frame["source"] = source
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send(frame))

    _RUNTIME_FRAME_HANDLERS: dict[str, str] = {
        F.CALL: "_on_call",
        F.BIDI_IN: "_on_bidi_in",
        F.BIDI_END_IN: "_on_bidi_end_in",
    }

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        handler_name = self._RUNTIME_FRAME_HANDLERS.get(kind)
        if handler_name is not None:
            await getattr(self, handler_name)(frame)
        elif kind == F.CANCEL:
            self._cancel(frame.get("call_id", ""))
        elif kind == F.SHUTDOWN:
            self._shutdown.set()
        else:
            logger.warning("worker: unknown frame type %r", kind)

    async def _on_call(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        kind = frame.get("kind", F.KIND_UNARY)
        request = RemoteRequest(
            package=self._package,
            method=frame["method"],
            args=frame.get("args") or [],
            kwargs=frame.get("kwargs") or {},
            call_id=call_id,
        )
        if kind == F.KIND_UNARY:
            task = asyncio.create_task(self._run_unary(call_id, request))
        elif kind == F.KIND_STREAM:
            task = asyncio.create_task(self._run_stream(call_id, request))
        elif kind == F.KIND_BIDI:
            in_q: asyncio.Queue = asyncio.Queue(maxsize=64)
            self._bidi_queues[call_id] = in_q
            task = asyncio.create_task(self._run_bidi(call_id, request, in_q))
        else:
            await self._send({
                "type": F.ERROR, "call_id": call_id,
                "error": RemoteError(type="BadFrame", message=f"unknown call kind {kind!r}").model_dump(),
            })
            return
        self._calls[call_id] = task
        task.add_done_callback(lambda _t: self._calls.pop(call_id, None))
        task.add_done_callback(lambda _t: self._bidi_queues.pop(call_id, None))

    async def _forward_stream_event(self, call_id: str, event: dict[str, Any]) -> bool:
        """Map a Dispatcher stream/bidi event to its wire frame; return True
        if the event was terminal (caller should stop iterating)."""
        kind = event.get("type")
        if kind == "item":
            await self._send({"type": F.STREAM_ITEM, "call_id": call_id, "value": event["value"]})
            return False
        if kind == "error":
            await self._send({"type": F.ERROR, "call_id": call_id, "error": event["error"]})
            return True
        if kind == "end":
            await self._send({"type": F.STREAM_END, "call_id": call_id})
            return True
        return False

    async def _run_unary(self, call_id: str, request: RemoteRequest) -> None:
        try:
            resp = await self._dispatcher.dispatch(request)
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
            return
        if resp.ok:
            await self._send({"type": F.RESULT, "call_id": call_id, "value": resp.value})
        else:
            await self._send({"type": F.ERROR, "call_id": call_id,
                              "error": (resp.error or RemoteError(type="Unknown", message="")).model_dump()})

    async def _run_stream(self, call_id: str, request: RemoteRequest) -> None:
        try:
            async for event in self._dispatcher.dispatch_stream(request):
                if await self._forward_stream_event(call_id, event):
                    return
            await self._send({"type": F.STREAM_END, "call_id": call_id})
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})

    async def _run_bidi(self, call_id: str, request: RemoteRequest, in_q: asyncio.Queue) -> None:
        adapter = self._dispatcher.input_adapter_for(request.method)

        async def _input_iter():
            while True:
                item = await in_q.get()
                if item is _END_SENTINEL:
                    return
                if adapter is not None:
                    try:
                        item = adapter.validate_python(item)
                    except Exception as exc:
                        await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
                        return
                yield item

        try:
            async for event in self._dispatcher.dispatch_bidi(request, _input_iter()):
                if await self._forward_stream_event(call_id, event):
                    return
            await self._send({"type": F.STREAM_END, "call_id": call_id})
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
        finally:
            # Unblock the input iterator if the impl exited early.
            try:
                in_q.put_nowait(_END_SENTINEL)
            except asyncio.QueueFull:
                pass

    async def _on_bidi_in(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        q = self._bidi_queues.get(call_id)
        if q is None:
            return
        await q.put(frame.get("item"))

    async def _on_bidi_end_in(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        q = self._bidi_queues.get(call_id)
        if q is None:
            return
        await q.put(_END_SENTINEL)

    def _cancel(self, call_id: str) -> None:
        task = self._calls.get(call_id)
        if task is not None:
            task.cancel()


# Module-level singleton used to signal "end of bidi input" through the
# input queue. Compared with `is`; the same object reference must be
# visible from both _on_bidi_end_in (pusher) and _run_bidi (consumer).
_END_SENTINEL: Any = object()


def _make_dispatcher(target: str) -> tuple[Dispatcher, str]:
    obj = _load_target(target)
    dispatcher = Dispatcher().bind_namespace(obj)
    # Routing key: the module the target lives in. For a bare-module
    # target the module IS the object; for `module:attr` we take the
    # module path so caller-side `fn.__module__` matches.
    package = obj.__name__ if inspect.ismodule(obj) else obj.__module__
    return dispatcher, package


async def _amain(target: str) -> None:
    try:
        dispatcher, package = _make_dispatcher(target)
    except Exception as exc:
        # Worker hasn't initialized stdio framing yet; bootstrap a minimal
        # writer so the multiplexer learns why we're exiting.
        from agentix.runtime.rpc import pack_frame
        sys.stdout.buffer.write(pack_frame({"type": F.BOOT_ERROR, "error": _err(exc)}))
        sys.stdout.buffer.flush()
        sys.exit(1)
    worker = Worker(dispatcher, package)
    await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(prog="agentix.runtime.worker")
    parser.add_argument(
        "--target", required=True,
        help="namespace to load — `module.path` (recommended, package-as-namespace) "
             "or `module.path:attr` (class-style or partial target)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args.target))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
