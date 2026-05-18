"""Protocol-level integration tests for callable remote calls.

These tests drive the runtime server over HTTP and Socket.IO while using
the in-process worker backend. Subprocess stdio is covered separately in
`test_worker_subprocess.py`.
"""

from __future__ import annotations

import asyncio
import functools

import httpx
import pytest
import socketio

from agentix import Channel, RemoteCallError, RuntimeClient
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.events import CANCEL, STREAM, STREAM_ERROR
from agentix.runtime.shared.models import RemoteRequest
from tests import _worker_target as target
from tests._rpc_helpers import request_for

pytestmark = pytest.mark.asyncio


# ── unary basics ───────────────────────────────────────────────────────


async def test_http_unary_calls_serialized_callable(runtime_module, use_inprocess_worker):
    use_inprocess_worker()
    server, _, _ = runtime_module
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        body = pack(request_for(target.echo, kwargs={"msg": "hi"}).model_dump())
        r = await http.post("/_remote", content=body, headers={"Content-Type": "application/msgpack"})

    assert r.status_code == 200
    assert unpack(r.content) == {"ok": True, "value": {"msg": "echo:hi"}, "error": None}


async def test_http_unary_bad_callable_payload_returns_error(runtime_module, use_inprocess_worker):
    use_inprocess_worker()
    server, _, _ = runtime_module
    req = RemoteRequest(callable_payload=b"not-a-pickle", display_name="bad", shape="unary")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post("/_remote", content=pack(req.model_dump()), headers={"Content-Type": "application/msgpack"})

    assert r.status_code == 200
    resp = unpack(r.content)
    assert resp["ok"] is False
    assert resp["error"]["type"] in {"UnpicklingError", "ValueError"}


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


async def test_remote_rejects_unpickleable_lambda(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
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


# ── streaming + bidi ───────────────────────────────────────────────────


async def test_stream_round_trip_via_socketio(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        items = [t async for t in c.remote(target.counter, n=2)]
    assert items == [0, 1]


async def test_stream_call_context_uses_routable_call_id(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        with c.call_context(call_id="ctx-stream"):
            items = [t async for t in c.remote(target.counter, n=1)]
    assert items == [0]


async def test_socketio_cancel_gets_cancelled_error_ack(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    sio = socketio.AsyncClient()
    errors: asyncio.Queue = asyncio.Queue()

    async def _on_error(data):
        await errors.put(unpack(data))

    sio.on(STREAM_ERROR, _on_error)
    await sio.connect(base_url)
    try:
        call_id = "cancel-me"
        req = request_for(target.slow_counter, call_id=call_id)
        await sio.emit(STREAM, pack(req.model_dump()))
        await asyncio.sleep(0.05)
        await sio.emit(CANCEL, pack({"call_id": call_id}))
        payload = await asyncio.wait_for(errors.get(), timeout=5)
    finally:
        await sio.disconnect()

    assert payload["call_id"] == "cancel-me"
    assert payload["error"]["type"] == "Cancelled"
    assert payload["error"]["cancelled"] is True


async def test_bidi_round_trip_via_socketio(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    inbox: Channel[target.UserMsg] = Channel()

    async def _push() -> None:
        for text in ("hi", "there"):
            await inbox.send(target.UserMsg(text=text))
        await inbox.close()

    async with RuntimeClient(base_url) as c:
        producer = asyncio.create_task(_push())
        replies = [r async for r in c.remote(target.chat, messages=inbox)]
        await producer

    assert [r.text for r in replies] == ["say:hi", "say:there"]


async def test_bidi_accepts_positional_channel_arg(use_inprocess_worker, live_server):
    use_inprocess_worker()
    base_url = await live_server()
    inbox: Channel[target.UserMsg] = Channel()

    async def _push() -> None:
        await inbox.send(target.UserMsg(text="positional"))
        await inbox.close()

    async with RuntimeClient(base_url) as c:
        producer = asyncio.create_task(_push())
        replies = [r async for r in c.remote(target.chat, inbox)]
        await producer

    assert [r.text for r in replies] == ["say:positional"]
