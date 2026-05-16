"""Unit tests for namespace discovery, call-shape detection, and
`Dispatcher.bind_namespace`.

Discovery is duck-typed — a namespace can be a Python module, a class,
or any object whose top-level attributes include async callables. The
three call shapes (unary / stream / bidi) are detected by
`agentix.dispatch.detect_shape` from `isasyncgenfunction(fn)` +
whether any parameter is annotated `Channel[T]`. End-to-end protocol
tests live in `test_namespace_protocol.py`.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from agentix import Channel
from agentix.dispatch import Dispatcher, detect_shape
from agentix.namespace import discover_methods
from agentix.runtime.models import RemoteRequest

# ── Namespace method discovery ──────────────────────────────────────


def test_namespace_methods_only_lists_public_callables() -> None:
    class N:
        @staticmethod
        def public(x: int) -> int: ...
        @staticmethod
        def _private() -> None: ...  # underscore → skipped
        constant = 42  # non-function → skipped

    names = [n for n, _ in discover_methods(N)]
    assert names == ["public"]


def test_namespace_excluded_hides_methods() -> None:
    class N:
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

    class Base:
        @staticmethod
        def common() -> int: ...

    class Extended(Base):
        @staticmethod
        def extra() -> str: ...

    names = sorted(n for n, _ in discover_methods(Extended))
    assert names == ["common", "extra"]


# ── Call-shape detection ────────────────────────────────────────────


def _sig(fn: object) -> inspect.Signature:
    # eval_str=True mirrors what Dispatcher.bind does — resolve PEP 563
    # stringified annotations so `get_origin(Channel[T])` works.
    return inspect.signature(fn, eval_str=True)  # type: ignore[arg-type]


def test_detect_unary_for_plain_signature() -> None:
    async def f(x: int) -> str:
        return ""
    assert detect_shape(f, _sig(f)) == "unary"


def test_detect_stream_for_async_generator_return() -> None:
    async def f(x: int) -> AsyncIterator[int]:
        yield 0
    assert detect_shape(f, _sig(f)) == "stream"


def test_detect_bidi_for_channel_param() -> None:
    async def f(events: Channel[str]) -> AsyncIterator[int]:
        async for _ in events:
            yield 0
    assert detect_shape(f, _sig(f)) == "bidi"


def test_detect_async_iter_param_is_not_bidi() -> None:
    """Only `Channel[T]` marks bidi — a bare AsyncIterator parameter is
    just a regular value parameter (and is silently classified as stream)."""
    async def f(events: AsyncIterator[str]) -> AsyncIterator[int]:
        async for _ in events:
            yield 0
    assert detect_shape(f, _sig(f)) == "stream"


# ── Dispatcher.bind_namespace ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_namespace_routes_through_dispatcher() -> None:
    """A full Namespace round-trip: one class with real method bodies."""

    class Math:
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
    class N:
        @staticmethod
        async def unary(x: int) -> int:
            return x

        @staticmethod
        async def stream(n: int) -> AsyncIterator[int]:
            for i in range(n):
                yield i

        @staticmethod
        async def bidi(events: Channel[str]) -> AsyncIterator[int]:
            async for e in events:
                yield len(e)

    d = Dispatcher().bind_namespace(N)
    assert d.is_streaming("unary") is False
    assert d.is_streaming("stream") is True
    assert d.is_bidi("stream") is False
    assert d.is_bidi("bidi") is True


@pytest.mark.asyncio
async def test_bind_namespace_accepts_a_module() -> None:
    """`bind_namespace` is duck-typed — modules work as namespaces too,
    not just classes."""
    import types
    mod = types.ModuleType("test_inline_module_ns")

    async def hello(name: str) -> str:
        return f"hi {name}"

    mod.hello = hello

    d = Dispatcher().bind_namespace(mod)
    assert d.methods() == ["hello"]
    resp = await d.dispatch(RemoteRequest(
        package="x", method="hello", args=[], kwargs={"name": "alice"},
    ))
    assert resp.ok and resp.value == "hi alice"
