"""Wire patterns — call-shape protocols.

A `WirePattern` owns one call shape (unary / server-streaming / bidi)
end to end: how to detect it from a stub's signature, how the server
runs it, and how the client invokes it. The three built-ins are
exhaustive — bidi if the stub takes and returns an `AsyncIterator`,
stream if it just returns one, unary otherwise. If a fourth shape
ever becomes necessary, add it here; there is no extension hook.

The Dispatcher caches the matched pattern instance on each bound
method; pattern lookup happens at `bind` time, not per call.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, get_args, get_origin

from agentix.runtime.models import STREAM_ORIGINS

if TYPE_CHECKING:
    from agentix.runtime.client.client import RuntimeClient


class WirePattern(ABC):
    """Strategy object for one call shape.

    Concrete subclasses must implement `matches`, `server_invoke`, and
    `client_invoke`. They may carry per-method state on the instance
    (e.g. pre-built `TypeAdapter`s) — one pattern instance per bound
    method.

    The `name` is the wire-protocol tag: the event-name prefix on the
    Socket.IO side (e.g. `stream`, `bidi`), or a future protocol tag.
    Two patterns must not share a name.
    """

    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        """Return True if a stub with this signature uses this pattern."""

    @abstractmethod
    def bind(self, sig: inspect.Signature) -> None:
        """Pre-compute per-method state (type adapters, stream params, …).

        Called once at `Dispatcher.bind` time. Subsequent calls reuse
        the cached state.
        """

    @abstractmethod
    def client_invoke(
        self,
        client: RuntimeClient,
        fn: Callable[..., Any],
        sig: inspect.Signature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Awaitable[Any] | AsyncIterator[Any]:
        """Invoke `fn` over the wire from the client side.

        Returns either:
          * an `Awaitable[R]` for unary patterns — caller `await`s it
          * an `AsyncIterator[T]` for streaming-style patterns — caller
            iterates with `async for`

        The pattern owns the wire framing: it picks the transport
        (HTTP `/_remote`, Socket.IO `stream`, custom event names) and
        handles correlation, type coercion, and error mapping.
        """


class UnaryPattern(WirePattern):
    """Request/response. The default — used when no other pattern matches.

    Wire: `POST /_remote` with JSON body, JSON response.
    """

    name = "unary"

    @classmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        return True  # fallback — always last in the registry

    def bind(self, sig: inspect.Signature) -> None:
        return  # adapters are computed in Dispatcher._BoundMethod for now

    def client_invoke(
        self,
        client: RuntimeClient,
        fn: Callable[..., Any],
        sig: inspect.Signature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Awaitable[Any]:
        return client._remote_unary(fn, sig.return_annotation, *args, **kwargs)


class StreamPattern(WirePattern):
    """Server-streaming. Stub returns `AsyncIterator[T]`, no streaming params.

    Wire: Socket.IO `stream` event → `stream:item` × N + `stream:end`
    (or `stream:error`).
    """

    name = "stream"

    @classmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        if get_origin(sig.return_annotation) not in STREAM_ORIGINS:
            return False
        # no AsyncIterator parameters — that's bidi
        for p in sig.parameters.values():
            if get_origin(p.annotation) in STREAM_ORIGINS:
                return False
        return True

    def bind(self, sig: inspect.Signature) -> None:
        return

    def client_invoke(
        self,
        client: RuntimeClient,
        fn: Callable[..., Any],
        sig: inspect.Signature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> AsyncIterator[Any]:
        return client._remote_stream(fn, sig, *args, **kwargs)


class BidiPattern(WirePattern):
    """Bidirectional streaming. Stub returns `AsyncIterator[U]` and takes
    exactly one `AsyncIterator[T]` parameter.

    Wire: Socket.IO `bidi:start` → `bidi:in` × N (client→server)
    interleaved with `bidi:out` × M (server→client) → `bidi:end`
    (or `bidi:error`).
    """

    name = "bidi"

    @classmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        if get_origin(sig.return_annotation) not in STREAM_ORIGINS:
            return False
        stream_params = [
            p for p in sig.parameters.values()
            if get_origin(p.annotation) in STREAM_ORIGINS
        ]
        return len(stream_params) == 1

    def bind(self, sig: inspect.Signature) -> None:
        return

    def client_invoke(
        self,
        client: RuntimeClient,
        fn: Callable[..., Any],
        sig: inspect.Signature,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> AsyncIterator[Any]:
        return client._remote_bidi(fn, sig, *args, **kwargs)


# ── Pattern list ────────────────────────────────────────────────────
#
# Frozen tuple — bidi first, stream second, unary last (most-general
# fallback). Not extensible at runtime; add a new pattern by editing
# this module.

_PATTERNS: tuple[type[WirePattern], ...] = (BidiPattern, StreamPattern, UnaryPattern)


def select_pattern(sig: inspect.Signature) -> type[WirePattern]:
    """Return the first pattern class whose `matches(sig)` is True.

    `UnaryPattern.matches` returns True unconditionally, so this never
    raises — every signature has a pattern.
    """
    for p in _PATTERNS:
        if p.matches(sig):
            return p
    raise TypeError(f"no WirePattern matches signature {sig!r}")


__all__ = [
    "AsyncIterator",
    "BidiPattern",
    "StreamPattern",
    "UnaryPattern",
    "WirePattern",
    "select_pattern",
]
