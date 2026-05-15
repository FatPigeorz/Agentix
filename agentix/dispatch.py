"""In-process closure dispatch.

A `Dispatcher` binds typed stub signatures to their impl callables. Closures
ship a `_register.register()` function that returns a populated Dispatcher.
The runtime imports each mounted closure's package, collects Dispatchers
into a `Registry`, and serves `POST /{ns}/_remote` by calling
`registry.get(ns).dispatch(request)` directly — no subprocess, no UDS,
no reverse proxy.

Serialization is driven by the stub's `inspect.signature`: each parameter's
annotation becomes a pydantic `TypeAdapter`, same for the return type.
Stubs use plain `def`/`async def` with `...` (Ellipsis) bodies — no
decorators, no base classes.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, ParamSpec, TypeVar, get_args, get_origin

from pydantic import TypeAdapter, ValidationError

import agentix.trace as trace
from agentix.idents import MethodName, PackageName
from agentix.namespace import Namespace, discover_methods
from agentix.runtime.models import (
    STREAM_ORIGINS,
    RemoteError,
    RemoteRequest,
    RemoteResponse,
)
from agentix.wire import (
    BidiPattern,
    StreamPattern,
    UnaryPattern,
    WirePattern,
    select_pattern,
)

logger = logging.getLogger("agentix.dispatch")

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _BoundMethod(Generic[P, R]):
    name: str
    stub: Callable[P, R]
    impl: Callable[..., Any]
    signature: inspect.Signature
    pattern: type[WirePattern]                    # wire pattern class (Unary/Stream/Bidi/…)
    param_adapters: dict[str, TypeAdapter[Any]]
    return_adapter: TypeAdapter[Any]
    item_adapter: TypeAdapter[Any] | None = None  # output item adapter (stream/bidi only)
    input_stream_param: str | None = None         # bidi: name of the AsyncIterator param
    input_item_adapter: TypeAdapter[Any] | None = None  # bidi: input item adapter

    @property
    def is_stream(self) -> bool:
        """True for stream and bidi — anything that emits a sequence of items."""
        return self.pattern is StreamPattern or self.pattern is BidiPattern

    @property
    def is_bidi(self) -> bool:
        return self.pattern is BidiPattern


class Dispatcher:
    """A namespace's collection of bound (stub, impl) pairs.

    Closures construct one of these in their `_register.register()`:

        from agentix.dispatch import Dispatcher
        from . import run               # the stub (Ellipsis body)
        from ._impl import run as _run  # the real impl

        def register() -> Dispatcher:
            d = Dispatcher()
            d.bind(run, _run)
            return d
    """

    def __init__(self) -> None:
        self._methods: dict[MethodName, _BoundMethod[Any, Any]] = {}

    def bind(
        self,
        stub: Callable[P, R],
        impl: Callable[..., R | Awaitable[R]],
    ) -> None:
        """Register `impl` as the implementation of `stub`.

        Both must share the same signature (the stub is just the typed
        contract; impl carries the body). The wire request's `method`
        field is `stub.__name__`. The `WirePattern` matching the stub's
        signature is selected at bind time and cached.
        """
        # eval_str=True resolves PEP 563 stringified annotations (`from
        # __future__ import annotations` in the stub module) — without it,
        # `param.annotation` would be the string "AsyncIterator[Foo]" and
        # `get_origin` would return None, mis-classifying streams as unary.
        sig = inspect.signature(stub, eval_str=True)
        name = MethodName(stub.__name__)
        if name in self._methods:
            raise ValueError(f"method '{name}' already bound on this dispatcher")
        pattern = select_pattern(sig)

        param_adapters: dict[str, TypeAdapter[Any]] = {}
        stream_params: list[tuple[str, type]] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            if get_origin(ann) in STREAM_ORIGINS:
                # AsyncIterator[T] params: adapter validates items, not the iterator
                args = get_args(ann)
                item_type = args[0] if args else Any
                stream_params.append((pname, item_type))
                param_adapters[pname] = TypeAdapter(item_type)
            else:
                param_adapters[pname] = TypeAdapter(ann)

        return_ann = sig.return_annotation if sig.return_annotation is not inspect.Signature.empty else Any
        item_adapter: TypeAdapter[Any] | None = None
        input_stream_param: str | None = None
        input_item_adapter: TypeAdapter[Any] | None = None
        if pattern is UnaryPattern:
            return_adapter = TypeAdapter(return_ann)
        elif pattern is StreamPattern:
            args = get_args(return_ann)
            item_type = args[0] if args else Any
            item_adapter = TypeAdapter(item_type)
            return_adapter = TypeAdapter(Any)  # unused on streaming path
        elif pattern is BidiPattern:
            args = get_args(return_ann)
            item_type = args[0] if args else Any
            item_adapter = TypeAdapter(item_type)
            # BidiPattern.matches already guaranteed exactly one stream param.
            input_stream_param, input_item_type = stream_params[0]
            input_item_adapter = TypeAdapter(input_item_type)
            return_adapter = TypeAdapter(Any)  # unused on streaming path
        else:
            # Custom pattern: framework doesn't know how to serialize. The
            # pattern owns wire framing (via its `bind` / `client_invoke` /
            # whatever server hook it registers). We store an `Any` adapter
            # so the dispatcher can still bind/coerce parameters; the return
            # path is the pattern's problem.
            return_adapter = TypeAdapter(Any)

        self._methods[name] = _BoundMethod(
            name=name,
            stub=stub,
            impl=impl,
            signature=sig,
            pattern=pattern,
            param_adapters=param_adapters,
            return_adapter=return_adapter,
            item_adapter=item_adapter,
            input_stream_param=input_stream_param,
            input_item_adapter=input_item_adapter,
        )

    def bind_namespace(self, cls: type[Namespace]) -> Dispatcher:
        """Bind every public method of `cls`.

        Closure methods are `@staticmethod` — the class is a namespace,
        method bodies carry the real logic, the signature is the
        contract. The dispatcher binds each function to itself (stub
        and impl are the same callable). No instance is needed.

        Returns `self` for fluent use in entry-point loaders.
        """
        for name, fn in discover_methods(cls):
            self.bind(fn, fn)
        return self

    def methods(self) -> list[MethodName]:
        return list(self._methods)

    def is_streaming(self, method: MethodName) -> bool:
        m = self._methods.get(method)
        return m is not None and m.is_stream

    def is_bidi(self, method: MethodName) -> bool:
        m = self._methods.get(method)
        return m is not None and m.is_bidi

    def input_adapter_for(self, method: MethodName) -> TypeAdapter[Any] | None:
        m = self._methods.get(method)
        return m.input_item_adapter if m else None

    async def dispatch(self, request: RemoteRequest) -> RemoteResponse:
        """Route a RemoteRequest to its bound impl, returning the wire response.

        Validates kwargs against the stub's signature, awaits async impls,
        serializes the return via the stub's return-type adapter, and
        traps exceptions into a RemoteError so the wire stays 200.
        """
        m = self._methods.get(request.method)
        if m is None:
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type="MethodNotFound",
                    message=f"method '{request.method}' is not bound on this dispatcher; "
                    f"available: {sorted(self._methods)}",
                ),
            )
        try:
            args, kwargs = self._coerce(m, request.args, request.kwargs)
        except ValidationError as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(type="ValidationError", message=str(exc)),
            )
        tokens = trace.set_call_context(request.call_id, _source_for(m.impl))
        try:
            try:
                result = m.impl(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                logger.exception("closure impl '%s' raised", m.name)
                return RemoteResponse(
                    ok=False,
                    error=RemoteError(
                        type=type(exc).__name__,
                        message=str(exc),
                        traceback=traceback.format_exc(),
                    ),
                )
        finally:
            trace.reset_call_context(tokens)
        try:
            value = m.return_adapter.dump_python(result, mode="json")
        except Exception as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type="SerializationError",
                    message=f"failed to serialize return value: {exc}",
                ),
            )
        return RemoteResponse(ok=True, value=value)

    async def dispatch_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        """Run a server-streaming impl, yielding event dicts to the transport.

        Event shapes:
            {"item": <serialized>}      — per yielded value
            {"error": {...}}            — impl raised, validation failed, etc.
            {"end": true}               — normal completion sentinel

        The transport (Socket.IO server / HTTP NDJSON) encodes the dicts to
        the wire. The dispatcher only deals with semantic events.
        """
        m = self._methods.get(request.method)
        if m is None:
            yield {"error": RemoteError(
                type="MethodNotFound",
                message=f"method '{request.method}' is not bound on this dispatcher; "
                f"available: {sorted(self._methods)}",
            ).model_dump()}
            return
        if not m.is_stream or m.is_bidi:
            yield {"error": RemoteError(
                type="NotAStreamingMethod",
                message=f"method '{request.method}' is not a (non-bidi) streaming method",
            ).model_dump()}
            return
        try:
            args, kwargs = self._coerce(m, request.args, request.kwargs)
        except ValidationError as exc:
            yield {"error": RemoteError(type="ValidationError", message=str(exc)).model_dump()}
            return
        tokens = trace.set_call_context(request.call_id, _source_for(m.impl))
        try:
            try:
                result = m.impl(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                assert m.item_adapter is not None
                async for item in result:
                    try:
                        value = m.item_adapter.dump_python(item, mode="json")
                    except Exception as exc:
                        yield {"error": RemoteError(
                            type="SerializationError",
                            message=f"failed to serialize item: {exc}",
                        ).model_dump()}
                        return
                    yield {"item": value}
            except Exception as exc:
                logger.exception("closure stream impl '%s' raised mid-stream", m.name)
                yield {"error": RemoteError(
                    type=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                ).model_dump()}
                return
        finally:
            trace.reset_call_context(tokens)
        yield {"end": True}

    async def dispatch_bidi(
        self,
        request: RemoteRequest,
        input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a bidi impl. `input_iter` yields items already coerced to the
        stub's input item type (transport pre-validates via `input_item_adapter`).

        Event shapes match `dispatch_stream` — same vocab of `item` / `end`
        / `error` — so the transport handles them uniformly.
        """
        m = self._methods.get(request.method)
        if m is None:
            yield {"error": RemoteError(
                type="MethodNotFound",
                message=f"method '{request.method}' is not bound on this dispatcher; "
                f"available: {sorted(self._methods)}",
            ).model_dump()}
            return
        if not m.is_bidi:
            yield {"error": RemoteError(
                type="NotABidiMethod",
                message=f"method '{request.method}' is not bidirectional",
            ).model_dump()}
            return
        assert m.input_stream_param is not None
        # Bind non-stream args/kwargs; inject input_iter as the stream param.
        non_stream_kwargs = dict(request.kwargs)
        non_stream_kwargs.pop(m.input_stream_param, None)
        try:
            bound = m.signature.bind_partial(*request.args, **non_stream_kwargs)
            bound.apply_defaults()
            coerced: dict[str, Any] = {}
            for pname, raw in bound.arguments.items():
                if pname == m.input_stream_param:
                    continue
                adapter = m.param_adapters.get(pname)
                coerced[pname] = adapter.validate_python(raw) if adapter is not None else raw
            coerced[m.input_stream_param] = input_iter
        except (TypeError, ValidationError) as exc:
            yield {"error": RemoteError(type=type(exc).__name__, message=str(exc)).model_dump()}
            return
        tokens = trace.set_call_context(request.call_id, _source_for(m.impl))
        try:
            try:
                result = m.impl(**coerced)
                if inspect.isawaitable(result):
                    result = await result
                assert m.item_adapter is not None
                async for item in result:
                    try:
                        value = m.item_adapter.dump_python(item, mode="json")
                    except Exception as exc:
                        yield {"error": RemoteError(
                            type="SerializationError",
                            message=f"failed to serialize item: {exc}",
                        ).model_dump()}
                        return
                    yield {"item": value}
            except Exception as exc:
                logger.exception("closure bidi impl '%s' raised mid-stream", m.name)
                yield {"error": RemoteError(
                    type=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                ).model_dump()}
                return
        finally:
            trace.reset_call_context(tokens)
        yield {"end": True}

    @staticmethod
    def _coerce(
        m: _BoundMethod[Any, Any],
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """Bind args/kwargs against the stub signature, coercing each through
        its parameter's TypeAdapter (pydantic does dataclass/BaseModel/JSON
        round-tripping). Defaults are filled from the stub.
        """
        bound = m.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        coerced: dict[str, Any] = {}
        for pname, raw in bound.arguments.items():
            adapter = m.param_adapters.get(pname)
            coerced[pname] = adapter.validate_python(raw) if adapter is not None else raw
        # Re-split into args / kwargs in original order for the impl call.
        out_args: list[Any] = []
        out_kwargs: dict[str, Any] = {}
        for pname, param in m.signature.parameters.items():
            if pname not in coerced:
                continue
            v = coerced[pname]
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                out_args.append(v)
            elif param.kind is inspect.Parameter.VAR_POSITIONAL:
                out_args.extend(v)
            elif param.kind is inspect.Parameter.VAR_KEYWORD:
                out_kwargs.update(v)
            else:  # KEYWORD_ONLY
                out_kwargs[pname] = v
        return out_args, out_kwargs


def _source_for(impl: Callable[..., Any]) -> PackageName | None:
    """Derive a closure package path from an impl function for trace events."""
    mod = getattr(impl, "__module__", None)
    if mod is None:
        return None
    return PackageName(mod)


# The entry-point group every closure declares under in its pyproject.toml:
#
#   [project.entry-points."agentix.closure"]
#   bash = "agentix.bash:Bash"
#
# The framework reads this at startup via `importlib.metadata.entry_points`.
CLOSURE_ENTRY_POINT_GROUP = "agentix.closure"


@dataclass
class _Entry:
    """One registered closure. `dispatcher` is built lazily on first use.

    `loader` returns the closure's `Namespace` subclass on demand. For
    entry-point-discovered closures it's `ep.load`; for test fixtures it's
    a pre-bound `lambda: cls`. Either way, the actual import + dispatcher
    build is deferred until `get_or_load(...)` is awaited.
    """

    loader: Callable[[], type]
    dist_name: str | None = None
    dist_version: str | None = None
    dispatcher: Dispatcher | None = None
    error: Exception | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Registry:
    """Per-runtime collection of package-path → closure entry.

    Closures are discovered via `importlib.metadata.entry_points(group=
    "agentix.closure")` at sandbox-startup time, but their Python packages
    are not imported and their Dispatchers are not built until the first
    call to `get_or_load(package)`. This keeps sandbox boot cheap and
    isolates per-closure import failures so they surface on call rather
    than at startup.

    The closure's Python import path (e.g. 'agentix.bash') is the routing
    key — there are no caller-chosen namespaces.
    """

    def __init__(self) -> None:
        self._entries: dict[PackageName, _Entry] = {}

    def register(
        self,
        package: PackageName,
        loader: Callable[[], type],
        *,
        dist_name: str | None = None,
        dist_version: str | None = None,
    ) -> None:
        """Mark a closure as known but not yet loaded.

        `loader()` must return the closure's `Namespace` subclass. The
        registry calls it lazily on first dispatch. `dist_name` and
        `dist_version` come from `importlib.metadata` and are surfaced
        via `/closures` for introspection.
        """
        if package in self._entries:
            raise ValueError(f"package '{package}' already registered")
        self._entries[package] = _Entry(
            loader=loader,
            dist_name=dist_name,
            dist_version=dist_version,
        )

    def register_entry_point(self, ep: Any) -> None:
        """Register a `importlib.metadata.EntryPoint`. Convenience for the
        normal entry-point discovery path.
        """
        package = PackageName(ep.value.split(":", 1)[0])
        dist_name = getattr(ep.dist, "name", None) if ep.dist else None
        dist_version = getattr(ep.dist, "version", None) if ep.dist else None
        self.register(
            package,
            loader=ep.load,
            dist_name=dist_name,
            dist_version=dist_version,
        )

    async def get_or_load(self, package: PackageName) -> Dispatcher | None:
        """Return the dispatcher for `package`, building it on first call.

        Returns None for unknown packages. Re-raises the original
        exception on every call if the load has previously failed.
        Concurrent first calls to the same package serialise on a
        per-entry lock so the loader + bind sequence runs once.
        """
        entry = self._entries.get(package)
        if entry is None:
            return None
        if entry.dispatcher is not None:
            return entry.dispatcher
        if entry.error is not None:
            raise entry.error
        async with entry.lock:
            if entry.dispatcher is not None:
                return entry.dispatcher
            if entry.error is not None:
                raise entry.error
            try:
                cls = entry.loader()
                if not isinstance(cls, type):
                    raise TypeError(
                        f"{package}: entry-point loader returned "
                        f"{type(cls).__name__}, expected a class"
                    )
                if Namespace not in cls.__mro__ or cls is Namespace:
                    raise TypeError(
                        f"{package}: {cls.__name__} is not a Namespace subclass"
                    )
                entry.dispatcher = Dispatcher().bind_namespace(cls)
            except Exception as exc:
                logger.exception("lazy-load failed for closure '%s'", package)
                entry.error = exc
                raise
            return entry.dispatcher

    def packages(self) -> list[PackageName]:
        """All known packages — registered, regardless of load state."""
        return list(self._entries)

    def loaded_packages(self) -> list[PackageName]:
        """Packages whose dispatcher has been built (post first-use)."""
        return [pkg for pkg, e in self._entries.items() if e.dispatcher is not None]

    def info_for(self, package: PackageName) -> tuple[str | None, str | None] | None:
        """`(dist_name, dist_version)` for the closure, or None if not registered."""
        e = self._entries.get(package)
        if e is None:
            return None
        return e.dist_name, e.dist_version

    def __contains__(self, package: PackageName) -> bool:
        return package in self._entries


def discover_entry_points() -> list[Any]:
    """Return every installed `agentix.closure` entry point.

    Cheap: walks `importlib.metadata` dist metadata; nothing is imported.
    The framework uses this at sandbox-startup to populate the Registry
    without paying the import cost of every closure.
    """
    eps = importlib.metadata.entry_points()
    # Python 3.10+: SelectableGroups with .select(); earlier: dict.
    if hasattr(eps, "select"):
        return list(eps.select(group=CLOSURE_ENTRY_POINT_GROUP))
    return list(eps.get(CLOSURE_ENTRY_POINT_GROUP, []))  # type: ignore[attr-defined]
