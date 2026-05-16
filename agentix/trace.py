"""Trace emission with fan-out sinks.

Namespace impls call `agentix.trace.emit(kind, payload)` to record one
event in the rollout's trace. Every registered **trace sink** receives
the event. The framework's runtime ships a sink that fans events out
over the Socket.IO `trace` channel; observability sinks (Sentry, OTel,
Logfire, …) register their own by calling `register_sink(fn)` at
startup — there's no entry-point machinery here, it's a plain Python
API.

```python
# in your runtime extension or app startup
from agentix.trace import register_sink

def my_sink(kind, payload, call_id, source):
    # forward to OTel / Sentry / your own bus
    ...

register_sink(my_sink)
```

`call_id` correlates events to a specific rollout. The dispatcher pins
the active call_id into a contextvar before invoking each impl, so
`emit()` picks it up automatically — namespaces don't have to thread it
through their code.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable
from typing import Any, Final

from agentix.idents import CallId, PackageName

logger = logging.getLogger("agentix.trace")

SinkFn = Callable[[str, dict[str, Any], CallId | None, PackageName | None], None]
"""A trace sink: `(kind, payload, call_id, source) -> None`. Sinks
should never raise (the framework swallows exceptions to keep tracing
from breaking a rollout), but the framework also defensively wraps
each call."""

# In-process sink list. `register_sink` appends; emit() fans out across
# every sink. Sinks live for the runtime's lifetime; tests use
# `unregister_sink` to clean up.
_sinks: list[SinkFn] = []

_current_call_id: contextvars.ContextVar[CallId | None] = contextvars.ContextVar(
    "agentix_trace_call_id", default=None,
)
_current_source: contextvars.ContextVar[PackageName | None] = contextvars.ContextVar(
    "agentix_trace_source", default=None,
)


def register_sink(sink: SinkFn) -> None:
    """Add a trace sink. Receives every event emitted from any namespace
    via `agentix.trace.emit(...)`.

    Sink errors are logged + swallowed (tracing must never break a
    rollout). Sinks are called in registration order.
    """
    _sinks.append(sink)


def unregister_sink(sink: SinkFn) -> None:
    """Remove a previously-registered sink. No-op if not present.

    Mostly used by tests to clean up after themselves; production
    sinks live for the runtime's lifetime.
    """
    try:
        _sinks.remove(sink)
    except ValueError:
        pass


def set_call_context(
    call_id: CallId | None,
    source: PackageName | None,
) -> tuple[contextvars.Token, contextvars.Token]:
    """Set the active call_id + source for trace events emitted while this
    context is on the call stack. Returns the contextvar reset tokens.
    """
    return _current_call_id.set(call_id), _current_source.set(source)


def reset_call_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    """Restore the call_id + source contextvars to their previous values."""
    cid_token, src_token = tokens
    _current_call_id.reset(cid_token)
    _current_source.reset(src_token)


def current_call_id() -> CallId | None:
    """The call_id pinned by the dispatcher for the current request, if any."""
    return _current_call_id.get()


def current_source() -> PackageName | None:
    """The namespace package currently being dispatched, if any."""
    return _current_source.get()


def emit(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    call_id: CallId | None = None,
    source: PackageName | None = None,
) -> None:
    """Record a single trace event. Fans out to every registered sink.

    `call_id` and `source` default to the dispatcher-set context. Namespaces
    should normally call `emit("kind", {...})` and let the runtime fill
    in the correlation. Sink errors are logged + swallowed — tracing
    must never break a rollout.
    """
    if not _sinks:
        return
    cid: Final = call_id if call_id is not None else _current_call_id.get()
    src: Final = source if source is not None else _current_source.get()
    pl = payload or {}
    for sink in _sinks:
        try:
            sink(kind, pl, cid, src)
        except Exception as exc:
            logger.warning("trace sink %r raised: %s", getattr(sink, "__name__", sink), exc)


def now() -> float:
    """Helper for callers that want to record their own timestamps."""
    return time.time()
