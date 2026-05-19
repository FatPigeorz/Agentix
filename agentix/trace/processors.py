"""Built-in `agentix.trace.Processor` implementations.

The abstract `Processor` and `Exporter` live in the package `__init__`
because they're part of the core abstraction. Concrete implementations
(console pretty-printer, future BatchProcessor, future exporters)
live here so the core file stays small.
"""

from __future__ import annotations

import sys
from typing import Any

from agentix.trace import Processor, Span, Trace


class ConsoleProcessor(Processor):
    """Pretty-print every trace/span lifecycle event to stderr,
    indented by parent depth. Useful for local dev / smoke tests."""

    def __init__(self, *, stream: Any = None) -> None:
        self._stream = stream or sys.stderr
        # Track per-trace span_id → depth so children indent.
        self._depth: dict[str, int] = {}

    def _write(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            pass

    def on_trace_start(self, t: Trace) -> None:
        self._depth[t.trace_id] = 0
        self._write(f"[trace.start] {t.name} ({t.trace_id[:14]}…) {t.metadata or ''}".rstrip())

    def on_trace_end(self, t: Trace) -> None:
        self._write(f"[trace.end]   {t.name} ({t.trace_id[:14]}…)")
        self._depth.pop(t.trace_id, None)

    def on_span_start(self, s: Span) -> None:
        depth = self._depth.get(s.trace_id, 0)
        indent = "  " * depth
        attrs = " ".join(f"{k}={v!r}" for k, v in s.attrs.items())
        self._write(f"{indent}[span.start] {s.name}  {attrs}".rstrip())
        # Children of this span indent one further.
        self._depth[s.span_id] = depth + 1

    def on_span_end(self, s: Span) -> None:
        self._depth.pop(s.span_id, None)
        depth = self._depth.get(s.trace_id, 0)
        if s.parent_id is not None:
            depth += 1
        indent = "  " * (depth - 1 if depth > 0 else 0)
        status = f" status={s.status}" if s.status != "unset" else ""
        ev = f" events={len(s.events)}" if s.events else ""
        err = f" error={s.error.message!r}" if s.error else ""
        self._write(f"{indent}[span.end]   {s.name}{status}{ev}{err}")


__all__ = ["ConsoleProcessor"]
