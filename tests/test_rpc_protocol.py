"""Protocol-level integration tests for remote calls.

Drives the runtime server over Socket.IO using the in-process worker
backend. Subprocess stdio is covered separately in
`test_worker_subprocess.py`.
"""

from __future__ import annotations

import asyncio
import functools

import httpx
import pytest
import socketio

from agentix import RemoteCallError, RuntimeClient
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.models import RemoteRequest
from tests import _worker_target as target
from tests._rpc_helpers import request_for

pytestmark = pytest.mark.asyncio


# ── basics ─────────────────────────────────────────────────────────────


async def test_http_remote_endpoint_is_not_registered(runtime_module):
    server, _, _ = runtime_module
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post("/_remote", content=b"")

    assert r.status_code == 404


async def test_socketio_call_serialized_callable(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    sio = socketio.AsyncClient()
    results: asyncio.Queue = asyncio.Queue()

    async def _on_result(data):
        await results.put(unpack(data))

    sio.on("call:result", _on_result)
    await sio.connect(base_url)
    try:
        req = request_for(target.echo, kwargs={"msg": "hi"}, call_id="call-ok")
        await sio.emit("call", pack(req.model_dump()))
        payload = await asyncio.wait_for(results.get(), timeout=5)
    finally:
        await sio.disconnect()

    assert payload["call_id"] == "call-ok"
    import pickle

    result = pickle.loads(payload["value"])
    assert result.msg == "echo:hi"


async def test_socketio_bad_callable_returns_error(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    sio = socketio.AsyncClient()
    errors: asyncio.Queue = asyncio.Queue()

    async def _on_error(data):
        await errors.put(unpack(data))

    sio.on("call:error", _on_error)
    await sio.connect(base_url)
    try:
        import pickle

        from agentix.runtime.shared.callables import RemoteCallable

        # Garbage base64 that can't be decoded into a callable.
        req = RemoteRequest(
            callable=RemoteCallable("not-valid-base64-pickle"),
            arguments=pickle.dumps(((), {})),
            call_id="call-bad",
        )
        await sio.emit("call", pack(req.model_dump()))
        payload = await asyncio.wait_for(errors.get(), timeout=5)
    finally:
        await sio.disconnect()

    assert payload["call_id"] == "call-bad"
    # b64decode can raise binascii.Error or pickle can raise UnpicklingError.
    assert payload["error"]["type"] in {"UnpicklingError", "ValueError", "Error", "EOFError"}


async def test_client_remote_round_trip(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        result = await c.remote(target.echo, msg="hello")
    assert result.msg == "echo:hello"


async def test_client_remote_raises_on_impl_error(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        with pytest.raises(RemoteCallError):
            await c.remote(target.boom)


# ── seamless callable forms ────────────────────────────────────────────


async def test_remote_rejects_unimportable_lambda(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        # pickle can't serialize a local lambda, so the host-side
        # `RemoteCallable._resolve(fn)` raises before the call leaves.
        with pytest.raises(Exception):
            await c.remote(lambda x: x + 1, 41)


async def test_remote_accepts_partial(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    add_three = functools.partial(target.add, 3)
    async with RuntimeClient(base_url) as c:
        assert await c.remote(add_three, 4) == 7


async def test_remote_accepts_bound_method(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        result = await c.remote(target.prefixer.bound, "hello")
    assert result.msg == "bound:instance:hello"


async def test_remote_accepts_callable_instance(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        result = await c.remote(target.prefixer, "hello")
    assert result.msg == "instance:hello"


# ── cancel ────────────────────────────────────────────────────────────


async def test_socketio_cancel_returns_cancelled_error(use_inprocess_worker, live_server):
    """Cancelling an in-flight call yields a Cancelled error."""
    use_inprocess_worker()
    base_url = await live_server()
    sio = socketio.AsyncClient()
    errors: asyncio.Queue = asyncio.Queue()

    async def _on_error(data):
        await errors.put(unpack(data))

    sio.on("call:error", _on_error)
    await sio.connect(base_url)
    try:
        # Use a slow remote call. asyncio.sleep is convenient — it's
        # importable and async; we just need it to outlast the cancel.
        import asyncio as _asyncio
        import pickle

        from agentix.runtime.shared.callables import RemoteCallable

        req = RemoteRequest(
            callable=RemoteCallable._resolve(_asyncio.sleep),
            arguments=pickle.dumps(((5.0,), {})),
            call_id="cancel-me",
        )
        await sio.emit("call", pack(req.model_dump()))
        await asyncio.sleep(0.1)
        await sio.emit("cancel", pack({"call_id": "cancel-me"}))
        payload = await asyncio.wait_for(errors.get(), timeout=5)
    finally:
        await sio.disconnect()

    assert payload["call_id"] == "cancel-me"
    assert payload["error"]["type"] == "Cancelled"
    assert payload["error"]["cancelled"] is True
