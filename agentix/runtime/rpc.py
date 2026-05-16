"""RPC framing for worker stdio — length-prefixed msgpack.

Each frame on a worker's stdin/stdout is:

  +--------+-------------------+
  | u32 LE | n bytes msgpack   |
  +--------+-------------------+

The msgpack blob is a dict — see frame schemas below. `agentix.runtime.codec`
handles encode/decode, including ext types for ndarray + pydantic models.

Frame schemas (`{"type": "...", ...}` — extra fields per type):

  ─── runtime → worker ─────────────────────────────────────
    call         {call_id, method, args, kwargs, kind: "unary"|"stream"|"bidi"}
    bidi_in      {call_id, item}            — push input chunk to a bidi call
    bidi_end_in  {call_id}                   — close input side of a bidi call
    cancel       {call_id}                   — abort an in-flight call
    shutdown     {}                          — graceful exit; worker drains then exits

  ─── worker → runtime ─────────────────────────────────────
    ready        {package}                   — sent once after class binds OK
    boot_error   {error}                     — sent once if class fails to bind
    result       {call_id, value}            — unary success
    error        {call_id, error}            — unary failure or stream/bidi error
    stream_item  {call_id, value}            — one chunk of a streaming response
    stream_end   {call_id}                   — clean end of stream/bidi out
    trace        {kind, payload, call_id?, source?}   — namespace trace.emit()

`call_id` correlates request frames with their response frames.

Args/kwargs/values are native Python objects (msgpack round-trips them
via codec's ext types). Pydantic validation happens on the receiving
end via `TypeAdapter.validate_python` — there's no JSON intermediate.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from agentix.runtime.codec import pack, unpack


def pack_frame(payload: dict[str, Any]) -> bytes:
    """Encode one frame: 4-byte LE length + msgpack body."""
    body = pack(payload)
    return struct.pack("<I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one frame from `reader`. Returns None on EOF."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    (n,) = struct.unpack("<I", header)
    if n == 0:
        return {}
    body = await reader.readexactly(n)
    return unpack(body)


async def write_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Write one frame and flush. Callers serialize concurrent writes via
    a lock; each call writes a complete frame in one shot."""
    writer.write(pack_frame(payload))
    await writer.drain()


__all__ = ["pack_frame", "read_frame", "write_frame"]
