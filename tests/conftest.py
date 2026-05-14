"""Shared fixtures for agentix tests."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
import textwrap
from pathlib import Path
from typing import Callable

import pytest


# ── network / runtime setup ──────────────────────────────────────


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def runtime_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated runtime: tmp /mnt + tmp upload root + reloaded modules.

    Returns (server_module, mount_root, upload_root).
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    upload_root = tmp_path / "workspace"
    upload_root.mkdir()
    monkeypatch.setenv("AGENTIX_CLOSURE_MOUNT_ROOT", str(mount_root))
    monkeypatch.setenv("AGENTIX_UPLOAD_ROOT", str(upload_root))

    for mod in ("agentix.runtime.builtins", "agentix.runtime.server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])

    from agentix.runtime import server

    return server, mount_root, upload_root


# ── closure-on-disk builder ──────────────────────────────────────


def _write_pkg(py_root: Path, package: str, init_src: str, impl_src: str, register_src: str) -> None:
    """Drop a Python package tree at `py_root/<package-path>/` with PEP 420
    namespace-package treatment for parents (only the leaf has __init__.py).
    """
    parts = package.split(".")
    parent = py_root
    for p in parts[:-1]:
        parent = parent / p
        parent.mkdir(parents=True, exist_ok=True)
    leaf = parent / parts[-1]
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / "__init__.py").write_text(init_src)
    (leaf / "_impl.py").write_text(impl_src)
    (leaf / "_register.py").write_text(register_src)


@pytest.fixture
def mount_package(runtime_module) -> Callable[..., Path]:
    """Lay out a closure mount: `<mount>/entry/{manifest.json, python/<pkg>/}`.

    Usage:
        mount = mount_package(
            "echo",
            package="agentix_closures.echo",
            init_src="...",
            impl_src="...",
            register_src="...",
        )
    """
    server, mount_root, _ = runtime_module

    def _mount(
        dirname: str,
        *,
        package: str,
        init_src: str,
        impl_src: str,
        register_src: str,
        abi: int = 1,
        version: str = "0.1.0",
        extra_manifest: dict | None = None,
    ) -> Path:
        mount = mount_root / dirname
        entry = mount / "entry"
        entry.mkdir(parents=True)
        manifest = {
            "abi": abi,
            "name": package.rsplit(".", 1)[-1].replace("_", "-"),
            "version": version,
            "package": package,
            **(extra_manifest or {}),
        }
        (entry / "manifest.json").write_text(json.dumps(manifest))
        _write_pkg(
            entry / "python",
            package=package,
            init_src=init_src,
            impl_src=impl_src,
            register_src=register_src,
        )
        return mount

    return _mount


# ── reusable closure sources ─────────────────────────────────────


ECHO_INIT = textwrap.dedent(
    """\
    from dataclasses import dataclass

    @dataclass
    class EchoResult:
        msg: str

    def echo(msg: str) -> EchoResult:
        raise NotImplementedError("call via RuntimeClient.remote(echo, ...)")
    """
)

ECHO_IMPL = textwrap.dedent(
    """\
    from . import EchoResult

    def echo(msg: str) -> EchoResult:
        return EchoResult(msg=f"echo:{msg}")
    """
)

ECHO_REGISTER = textwrap.dedent(
    """\
    from agentix.dispatch import Dispatcher
    from . import echo
    from ._impl import echo as _echo_impl

    def register() -> Dispatcher:
        d = Dispatcher()
        d.bind(echo, _echo_impl)
        return d
    """
)


@pytest.fixture
def mount_echo(mount_package) -> Callable[..., Path]:
    """Mount the canonical 'echo' closure used across many tests.

    Returns the mount dir. After this fixture runs and the runtime's
    `_auto_load()` is called, `from agentix_closures.echo import echo`
    becomes importable in the test process (because _auto_load prepends
    the closure's `entry/python` to sys.path).
    """
    def _mount(dirname: str = "echo", *, package: str = "agentix_closures.echo") -> Path:
        return mount_package(
            dirname,
            package=package,
            init_src=ECHO_INIT,
            impl_src=ECHO_IMPL,
            register_src=ECHO_REGISTER,
        )
    return _mount


@pytest.fixture(autouse=True)
def _purge_test_packages():
    """Per-test cleanup: drop any agentix_closures.* modules imported by the
    runtime's _auto_load so the next test's fresh sys.path takes effect.
    """
    yield
    for mod in list(sys.modules):
        if mod == "agentix_closures" or mod.startswith("agentix_closures."):
            sys.modules.pop(mod, None)


# ── live uvicorn server (for Socket.IO e2e tests) ────────────────


@pytest.fixture
async def live_server(runtime_module):
    """Yields an async `start()` callable that boots uvicorn on a free port
    serving the runtime's combined FastAPI+Socket.IO ASGI app.

    Test order:
        1. mount_package(...)        # populate /mnt
        2. base_url = await start()  # uvicorn starts; lifespan runs _auto_load
        3. connect via RuntimeClient(base_url) etc.

    The server is torn down in fixture finalisation.
    """
    import contextlib as _ctx
    import socket as _socket

    import httpx as _httpx
    import uvicorn

    server, _, _ = runtime_module
    state: dict = {"task": None, "srv": None}

    async def _start() -> str:
        if state["task"] is not None:
            raise RuntimeError("live_server already started")
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = uvicorn.Config(
            server.app, host="127.0.0.1", port=port,
            log_level="error", lifespan="on",
        )
        srv = uvicorn.Server(config)
        state["srv"] = srv
        state["task"] = asyncio.create_task(srv.serve())
        base_url = f"http://127.0.0.1:{port}"
        async with _httpx.AsyncClient() as c:
            for _ in range(100):
                try:
                    r = await c.get(f"{base_url}/health")
                    if r.status_code == 200:
                        return base_url
                except (_httpx.ConnectError, _httpx.ReadError):
                    pass
                await asyncio.sleep(0.05)
        raise RuntimeError("live_server did not become healthy in 5s")

    try:
        yield _start
    finally:
        if state["srv"] is not None:
            state["srv"].should_exit = True
            with _ctx.suppress(BaseException):
                await asyncio.wait_for(state["task"], timeout=5)
