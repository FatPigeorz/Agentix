"""Per-axis plugin tests: deployment, trace_sink, spec_resolver, wire_pattern.

Each axis test verifies its specific semantics on top of the shared
`Registry[T]` machinery: select-one for deployment, fan-out for trace
sinks, chain-of-responsibility for spec resolvers, ordered merge for
wire patterns.
"""

from __future__ import annotations

import inspect

import pytest

# ── deployment (select-one) ──────────────────────────────────────────


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


# ── trace sinks (fan-out) ────────────────────────────────────────────


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

    # Register `bad` first to confirm later sinks still fire.
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
    # _sinks is module-private; access it for the assertion only.
    assert isinstance(trace._sinks, list)
    snapshot = list(trace._sinks)
    trace._sinks.clear()
    try:
        trace.emit("z", {})  # must not raise
    finally:
        trace._sinks.extend(snapshot)


# ── spec resolvers (chain) ───────────────────────────────────────────


def test_spec_resolver_chain_priority_order(monkeypatch):
    from agentix.cli._resolve import (
        NamespaceSpec,
        register_spec_resolver,
        resolve_spec,
        spec_resolvers,
    )

    class HighPriority:
        priority = 100

        def resolve(self, spec):
            if spec == "shared":
                return NamespaceSpec(short="hi", kind="pypi", pypi_dist="hi-pkg")
            return None

    class LowPriority:
        priority = 10

        def resolve(self, spec):
            if spec == "shared":
                return NamespaceSpec(short="lo", kind="pypi", pypi_dist="lo-pkg")
            return None

    monkeypatch.setattr(spec_resolvers(), "_walk_entry_points", lambda: [])
    spec_resolvers().reset()
    register_spec_resolver("hi", HighPriority)
    register_spec_resolver("lo", LowPriority)

    # Higher priority wins even though it was registered first.
    result = resolve_spec("shared")
    assert result.short == "hi"


def test_spec_resolver_falls_through_on_none(monkeypatch):
    from agentix.cli._resolve import (
        NamespaceSpec,
        register_spec_resolver,
        resolve_spec,
        spec_resolvers,
    )

    class AlwaysSkip:
        priority = 100
        def resolve(self, spec):
            return None

    class Claims:
        priority = 50
        def resolve(self, spec):
            return NamespaceSpec(short="x", kind="pypi", pypi_dist="x")

    monkeypatch.setattr(spec_resolvers(), "_walk_entry_points", lambda: [])
    spec_resolvers().reset()
    register_spec_resolver("skip", AlwaysSkip)
    register_spec_resolver("claim", Claims)
    assert resolve_spec("anything").short == "x"


def test_spec_resolver_no_match_raises(monkeypatch):
    from agentix.cli._resolve import register_spec_resolver, resolve_spec, spec_resolvers

    class AlwaysNone:
        priority = 10
        def resolve(self, spec):
            return None

    monkeypatch.setattr(spec_resolvers(), "_walk_entry_points", lambda: [])
    spec_resolvers().reset()
    register_spec_resolver("none", AlwaysNone)
    with pytest.raises(SystemExit, match="no spec resolver claimed"):
        resolve_spec("anything")


# ── wire patterns (ordered merge) ────────────────────────────────────


def test_wire_pattern_registered_pattern_outranks_builtin():
    from agentix.wire import (
        WirePattern,
        _reset_patterns,
        register_pattern,
        select_pattern,
    )

    class HeadPattern(WirePattern):
        name = "head"

        @classmethod
        def matches(cls, sig):
            return True  # always wins if it gets a vote

        def bind(self, sig):
            pass

        def client_invoke(self, client, fn, sig, args, kwargs):
            raise NotImplementedError

    try:
        register_pattern(HeadPattern)

        # Any plain unary signature now resolves to HeadPattern, not Unary.
        def sample(x: int) -> str: ...   # noqa: ARG001
        sig = inspect.signature(sample, eval_str=True)
        assert select_pattern(sig) is HeadPattern
    finally:
        _reset_patterns()


def test_wire_pattern_falls_back_to_unary_when_no_match():
    from agentix.wire import UnaryPattern, _reset_patterns, select_pattern

    _reset_patterns()
    def plain(x: int) -> int: ...   # noqa: ARG001
    sig = inspect.signature(plain, eval_str=True)
    assert select_pattern(sig) is UnaryPattern


# ── plugins CLI happy path ───────────────────────────────────────────


def test_plugins_cli_lists_known_groups(capsys):
    from agentix.cli.plugins import main as plugins_main

    rc = plugins_main([])
    out = capsys.readouterr().out
    assert rc in (0, 1)  # nonzero only if some group has load errors
    for group in ("agentix.namespace", "agentix.deployment",
                  "agentix.trace_sink", "agentix.spec_resolver",
                  "agentix.wire_pattern", "agentix.cli"):
        assert group in out
