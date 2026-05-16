"""Unit tests for `Namespace`, `WirePattern`, and `Dispatcher.bind_namespace`.

Covers the R1 typing rule (dynamic bind + static typing). Wire patterns
are exercised at the `select_pattern` boundary — the three built-ins
are fixed and not user-extensible. Closure-protocol-level integration
is exercised in `test_namespace_protocol.py`.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from agentix.dispatch import Dispatcher
from agentix.namespace import Namespace, discover_methods
from agentix.runtime.models import RemoteRequest
from agentix.wire import (
    BidiPattern,
    StreamPattern,
    UnaryPattern,
    select_pattern,
)

# ── Namespace method discovery ──────────────────────────────────────


def test_namespace_methods_only_lists_public_callables() -> None:
    class N(Namespace):
        @staticmethod
        def public(x: int) -> int: ...
        @staticmethod
        def _private() -> None: ...  # underscore → skipped
        constant = 42  # non-function → skipped

    names = [n for n, _ in discover_methods(N)]
    assert names == ["public"]


def test_namespace_excluded_hides_methods() -> None:
    class N(Namespace):
        __namespace_excluded__ = frozenset({"hidden"})

        @staticmethod
        def visible() -> None: ...
        @staticmethod
        def hidden() -> None: ...

    assert [n for n, _ in discover_methods(N)] == ["visible"]


def test_namespace_inherits_methods_from_namespace_ancestors() -> None:
    """A Namespace subclass may inherit methods from another Namespace
    (e.g. for shared-mixin stubs). The composition rule applies to stub↔impl,
    not stub↔stub."""

    class Base(Namespace):
        @staticmethod
        def common() -> int: ...

    class Extended(Base):
        @staticmethod
        def extra() -> str: ...

    names = sorted(n for n, _ in discover_methods(Extended))
    assert names == ["common", "extra"]


# ── Pattern selection ───────────────────────────────────────────────


def _sig(fn: object) -> inspect.Signature:
    # eval_str=True mirrors what Dispatcher.bind does — resolve PEP 563
    # stringified annotations so `get_origin(AsyncIterator[T])` works.
    return inspect.signature(fn, eval_str=True)  # type: ignore[arg-type]


def test_select_unary_for_plain_signature() -> None:
    def f(x: int) -> str: ...
    assert select_pattern(_sig(f)) is UnaryPattern


def test_select_stream_for_async_iterator_return() -> None:
    def f(x: int) -> AsyncIterator[int]: ...
    assert select_pattern(_sig(f)) is StreamPattern


def test_select_bidi_for_async_iterator_param_and_return() -> None:
    def f(events: AsyncIterator[str]) -> AsyncIterator[int]: ...
    assert select_pattern(_sig(f)) is BidiPattern


# ── Dispatcher.bind_namespace ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_namespace_routes_through_dispatcher() -> None:
    """A full Namespace round-trip: one class with real method bodies."""

    class Math(Namespace):
        @staticmethod
        async def add(a: int, b: int) -> int:
            return a + b

        @staticmethod
        async def echo(items: list[str]) -> list[str]:
            return list(reversed(items))

    d = Dispatcher().bind_namespace(Math)
    assert set(d.methods()) == {"add", "echo"}

    resp = await d.dispatch(RemoteRequest(
        package="x", method="add", args=[], kwargs={"a": 2, "b": 3},
    ))
    assert resp.ok and resp.value == 5

    resp = await d.dispatch(RemoteRequest(
        package="x", method="echo", args=[], kwargs={"items": ["a", "b", "c"]},
    ))
    assert resp.ok and resp.value == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_bind_namespace_picks_correct_pattern() -> None:
    class N(Namespace):
        @staticmethod
        async def unary(x: int) -> int:
            return x

        @staticmethod
        async def stream(n: int) -> AsyncIterator[int]:
            for i in range(n):
                yield i

        @staticmethod
        async def bidi(events: AsyncIterator[str]) -> AsyncIterator[int]:
            async for e in events:
                yield len(e)

    d = Dispatcher().bind_namespace(N)
    assert d.is_streaming("unary") is False
    assert d.is_streaming("stream") is True
    assert d.is_bidi("stream") is False
    assert d.is_bidi("bidi") is True


@pytest.mark.asyncio
async def test_bind_namespace_works_with_protocol_subclass() -> None:
    """A Namespace subclass that's *also* a Protocol still binds fine —
    the class IS the namespace; method bodies carry the real logic."""
    from typing import Protocol, runtime_checkable

    @runtime_checkable
    class Greeting(Namespace, Protocol):
        @staticmethod
        async def hello(name: str) -> str: ...

    # Test fixture: the real class has bodies. Pyright doesn't see this
    # as instantiating a Protocol because Greeting isn't directly used as
    # the impl — `bind_namespace` accepts the class and instantiates it.
    class GreetingImpl(Greeting):  # noqa: ARG001  (Protocol subclass for typing)
        @staticmethod
        async def hello(name: str) -> str:
            return f"hi {name}"

    d = Dispatcher().bind_namespace(GreetingImpl)
    assert d.methods() == ["hello"]
    resp = await d.dispatch(RemoteRequest(
        package="x", method="hello", args=[], kwargs={"name": "alice"},
    ))
    assert resp.ok and resp.value == "hi alice"
