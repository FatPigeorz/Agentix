# Agentix RPC protocol

This file is the source-of-truth contract for the three RPC shapes,
their wire FSMs, and the invariants every transport (HTTP `/_remote`,
Socket.IO, in-process, subprocess) must uphold. Tests in
`tests/test_namespace_protocol.py` enforce these.

Anything that doesn't match this doc is a bug. Update both code and
this file together.

## Transports

| Path                    | Shape used | Wire                                |
| ----------------------- | ---------- | ----------------------------------- |
| `POST /_remote`         | unary      | msgpack body + msgpack response     |
| `/socket.io/`           | stream + bidi | msgpack-payload events            |
| `python -m agentix.runtime.worker` stdin/stdout | all three | length-prefixed msgpack frames |

The first two are the **host↔sandbox** edge (host = trainer, sandbox =
runtime container). The third is the **multiplexer↔worker** edge
inside the sandbox; one worker subprocess per namespace.

## The three shapes

Detection at `Dispatcher.bind` and `RuntimeClient.remote` runs the same
`agentix.dispatch.detect_shape(fn)`:

```
isasyncgenfunction(fn) → True  + has Channel[T] param → bidi
isasyncgenfunction(fn) → True  + no Channel[T] param  → stream
isasyncgenfunction(fn) → False                        → unary
```

Annotations are a hint only. `inspect.isasyncgenfunction` is the
source of truth — a regular `async def` returning an iterator value is
**unary**, not stream.

| Shape  | Impl signature                                                          | Client-side return       |
| ------ | ----------------------------------------------------------------------- | ------------------------ |
| unary  | `async def f(...) -> T`                                                 | `Unary[T]` (awaitable)   |
| stream | `async def f(...) -> AsyncIterator[T]: yield ...`                       | `Stream[T]` (async-iter) |
| bidi   | `async def f(..., inbox: Channel[I]) -> AsyncIterator[O]: yield ...`    | `Bidi[I, O]` (async-iter + `.inbox`) |

## Wire FSMs

```
unary:   CALL  →  (CANCEL?)  →  RESULT | ERROR

stream:  CALL  →  (CANCEL?)  →  STREAM_ITEM*  →  STREAM_END | ERROR

bidi (out):  CALL  →  (CANCEL?)  →  STREAM_ITEM*  →  STREAM_END | ERROR
bidi (in):   BIDI_IN*  →  BIDI_END_IN
                   (independent of out; either may end first)
```

`(CANCEL?)` is at-most-one CANCEL frame from the client. Duplicate
CANCEL is idempotent and silently dropped.

## Common invariants

All three shapes obey these. Tests verify each:

1. **Exactly one terminal frame** per call from server to client —
   `RESULT`, `STREAM_END`, or `ERROR`. After it lands, the
   call_id is closed; both ends GC their per-call state.

2. **Terminal-then-quiet** — after the server emits its terminal
   frame, any client→server frame for that call_id is silently
   dropped with a single warn log. Race-tolerant by design (caller
   may CANCEL just as server emits RESULT).

3. **Dual-side validation** — args go through pydantic
   `TypeAdapter` on the client before send AND on the server before
   dispatch. Items yielded by stream/bidi impls go through the
   output `TypeAdapter` server-side; items received go through the
   item `TypeAdapter` client-side.

4. **CANCEL → terminal ack** — when the client sends CANCEL, the
   server MUST close the call with `ERROR(type="Cancelled", cancelled=True)`.
   The client SHOULD await this terminal frame before declaring the
   call done; the current implementation is fire-and-forget (the
   ack arrives but lands in a closed call_id state and is silently
   dropped). Functional risk is low — the worker still cancels its
   impl task on CANCEL — but spec-level ack-and-wait is future
   work.

5. **Worker death** — when a namespace worker subprocess exits
   (crash, SIGKILL, graceful shutdown mid-call), the multiplexer
   fails every in-flight call_id with
   `ERROR(type="WorkerExited")`. No call hangs indefinitely.

6. **CANCEL is idempotent** — server tracks "cancelled" state per
   call_id; second CANCEL has no effect.

## Cancellation matrix

| Trigger                                  | Client behavior                            | Server behavior                                      |
| ---------------------------------------- | ------------------------------------------ | ---------------------------------------------------- |
| Caller `cancel()`s `c.remote(...)`        | emit CANCEL, await terminal               | `aclose()` impl; finally blocks run; emit ERROR(cancelled=True) |
| `async for ... break`                    | same                                       | same                                                  |
| `Channel.close()` (bidi only)            | emit BIDI_END_IN                          | impl's `async for inbox:` ends naturally; impl decides when to return |
| Server impl raises                       | iter raises `RemoteCallError` at next anext | emit ERROR                                            |
| Server impl returns / generator exhausts | iter's anext raises StopAsyncIteration    | emit STREAM_END / RESULT                              |

## Half-close (bidi)

Out-stream (server → client) and in-stream (client → server) are
**independent**:

- Server impl can `return` before client closes its Channel → server
  emits STREAM_END. Late BIDI_IN frames are dropped per invariant 2.
  Client SHOULD `await inbox.close()` for cleanliness but isn't
  required to.
- Client closes Channel before server is done → BIDI_END_IN frame.
  Server's `async for inbox:` exits; impl can continue emitting
  outputs or return.

## Error model

`ERROR` frames carry:

```python
{ "type": str,           # e.g. "ValueError", "Cancelled", "WorkerExited"
  "message": str,
  "traceback": str | None,
  "cancelled": bool,     # True only when emitted in response to CANCEL
}
```

Client maps to:

- `cancelled=True` → `asyncio.CancelledError` re-raised at the call site
- everything else → `agentix.RemoteCallError(package, method, error)`

## Backpressure

- **Server → client (all shapes)**: rely on TCP / Socket.IO emit
  buffer. Server emit blocks when the buffer is full; the impl's
  next yield pauses naturally.
- **Client → server (bidi only)**: bounded `Channel(maxsize=N)` on
  the caller side. `await ch.send(item)` blocks when local buffer
  full. The framework's BIDI_IN pump drains the channel only as
  fast as Socket.IO emit succeeds, so a slow consumer on the
  sandbox side propagates back to the user's `.send()` call.
- **Worker-internal (bidi in-queue)**: per-call queue is filled by
  a dedicated pump task that does `await q.put(item)`. The main
  read loop never blocks on a slow consumer; it routes frames to
  per-call pumps. A pump that blocks only blocks its own call_id.

## Frame ordering

- Worker → multiplexer: one `_outbound_q` per worker. Drainer task
  preserves FIFO across TRACE + RESULT + STREAM_ITEM frames.
  Observers see traces in the order the impl emitted them.
- Multiplexer → worker: `_send_lock` serializes writes from
  multiple coroutines into one stdin stream.
- Socket.IO: `async_handlers=True` (the default — one task per event)
  is safe AS LONG AS handlers complete atomically (no awaits before
  any state mutation). `on_bidi_in` is therefore intentionally
  synchronous — it does `put_nowait` on an unbounded intake; the
  blocking `await put` onto the bounded in_queue happens on a
  dedicated per-call pump task. This way concurrent handler tasks
  finish in scheduling order (which matches arrival order for
  same-session events) without registering at a bounded queue's
  putter deque, so BIDI_IN ordering is preserved AND CANCEL can
  interleave mid-flow.

## Connection lifecycle

| Edge                  | Connect                                       | Disconnect / cleanup                                                                  |
| --------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------- |
| host → runtime HTTP   | per-call ephemeral                            | httpx closes                                                                          |
| host → runtime SocketIO | lazy on first stream/bidi/log call          | `RuntimeClient.close()` disconnects; server drops all sessions' in-flight calls (cancel + drain) |
| runtime → worker      | lazy on first dispatch (spawn subprocess)     | on EOF the multiplexer fails all in-flight calls (invariant 5); on shutdown, SHUTDOWN frame + `await proc.wait()` + reap |

## Frame type catalog

(See `agentix.runtime.frames` for the constants — these names are the
on-wire `type` field values.)

| Direction        | Frame         | Payload fields                                                  |
| ---------------- | ------------- | --------------------------------------------------------------- |
| mux → worker     | `CALL`        | call_id, kind ("unary"/"stream"/"bidi"), method, args, kwargs   |
| mux → worker     | `BIDI_IN`     | call_id, item                                                   |
| mux → worker     | `BIDI_END_IN` | call_id                                                         |
| mux → worker     | `CANCEL`      | call_id                                                         |
| mux → worker     | `SHUTDOWN`    | —                                                               |
| worker → mux     | `READY`       | package                                                         |
| worker → mux     | `BOOT_ERROR`  | error                                                           |
| worker → mux     | `RESULT`      | call_id, value (unary only)                                     |
| worker → mux     | `STREAM_ITEM` | call_id, value                                                  |
| worker → mux     | `STREAM_END`  | call_id                                                         |
| worker → mux     | `ERROR`       | call_id, error                                                  |
| worker → mux     | `TRACE`       | kind, payload, call_id?, source?                                |

The Socket.IO event names on the host↔runtime edge are a 1:1
translation of these (`stream:item` ↔ STREAM_ITEM, `bidi:out` ↔
STREAM_ITEM with bidi semantics, etc.) — see `agentix/runtime/events.py`.

## What's NOT in scope

- **Per-call timeouts**: the framework doesn't impose them. Wrap
  with `asyncio.wait_for(...)` at the call site if needed.
- **Retries**: not modeled. Calls are at-most-once.
- **Credit-based flow control**: simpler bounded-Channel
  backpressure suffices for RL workloads; revisit if a real-time
  stream needs cross-network credits.
- **Auth / TLS**: deployment-layer concern, not the wire.
