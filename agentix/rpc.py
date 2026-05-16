"""Tagged-union RPC variants returned by `RuntimeClient.remote()`.

The framework's three call shapes (unary, server-stream, bidi) are
exposed as three frozen dataclass variants. Each implements the
Python protocol that matches its shape, so the common case is a
one-liner:

    action = await c.remote(agent.predict, obs=obs)               # Unary
    async for step in c.remote(env.rollout, seed=42): ...         # Stream
    async for reply in c.remote(chat.chat, inbox=ch, opts=opts):  # Bidi
        ch.send(...)

For generic dispatch over any shape, `match` on the variant:

    match c.remote(fn, ...):
        case Unary(_) as u: result = await u
        case Stream() as s: async for x in s: ...
        case Bidi(inbox, _) as b: ...

`Channel[T]` is the input-side helper for bidi. The user constructs
one, passes it to `c.remote` as the bidi-marked kwarg, and pushes
items via `.send()` from anywhere — typically from a producer task
that runs concurrently with the output `async for`. Backpressure is
the queue's `maxsize`; on close the channel raises `StopAsyncIteration`
from its async iterator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Generator
from dataclasses import dataclass
from typing import Any, Generic, TypeAlias, TypeVar, get_origin

In = TypeVar("In")
R = TypeVar("R")


def is_channel_annotation(ann: Any) -> bool:
    """True if `ann` is `Channel` or `Channel[T]`. The marker that
    `agentix.dispatch.detect_shape` and `RuntimeClient.remote` use to
    distinguish bidi from stream."""
    return ann is Channel or get_origin(ann) is Channel


# Module-level singleton used to signal end-of-input through Channel's
# internal queue. Compared with `is` — never exposed to user code.
_CHANNEL_CLOSED: Any = object()


class Channel(AsyncIterator[In], Generic[In]):
    """User-pushed async channel for bidi inputs.

    Satisfies `AsyncIterator[I]` — `Channel[T]` as a bidi method's
    parameter annotation is what marks the call as bidi (see
    `agentix.dispatch.detect_shape`). The caller pushes items with
    `await ch.send(item)`; `await ch.close()` signals end-of-input.
    Items are delivered FIFO. `maxsize` bounds the local buffer;
    when full, `.send()` awaits until consumers (the framework's
    pump task) drain space.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def send(self, item: In) -> None:
        if self._closed:
            raise RuntimeError("Channel is closed")
        await self._q.put(item)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._q.put(_CHANNEL_CLOSED)

    def __aiter__(self) -> AsyncIterator[In]:
        return self

    async def __anext__(self) -> In:
        item = await self._q.get()
        if item is _CHANNEL_CLOSED:
            raise StopAsyncIteration
        return item


@dataclass(frozen=True, slots=True)
class Unary(Generic[R]):
    """Awaitable result of a unary remote call. `await` it to get `R`."""

    _coro: Coroutine[Any, Any, R]

    def __await__(self) -> Generator[Any, None, R]:
        return self._coro.__await__()


@dataclass(frozen=True, slots=True)
class Stream(Generic[R]):
    """Server-streaming result. `async for` over it yields `R` items."""

    _aiter: AsyncIterator[R]

    def __aiter__(self) -> AsyncIterator[R]:
        return self._aiter


@dataclass(frozen=True, slots=True)
class Bidi(Generic[In, R]):
    """Bidirectional remote call. Push to `inbox`, `async for` for outputs.

    `inbox` is the same `Channel[In]` the caller passed to `c.remote`;
    storing it here lets generic match-based dispatch reach it without
    needing the original variable.
    """

    inbox: Channel[In]
    _aiter: AsyncIterator[R]

    def __aiter__(self) -> AsyncIterator[R]:
        return self._aiter


RemoteCall: TypeAlias = Unary[R] | Stream[R] | Bidi[Any, R]


__all__ = [
    "Bidi", "Channel", "RemoteCall", "Stream", "Unary",
    "is_channel_annotation",
]
