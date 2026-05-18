"""`FunctionInvoker` — validates and invokes remote callables.

The runtime receives a pickle-serialized callable, resolves its
signature, builds pydantic adapters, coerces wire args, calls the
callable, serializes outputs, and traps exceptions into `RemoteError` so
the wire stays in-band.
"""

from __future__ import annotations

import inspect
import logging
import traceback
from collections.abc import AsyncIterator
from typing import Any, get_args

from pydantic import TypeAdapter, ValidationError

from agentix.invoke.bound import _BoundMethod, coerce_args
from agentix.invoke.shape import detect_shape
from agentix.runtime.shared.models import (
    RemoteError,
    RemoteRequest,
    RemoteResponse,
)
from agentix.runtime.shared.rpc import is_channel_annotation

logger = logging.getLogger("agentix.invoke")


class FunctionInvoker:
    """Call one pickle-resolved Python callable."""

    def _build(self, name: str, fn: Any) -> _BoundMethod[Any, Any]:
        # eval_str=True resolves PEP 563 stringified annotations
        # (`from __future__ import annotations` in the module) — without
        # it, `param.annotation` would be a string and `get_origin` would
        # return None, mis-classifying streams as unary.
        sig = inspect.signature(fn, eval_str=True)
        shape = detect_shape(fn, sig)

        param_adapters: dict[str, TypeAdapter[Any]] = {}
        channel_params: list[tuple[str, Any]] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            if is_channel_annotation(ann):
                # Channel[T] params: adapter validates items, not the channel itself.
                args = get_args(ann)
                item_type = args[0] if args else Any
                channel_params.append((pname, item_type))
                param_adapters[pname] = TypeAdapter(item_type)
            else:
                param_adapters[pname] = TypeAdapter(ann)

        return_ann = sig.return_annotation if sig.return_annotation is not inspect.Signature.empty else Any
        item_adapter: TypeAdapter[Any] | None = None
        input_channel_param: str | None = None
        input_item_adapter: TypeAdapter[Any] | None = None
        if shape == "unary":
            return_adapter = TypeAdapter(return_ann)
        else:
            # Stream + bidi both serialise items via the return type's T.
            args = get_args(return_ann)
            item_type = args[0] if args else Any
            item_adapter = TypeAdapter(item_type)
            return_adapter = TypeAdapter(Any)  # unused on streaming path
            if shape == "bidi":
                input_channel_param, input_item_type = channel_params[0]
                input_item_adapter = TypeAdapter(input_item_type)

        return _BoundMethod(
            name=name,
            stub=fn,
            impl=fn,
            signature=sig,
            shape=shape,
            param_adapters=param_adapters,
            return_adapter=return_adapter,
            item_adapter=item_adapter,
            input_channel_param=input_channel_param,
            input_item_adapter=input_item_adapter,
        )

    # ── call entry points ─────────────────────────────────────────

    async def call_unary(self, fn: Any, request: RemoteRequest) -> RemoteResponse:
        """Invoke a unary callable and serialize its return value."""
        m = self._build(request.display_name, fn)
        if m.shape != request.shape:
            return RemoteResponse(ok=False, error=_shape_error(request.display_name, request.shape, m.shape))
        if m.shape != "unary":
            return RemoteResponse(ok=False, error=RemoteError(
                type="NotAUnaryCallable",
                message=f"callable {request.display_name!r} is {m.shape}, not unary",
            ))
        try:
            args, kwargs = coerce_args(m, request.args, request.kwargs)
        except ValidationError as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(type="ValidationError", message=str(exc)),
            )
        try:
            result = m.impl(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.exception("remote callable '%s' raised", m.name)
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                ),
            )
        try:
            value = m.return_adapter.dump_python(result, mode="python")
        except Exception as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type="SerializationError",
                    message=f"failed to serialize return value: {exc}",
                ),
            )
        return RemoteResponse(ok=True, value=value)

    async def call_stream(self, fn: Any, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        """Server-streaming call. Yields {item|end|error} event dicts."""
        m = self._build(request.display_name, fn)
        if m.shape != request.shape:
            yield {"type": "error", "error": _shape_error(request.display_name, request.shape, m.shape).model_dump()}
            return
        if not m.is_stream or m.is_bidi:
            yield {"type": "error", "error": RemoteError(
                type="NotAStreamFunction",
                message=f"callable {request.display_name!r} is not a non-bidi streaming callable",
            ).model_dump()}
            return
        try:
            args, kwargs = coerce_args(m, request.args, request.kwargs)
        except ValidationError as exc:
            yield {"type": "error", "error": RemoteError(type="ValidationError", message=str(exc)).model_dump()}
            return
        try:
            result = m.impl(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            assert m.item_adapter is not None
            async for item in result:
                try:
                    value = m.item_adapter.dump_python(item, mode="python")
                except Exception as exc:
                    yield {"type": "error", "error": RemoteError(
                        type="SerializationError",
                        message=f"failed to serialize item: {exc}",
                    ).model_dump()}
                    return
                yield {"type": "item", "value": value}
        except Exception as exc:
            logger.exception("remote stream callable '%s' raised mid-stream", m.name)
            yield {"type": "error", "error": RemoteError(
                type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            ).model_dump()}
            return
        yield {"type": "end"}

    async def call_bidi(
        self,
        fn: Any,
        request: RemoteRequest,
        input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Bidirectional call. `input_iter` is the caller-pushed inbound stream;
        items must already match the impl's `Channel[T]` item type."""
        m = self._build(request.display_name, fn)
        if m.shape != request.shape:
            yield {"type": "error", "error": _shape_error(request.display_name, request.shape, m.shape).model_dump()}
            return
        if not m.is_bidi:
            yield {"type": "error", "error": RemoteError(
                type="NotABidiFunction",
                message=f"callable {request.display_name!r} is not bidirectional",
            ).model_dump()}
            return
        assert m.input_channel_param is not None
        # Bind non-channel args/kwargs; inject input_iter as the channel param.
        non_channel_kwargs = dict(request.kwargs)
        non_channel_kwargs.pop(m.input_channel_param, None)
        try:
            bound = m.signature.bind_partial(*request.args, **non_channel_kwargs)
            bound.apply_defaults()
            coerced: dict[str, Any] = {}
            for pname, raw in bound.arguments.items():
                if pname == m.input_channel_param:
                    continue
                adapter = m.param_adapters.get(pname)
                coerced[pname] = adapter.validate_python(raw) if adapter is not None else raw
            coerced[m.input_channel_param] = input_iter
        except (TypeError, ValidationError) as exc:
            yield {"type": "error", "error": RemoteError(type=type(exc).__name__, message=str(exc)).model_dump()}
            return
        try:
            result = m.impl(**coerced)
            if inspect.isawaitable(result):
                result = await result
            assert m.item_adapter is not None
            async for item in result:
                try:
                    value = m.item_adapter.dump_python(item, mode="python")
                except Exception as exc:
                    yield {"type": "error", "error": RemoteError(
                        type="SerializationError",
                        message=f"failed to serialize item: {exc}",
                    ).model_dump()}
                    return
                yield {"type": "item", "value": value}
        except Exception as exc:
            logger.exception("remote bidi callable '%s' raised mid-stream", m.name)
            yield {"type": "error", "error": RemoteError(
                type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            ).model_dump()}
            return
        yield {"type": "end"}

    def input_adapter_for(self, fn: Any, request: RemoteRequest) -> TypeAdapter[Any] | None:
        m = self._build(request.display_name, fn)
        return m.input_item_adapter


def _shape_error(display_name: str, expected: str, actual: str) -> RemoteError:
    return RemoteError(
        type="ShapeMismatch",
        message=f"callable {display_name!r} was sent as {expected!r} but resolved as {actual!r}",
    )


__all__ = ["FunctionInvoker"]
