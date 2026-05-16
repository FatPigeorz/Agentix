"""End-to-end tests for the subprocess worker path.

In-process tests (test_namespace_protocol.py) exercise the multiplexer
through its InProcessWorker backend — same protocol, no subprocess.
These tests use the real SubprocessWorker so the stdio framing, RPC
correlation, and trace-frame forwarding all run for real.

The target class lives in `tests/_worker_target.py` — a real importable
module so the worker subprocess can `import _worker_target` after we
add `tests/` to its PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agentix.runtime.models import RemoteRequest
from agentix.runtime.multiplexer import NamespaceMultiplexer


TESTS_DIR = Path(__file__).parent
_PACKAGE = "_worker_target"
_TARGET = f"{_PACKAGE}:Echo"


@pytest.fixture
def worker_env(monkeypatch):
    """Inject `tests/` into PYTHONPATH so the worker subprocess can find
    `_worker_target`. The framework's own modules are already importable
    from the runtime's site-packages.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(TESTS_DIR), existing] if existing else [str(TESTS_DIR)]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))


def _make_multiplexer(trace_forwarder=None) -> NamespaceMultiplexer:
    mp = NamespaceMultiplexer(trace_forwarder=trace_forwarder)
    mp.register_subprocess(_PACKAGE, _TARGET, sys.executable, dist_name="test-worker")
    return mp


async def test_subprocess_worker_unary_round_trip(worker_env):
    """A real worker subprocess runs a method and returns the value."""
    mp = _make_multiplexer()
    try:
        resp = await mp.dispatch_unary(RemoteRequest(
            package=_PACKAGE, method="echo", kwargs={"msg": "hi"},
        ))
        assert resp.ok, resp.error
        assert resp.value == {"msg": "echo:hi"}
    finally:
        await mp.shutdown()


async def test_subprocess_worker_streaming(worker_env):
    """Server-streaming method round-trip via subprocess."""
    mp = _make_multiplexer()
    try:
        events = []
        async for ev in mp.dispatch_stream(RemoteRequest(
            package=_PACKAGE, method="counter", kwargs={"n": 3},
        )):
            events.append(ev)
            if ev.get("type") in ("end", "error"):
                break
        items = [e["value"] for e in events if e.get("type") == "item"]
        assert items == [0, 1, 2]
        assert events[-1] == {"type": "end"}
    finally:
        await mp.shutdown()


async def test_subprocess_worker_trace_forwarding(worker_env):
    """trace.emit() in the worker reaches the runtime's trace_forwarder."""
    received: list[tuple[str, dict]] = []

    def forwarder(kind, payload, call_id, source):
        received.append((kind, payload))

    mp = _make_multiplexer(trace_forwarder=forwarder)
    try:
        resp = await mp.dispatch_unary(RemoteRequest(
            package=_PACKAGE, method="trace_then_echo", kwargs={"msg": "x"},
        ))
        assert resp.ok, resp.error
        # Trace frame is fire-and-forget on the worker side; let the
        # multiplexer read loop pick it up.
        await asyncio.sleep(0.2)
        assert ("test_event", {"msg": "x"}) in received
    finally:
        await mp.shutdown()
