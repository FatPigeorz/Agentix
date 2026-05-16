"""Per-bidi-call queue plumbing — used by both the in-sandbox worker and
the host-side Socket.IO server.

Bidi inbound is two-tier:

  * `intake` (unbounded) — the wire-facing handler does a synchronous
    `put_nowait` here, so the main read loop never blocks.
  * `user_q` (bounded `DEFAULT_BIDI_BUFFER`) — the dispatcher's
    `_input_iter` reads from here. The bound is what gives end-to-end
    backpressure: when full, `drain`'s `await put` blocks, intake
    grows briefly, OS pipe / Socket.IO buffer fills, ultimately the
    caller's `Channel.send()` awaits.

This module owns the loop body and the cleanup helpers; the surrounding
state (which queues belong to which call_id, how cancel triggers) stays
with each caller.
"""

from __future__ import annotations

import asyncio
from typing import Any

DEFAULT_BIDI_BUFFER = 64


async def drain(intake: asyncio.Queue, user_q: asyncio.Queue, sentinel: Any) -> None:
    """Move items from `intake` to `user_q` in order. Stops after
    forwarding `sentinel`. `CancelledError` exits silently — the call
    is being torn down."""
    try:
        while True:
            item = await intake.get()
            await user_q.put(item)
            if item is sentinel:
                return
    except asyncio.CancelledError:
        pass


def cancel_if_running(task: asyncio.Task | None) -> None:
    """Cancel `task` if it exists and hasn't already completed."""
    if task is not None and not task.done():
        task.cancel()


__all__ = ["DEFAULT_BIDI_BUFFER", "cancel_if_running", "drain"]
