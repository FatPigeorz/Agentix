"""Extension-point semantics tests.

Two surfaces qualify as plugin axes (entry-point discovered, `Registry[T]`-backed):

  * `agentix.namespace` — covered by `test_namespace.py` / `test_namespace_protocol.py`
  * `agentix.deployment` — covered below (select-one by name)

The host-side trace-sink API (`register_sink` / `unregister_sink`) is
a plain Python hook, not a plugin axis — sanity-tested below alongside
the deployment plugin tests.
"""

from __future__ import annotations

import pytest

# ── deployment (select-one plugin axis) ──────────────────────────────


def test_deployment_register_and_load(monkeypatch):
    from agentix.deployment.base import (
        Deployment,
        deployments,
        load_deployment,
        register_deployment,
    )

    class FakeDep:
        async def create(self, config): ...   # noqa: ARG002
        async def delete(self, sandbox_id): ...   # noqa: ARG002
        async def get(self, sandbox_id): ...   # noqa: ARG002

    monkeypatch.setattr(deployments(), "_walk_entry_points", lambda: [])
    deployments().reset()
    register_deployment("fake", FakeDep)
    cls = load_deployment("fake")
    assert cls is FakeDep
    assert isinstance(FakeDep(), Deployment)  # structural — Protocol check


def test_deployment_unknown_name_raises(monkeypatch):
    from agentix.deployment.base import deployments, load_deployment

    monkeypatch.setattr(deployments(), "_walk_entry_points", lambda: [])
    deployments().reset()
    with pytest.raises(KeyError, match="agentix.deployment"):
        load_deployment("never-registered")


# ── trace sinks (host-side fan-out API) ──────────────────────────────


def test_trace_sinks_fan_out():
    from agentix import trace
    seen_a: list = []
    seen_b: list = []

    def sink_a(kind, payload, call_id, source):
        seen_a.append((kind, payload))

    def sink_b(kind, payload, call_id, source):
        seen_b.append(kind)

    trace.register_sink(sink_a)
    trace.register_sink(sink_b)
    try:
        trace.emit("x", {"v": 1})
        assert ("x", {"v": 1}) in seen_a
        assert "x" in seen_b
    finally:
        trace.unregister_sink(sink_a)
        trace.unregister_sink(sink_b)


def test_trace_one_sink_failure_does_not_block_others():
    from agentix import trace
    delivered: list = []

    def good(kind, payload, call_id, source):
        delivered.append(kind)

    def bad(kind, payload, call_id, source):
        raise RuntimeError("sink down")

    trace.register_sink(bad)
    trace.register_sink(good)
    try:
        trace.emit("y", {})
        assert "y" in delivered
    finally:
        trace.unregister_sink(bad)
        trace.unregister_sink(good)


def test_trace_no_sinks_is_noop():
    """emit() with no sinks must not raise — namespaces running outside
    a runtime should be able to call trace.emit() freely."""
    from agentix import trace
    snapshot = list(trace._sinks)
    trace._sinks.clear()
    try:
        trace.emit("z", {})  # must not raise
    finally:
        trace._sinks.extend(snapshot)


