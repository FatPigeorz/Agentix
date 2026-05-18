"""User projects should not need an `agentix.namespace` entry point.

The framework's namespace mechanism is for *plugins* — reusable Python
packages that ship under `agentix.*`. A regular user project that just
wants to dispatch some of its own functions to a sandbox shouldn't have
to declare an entry point, ship under `agentix.<short>`, or follow any
of that ceremony. `c.remote(my_app.tasks.fn, …)` should work for any
importable module.

These tests cover the multiplexer's on-demand registration path:
dispatching to a package not in the entry-point table triggers a probe
of each known venv interpreter; first match registers + spawns a worker.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentix.runtime.server.multiplexer import NamespaceMultiplexer
from agentix.runtime.shared.models import RemoteRequest

TESTS_DIR = Path(__file__).parent
_USER_PACKAGE = "_user_app_target"


async def test_dispatch_to_module_without_entry_point(monkeypatch):
    """Dispatch to `_user_app_target` even though no agentix.namespace
    entry point exists for it.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(TESTS_DIR), existing] if existing else [str(TESTS_DIR)]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))
    # Also inject into this process's sys.path so the in-process
    # `find_spec` fast path finds the module.
    monkeypatch.syspath_prepend(str(TESTS_DIR))

    mp = NamespaceMultiplexer()
    # discover_venvs populates `_venv_pythons` with sys.executable
    # so the slow-path probe has somewhere to land if the fast path misses.
    mp.discover_venvs()
    assert _USER_PACKAGE not in mp._entries  # not yet registered

    try:
        resp = await mp.dispatch_unary(RemoteRequest(
            package=_USER_PACKAGE, method="greet", kwargs={"name": "world"},
        ))
        assert resp.ok, resp.error
        assert resp.value == "hello world"

        # Second method on the same auto-registered package — should reuse
        # the worker, not re-probe.
        resp2 = await mp.dispatch_unary(RemoteRequest(
            package=_USER_PACKAGE, method="add", kwargs={"a": 3, "b": 4},
        ))
        assert resp2.ok, resp2.error
        assert resp2.value == 7

        # The entry should now be cached.
        assert _USER_PACKAGE in mp._entries
    finally:
        await mp.shutdown()


async def test_unimportable_module_returns_package_not_loaded():
    """If a dispatch arrives for a module no venv can import, the
    multiplexer must return PackageNotLoaded — not hang, not crash."""
    mp = NamespaceMultiplexer()
    mp.discover_venvs()

    try:
        resp = await mp.dispatch_unary(RemoteRequest(
            package="this.package.really.does.not.exist",
            method="anything",
        ))
        assert not resp.ok
        assert resp.error is not None
        assert resp.error.type == "PackageNotLoaded"
    finally:
        await mp.shutdown()
