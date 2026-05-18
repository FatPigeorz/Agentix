"""Runtime worker subprocess.

The worker receives pickle-serialized callables and runs remote
calls over stdin/stdout using length-prefixed msgpack frames.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from typing import Any

from agentix.runtime.server.worker.invoker import CallableInvoker
from agentix.runtime.shared import frames as F
from agentix.runtime.shared import pump as _pump
from agentix.runtime.shared.callables import load_callable
from agentix.runtime.shared.framing import read_frame, write_frame
from agentix.runtime.shared.idents import CallId
from agentix.runtime.shared.models import RemoteError, RemoteRequest

logger = logging.getLogger("agentix.runtime.server.worker.process")


def _err(exc: BaseException) -> dict[str, Any]:
    return RemoteError(
        type=type(exc).__name__,
        message=str(exc),
        traceback=traceback.format_exc(),
    ).model_dump()


class Worker:
    """One process serving remote callable invocations."""

    def __init__(self) -> None:
        self._invoker = CallableInvoker()
        self._calls: dict[str, asyncio.Task] = {}
        self._bidi_queues: dict[str, asyncio.Queue] = {}
        self._bidi_intakes: dict[str, asyncio.Queue] = {}
        self._bidi_pumps: dict[str, asyncio.Task] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._shutdown = asyncio.Event()
        self._outbound_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._drainer: asyncio.Task | None = None

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

        self._drainer = loop.create_task(self._drain_outbound())
        await self._send({"type": F.READY})

        while not self._shutdown.is_set():
            try:
                frame = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break
            if frame is None:
                break
            await self._handle(frame)

        for task in list(self._calls.values()):
            task.cancel()
        if self._calls:
            await asyncio.gather(*self._calls.values(), return_exceptions=True)
        await self._outbound_q.join()
        if self._drainer is not None:
            self._drainer.cancel()

    async def _drain_outbound(self) -> None:
        assert self._writer is not None
        try:
            while True:
                frame = await self._outbound_q.get()
                try:
                    await write_frame(self._writer, frame)
                except Exception:
                    logger.exception("outbound frame write failed")
                finally:
                    self._outbound_q.task_done()
        except asyncio.CancelledError:
            pass

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._outbound_q.put(payload)

    _RUNTIME_FRAME_HANDLERS: dict[str, str] = {
        F.CALL: "_on_call",
        F.BIDI_IN: "_on_bidi_in",
        F.BIDI_END_IN: "_on_bidi_end_in",
    }

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if not isinstance(kind, str):
            logger.warning("worker: missing frame type")
            return
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
            callable_payload=frame["callable_payload"],
            display_name=frame["display_name"],
            shape=frame["shape"],
            args=frame.get("args") or [],
            kwargs=frame.get("kwargs") or {},
            call_id=CallId(call_id) if call_id else None,
        )
        if kind == F.KIND_UNARY:
            task = asyncio.create_task(self._run_unary(call_id, request))
        elif kind == F.KIND_STREAM:
            task = asyncio.create_task(self._run_stream(call_id, request))
        elif kind == F.KIND_BIDI:
            user_q: asyncio.Queue = asyncio.Queue(maxsize=_pump.DEFAULT_BIDI_BUFFER)
            intake_q: asyncio.Queue = asyncio.Queue()
            self._bidi_queues[call_id] = user_q
            self._bidi_intakes[call_id] = intake_q
            self._bidi_pumps[call_id] = asyncio.create_task(
                _pump.drain(intake_q, user_q, _END_SENTINEL)
            )
            task = asyncio.create_task(self._run_bidi(call_id, request, user_q))
        else:
            await self._send({
                "type": F.ERROR,
                "call_id": call_id,
                "error": RemoteError(
                    type="BadFrame",
                    message=f"unknown call kind {kind!r}",
                ).model_dump(),
            })
            return
        self._calls[call_id] = task
        task.add_done_callback(lambda _t: self._calls.pop(call_id, None))
        task.add_done_callback(lambda _t, cid=call_id: self._cleanup_bidi(cid))

    async def _forward_stream_event(self, call_id: str, event: dict[str, Any]) -> bool:
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

    def _callable_or_error(self, request: RemoteRequest) -> tuple[Any | None, dict[str, Any] | None]:
        try:
            return load_callable(request.callable_payload), None
        except Exception as exc:
            return None, _err(exc)

    async def _run_unary(self, call_id: str, request: RemoteRequest) -> None:
        fn, err = self._callable_or_error(request)
        if err is not None:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": err})
            return
        assert fn is not None
        try:
            resp = await self._invoker.call_unary(fn, request)
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
            return
        if resp.ok:
            await self._send({"type": F.RESULT, "call_id": call_id, "value": resp.value})
        else:
            await self._send({"type": F.ERROR, "call_id": call_id,
                              "error": (resp.error or RemoteError(type="Unknown", message="")).model_dump()})

    async def _run_stream(self, call_id: str, request: RemoteRequest) -> None:
        fn, err = self._callable_or_error(request)
        if err is not None:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": err})
            return
        assert fn is not None
        try:
            async for event in self._invoker.call_stream(fn, request):
                if await self._forward_stream_event(call_id, event):
                    return
            await self._send({"type": F.STREAM_END, "call_id": call_id})
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})

    async def _run_bidi(self, call_id: str, request: RemoteRequest, in_q: asyncio.Queue) -> None:
        fn, err = self._callable_or_error(request)
        if err is not None:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": err})
            return
        assert fn is not None
        adapter = self._invoker.input_adapter_for(fn, request)

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
            async for event in self._invoker.call_bidi(fn, request, _input_iter()):
                if await self._forward_stream_event(call_id, event):
                    return
            await self._send({"type": F.STREAM_END, "call_id": call_id})
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
        finally:
            try:
                in_q.put_nowait(_END_SENTINEL)
            except asyncio.QueueFull:
                pass

    async def _on_bidi_in(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        intake = self._bidi_intakes.get(call_id)
        if intake is None:
            return
        intake.put_nowait(frame.get("item"))

    async def _on_bidi_end_in(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        intake = self._bidi_intakes.get(call_id)
        if intake is None:
            return
        intake.put_nowait(_END_SENTINEL)

    def _cleanup_bidi(self, call_id: str) -> None:
        self._bidi_queues.pop(call_id, None)
        self._bidi_intakes.pop(call_id, None)
        _pump.cancel_if_running(self._bidi_pumps.pop(call_id, None))

    def _cancel(self, call_id: str) -> None:
        task = self._calls.get(call_id)
        if task is not None:
            task.cancel()
            asyncio.create_task(self._send({
                "type": F.ERROR,
                "call_id": call_id,
                "error": RemoteError(
                    type="Cancelled",
                    message="remote call cancelled",
                    cancelled=True,
                ).model_dump(),
            }))


_END_SENTINEL: Any = object()


async def _amain() -> None:
    worker = Worker()
    await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
