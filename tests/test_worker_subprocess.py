"""End-to-end tests for the subprocess worker path.

Protocol tests exercise the worker client without subprocess stdio.
These tests use the real subprocess worker so the stdio framing and
call correlation run for real.

The target module lives in `tests/_worker_target.py`.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from agentix.runtime.server.worker_client import RuntimeWorkerClient
from agentix.runtime.shared.models import RemoteRequest
from tests import _worker_target as target
from tests._rpc_helpers import request_for


@pytest.fixture
def worker_env():
    """The test target is importable as `tests._worker_target`."""
    return None


def _make_worker_client() -> RuntimeWorkerClient:
    mp = RuntimeWorkerClient()
    mp._python = sys.executable
    return mp


async def test_subprocess_worker_unary_round_trip(worker_env):
    """A real worker subprocess runs a callable and returns the value."""
    mp = _make_worker_client()
    try:
        resp = await mp.call_unary(request_for(target.echo, kwargs={"msg": "hi"}))
        assert resp.ok, resp.error
        assert resp.value == {"msg": "echo:hi"}
    finally:
        await mp.shutdown()


async def test_subprocess_worker_bad_callable_payload_fails_without_hanging(worker_env):
    """A bad callable payload must surface an error, not hang."""
    mp = RuntimeWorkerClient()
    mp._python = sys.executable
    try:
        resp = await asyncio.wait_for(mp.call_unary(RemoteRequest(
            callable_payload=b"not-a-pickle", display_name="bad", shape="unary",
        )), timeout=20)
    finally:
        await mp.shutdown()
    assert not resp.ok
    assert resp.error is not None


async def test_subprocess_worker_streaming(worker_env):
    """Server-streaming function round-trip via subprocess."""
    mp = _make_worker_client()
    try:
        events = []
        async for ev in mp.call_stream(request_for(target.counter, kwargs={"n": 3})):
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
    mp = _make_worker_client()
    try:
        # Force the worker to spawn by issuing one unary first.
        resp = await mp.call_unary(request_for(target.echo, kwargs={"msg": "warm"}))
        assert resp.ok

        # Start a long stream, kill the worker before it completes.
        gen = mp.call_stream(request_for(target.counter, kwargs={"n": 1_000_000}))
        events: list[dict] = []

        async def _drain() -> None:
            async for ev in gen:
                events.append(ev)
                if ev.get("type") in ("end", "error"):
                    return

        consumer = asyncio.create_task(_drain())
        # Let a few items flow before pulling the rug.
        await asyncio.sleep(0.1)

        worker = mp._worker
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
