"""Namespace round-trip: plugin sandbox-side namespace talks to a
plugin host-side namespace handler."""

from __future__ import annotations

import asyncio
import logging

import pytest

from agentix import AsyncClientNamespace, RuntimeClient
from tests._namespace_target import (
    echo_via_namespace,
    emit_formatted_log,
    emit_log_line,
    emit_log_with_exception,
    emit_log_with_extra,
)


class _EchoHost(AsyncClientNamespace):
    def __init__(self) -> None:
        super().__init__("/plugin-test")
        self.seen: list = []

    async def on_echo(self, data):
        self.seen.append(data)
        await self.emit(
            "echo:result",
            {
                "request_id": data["request_id"],
                "value": {"echoed": data["data"]},
            },
        )


@pytest.mark.asyncio
async def test_plugin_namespace_round_trip(live_server):
    base_url = await live_server()
    host_ns = _EchoHost()

    client = RuntimeClient(base_url)
    client.register_namespace(host_ns)
    async with client as c:
        result = await c.remote(echo_via_namespace, {"hello": 1})

    assert result == {"echoed": {"hello": 1}}
    assert len(host_ns.seen) == 1
    assert host_ns.seen[0]["data"] == {"hello": 1}


@pytest.mark.asyncio
async def test_log_records_arrive_on_host(live_server):
    """Verify the full /log experience: plain messages, %-format args,
    extras dicts, and exception tracebacks all reach the host intact.
    Logger names + levelno round-trip so host filters see the sandbox
    record as if it had originated locally.
    """
    base_url = await live_server()

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name == "namespace_target":
                captured.append(record)

    target_logger = logging.getLogger("namespace_target")
    target_logger.setLevel(logging.INFO)
    handler = _Capture()
    target_logger.addHandler(handler)
    try:
        async with RuntimeClient(base_url) as c:
            await c.remote(emit_log_line, "from sandbox", "INFO")
            await c.remote(emit_formatted_log, "user %s acted on %s", "alice", "doc-7")
            await c.remote(emit_log_with_extra, "with extras", request_id="r-42", attempt=3)
            await c.remote(emit_log_with_exception, "caught one")
            # Let the /log pipe drain.
            await asyncio.sleep(0.5)
    finally:
        target_logger.removeHandler(handler)

    messages = {r.getMessage(): r for r in captured}

    # Plain log line.
    assert "from sandbox" in messages

    # %-style formatting: getMessage() already ran in the sandbox.
    assert "user alice acted on doc-7" in messages

    # extras kwargs survive — they show up as attributes on the record.
    extras_rec = messages.get("with extras")
    assert extras_rec is not None
    assert getattr(extras_rec, "request_id", None) == "r-42"
    assert getattr(extras_rec, "attempt", None) == 3

    # logger.exception() ships the formatted traceback in exc_text.
    exc_rec = messages.get("caught one")
    assert exc_rec is not None
    assert exc_rec.exc_text and "ValueError: kaboom" in exc_rec.exc_text
