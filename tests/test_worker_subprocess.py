"""End-to-end tests for the subprocess worker path.

In-process tests (test_namespace_protocol.py) exercise the multiplexer
through its InProcessWorker backend — same protocol, no subprocess.
These tests use the real SubprocessWorker so the stdio framing + RPC
correlation run for real.

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

from agentix.runtime.server.multiplexer import NamespaceMultiplexer
from agentix.runtime.shared.models import RemoteRequest

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


def _make_multiplexer() -> NamespaceMultiplexer:
    mp = NamespaceMultiplexer()
    mp._register_subprocess(_PACKAGE, _TARGET, sys.executable)
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


async def test_subprocess_worker_bad_target_fails_without_hanging(worker_env):
    """A worker that exits before READY must surface an error, not hang startup."""
    mp = NamespaceMultiplexer()
    mp._register_subprocess(
        "agentix.missing",
        "agentix.definitely_missing:Nope",
        sys.executable,
    )
    try:
        # Worker subprocess pays pydantic_core's one-time init cost
        # (~4s on some machines) before it can fail. Budget = the
        # multiplexer's own _WORKER_START_TIMEOUT (15s) plus a hair.
        with pytest.raises(RuntimeError, match="failed to boot|exited before ready"):
            await asyncio.wait_for(mp.dispatch_unary(RemoteRequest(
                package="agentix.missing", method="x",
            )), timeout=20)
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


async def test_subprocess_worker_death_fails_in_flight_stream(worker_env):
    """Killing the worker mid-stream surfaces WorkerExited to the caller —
    PROTOCOL.md invariant #5 (no call hangs indefinitely)."""
    mp = _make_multiplexer()
    try:
        # Force the worker to spawn by issuing one unary first.
        resp = await mp.dispatch_unary(RemoteRequest(
            package=_PACKAGE, method="echo", kwargs={"msg": "warm"},
        ))
        assert resp.ok

        # Start a long stream, kill the worker before it completes.
        gen = mp.dispatch_stream(RemoteRequest(
            package=_PACKAGE, method="counter", kwargs={"n": 1_000_000},
        ))
        events: list[dict] = []

        async def _drain() -> None:
            async for ev in gen:
                events.append(ev)
                if ev.get("type") in ("end", "error"):
                    return

        consumer = asyncio.create_task(_drain())
        # Let a few items flow before pulling the rug.
        await asyncio.sleep(0.1)

        entry = mp._entries[_PACKAGE]   # noqa: SLF001  (test-internal probe)
        worker = entry.worker
        assert worker is not None
        # Reach down for the live subprocess and SIGKILL it.
        proc = worker._proc                                       # type: ignore[attr-defined]
        assert proc is not None
        proc.kill()

        await asyncio.wait_for(consumer, timeout=5)
    finally:
        await mp.shutdown()

    terminal = events[-1]
    assert terminal["type"] == "error"
    assert terminal["error"]["type"] == "WorkerExited"
