"""R2 extensibility proof: a third-party `WirePattern` slots in without
touching framework code.

This is a demo, not a wire-complete implementation. It exercises every
extension hook the framework provides — `register_pattern`,
`select_pattern`, the `WirePattern` ABC, and `Dispatcher.bind` routing
through the chosen pattern — and shows that a custom pattern with its
own marker type and its own client/server logic plugs in cleanly.

The demo pattern is **PubSub**: a 1-to-N broadcast shape distinct from
the built-in stream/bidi (which are 1-to-1). A method returning
`Topic[T]` is treated as publishing onto a topic; callers subscribe
through the same method to receive every event broadcast on it.

The body of the test is intentionally hand-rolled — a real third-party
pattern would ship its own server-side wire handlers (FastAPI routes,
Socket.IO event names, etc.). What the framework guarantees is the
*hook*: bind-time pattern selection + client-side dispatch entry point.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

import pytest

from agentix.dispatch import Dispatcher
from agentix.namespace import Namespace
from agentix.runtime.models import RemoteRequest
from agentix.wire import (
    StreamPattern,
    UnaryPattern,
    WirePattern,
    _reset_patterns,
    register_pattern,
    select_pattern,
)

T = TypeVar("T")


class Topic(Generic[T]):
    """Marker type — a `-> Topic[T]` return annotation declares pubsub semantics.

    Lives in user code, not in the framework. The framework only sees
    `get_origin(Topic[T]) is Topic`.
    """


class PubSubPattern(WirePattern):
    """Third-party pubsub pattern. Matches `-> Topic[T]` returns.

    Server side: collect every event the impl publishes onto an in-memory
    bus (this demo); a real impl would broadcast via Socket.IO to a
    topic room or a Redis channel. Client side: subscribe and receive.

    No framework code knew this pattern existed — proves R2.
    """

    name = "pubsub"

    @classmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        ret = sig.return_annotation
        # `get_origin` on `Topic[int]` returns `Topic`; on bare `Topic`,
        # `inspect.signature` reports `Topic` directly.
        origin = getattr(ret, "__origin__", ret)
        return origin is Topic

    def bind(self, sig: inspect.Signature) -> None:
        return  # nothing to precompute for the demo

    def client_invoke(self, client, fn, sig, args, kwargs):
        # The framework's only requirement is that this returns either an
        # Awaitable or an AsyncIterator. A real pubsub would hand back an
        # AsyncIterator wired to a Socket.IO topic-subscription. For the
        # demo we never actually run the client path — the test exercises
        # bind + server dispatch directly.
        raise NotImplementedError(
            "pubsub demo doesn't wire a client path; the test invokes "
            "the bound impl directly to prove pattern selection"
        )


@pytest.fixture(autouse=True)
def _isolate_patterns():
    """Each test re-registers; reset the registry afterwards so the
    built-ins are back in place for unrelated tests."""
    yield
    _reset_patterns()


def test_pubsub_pattern_matches_only_its_marker() -> None:
    register_pattern(PubSubPattern)

    def feed() -> Topic[int]: ...  # noqa: ARG001 — return type is the contract
    def echo(x: int) -> str: ...   # noqa: ARG001 — plain unary

    assert select_pattern(inspect.signature(feed, eval_str=True)) is PubSubPattern
    # plain unary unaffected — built-in registry order isn't disturbed
    assert select_pattern(inspect.signature(echo, eval_str=True)) is UnaryPattern


def test_dispatcher_bind_records_pubsub_pattern() -> None:
    """The bound method carries the resolved pattern, so the dispatcher
    can route accordingly on every call."""
    register_pattern(PubSubPattern)

    class Feed(Namespace):
        @staticmethod
        def emit(payload: int) -> Topic[int]:
            return Topic[int]()  # type: ignore[return-value]

    d = Dispatcher().bind_namespace(Feed)
    bound = d._methods["emit"]  # noqa: SLF001 — demo introspection
    assert bound.pattern is PubSubPattern


def test_pubsub_pattern_does_not_clash_with_stream() -> None:
    """Stream-returning stubs still resolve to `StreamPattern` after the
    pubsub pattern is registered — isolation between patterns."""
    register_pattern(PubSubPattern)

    def stream() -> AsyncIterator[int]: ...
    def pub() -> Topic[int]: ...

    assert select_pattern(inspect.signature(stream, eval_str=True)) is StreamPattern
    assert select_pattern(inspect.signature(pub, eval_str=True)) is PubSubPattern


@pytest.mark.asyncio
async def test_dispatcher_dispatch_rejects_pubsub_via_unary_path() -> None:
    """The Dispatcher's `dispatch()` is the unary path. A pubsub-bound
    method falls through it as a non-stream, non-bidi method — so it
    actually executes the impl. The point: the framework didn't pretend
    to know the pattern; it just bound it. A real PubSubPattern would
    register its own server entry point (route / SIO event) and bypass
    the unary `/_remote` HTTP path entirely."""
    register_pattern(PubSubPattern)

    class Feed(Namespace):
        received: int = -1

        @staticmethod
        def emit(payload: int) -> Topic[int]:
            Feed.received = payload  # write to class so the test can read it
            return Topic[int]()  # type: ignore[return-value]

    d = Dispatcher().bind_namespace(Feed)

    # Demo: invoke the bound impl directly to show the binding works.
    # A real PubSubPattern would intercept dispatch via its own transport.
    resp = await d.dispatch(RemoteRequest(
        package="demo", method="emit", args=[], kwargs={"payload": 42},
    ))
    # The unary path can't serialize a `Topic[int]()` instance through
    # pydantic, so the response is a SerializationError. That's expected
    # — the takeaway is that the impl ran (Feed.received == 42) and the
    # pattern was recorded; a real transport would short-circuit before
    # this point.
    assert not resp.ok
    assert Feed.received == 42
