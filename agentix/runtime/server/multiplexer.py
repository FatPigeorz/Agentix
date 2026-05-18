"""Multiplexer — manages one worker subprocess per dispatched package.

Sits between the FastAPI/Socket.IO transports and per-package workers.
A worker is a child Python process that loads one Python module + runs
its async functions through a `Dispatcher`. The multiplexer:

  1. **Spawns workers lazily.** First dispatch for a package forks
     `python -m agentix.runtime.server.worker --target <pkg>` with the
     bundle's Python interpreter, plumbs stdin/stdout for frames.
  2. **Routes frames** between transports (POST /_remote, Socket.IO)
     and workers, correlated by `call_id`.
  3. **Tears down** workers on shutdown.

There's no "namespace" concept here — any importable Python module is
a valid worker target. On first dispatch to an unseen package, the
multiplexer auto-probes the runtime venv for whether the module
imports, and spawns a worker if so.

Two worker variants share one routing layer:

  - `_SubprocessWorker` — `target_module` + python interpreter path;
    real isolated process per package. Production path.
  - `_InProcessWorker` — already-bound Dispatcher held in this
    process. Test fixture path; lets pytest exercise the multiplexer's
    wire protocol without forcing every test class into a real
    subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import logging
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentix.dispatch import Dispatcher
from agentix.runtime.shared import frames as F
from agentix.runtime.shared.models import RemoteError, RemoteRequest, RemoteResponse
from agentix.runtime.shared.rpc import read_frame, write_frame

logger = logging.getLogger("agentix.runtime.server.multiplexer")

_WORKER_START_TIMEOUT = 15.0

class _WorkerLike(Protocol):
    """Both _InProcessWorker and _SubprocessWorker satisfy this surface;
    the multiplexer routes through it without caring which backs the call."""

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse: ...
    def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]: ...
    def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def shutdown(self) -> None: ...


# ── entries (one per discovered namespace) ──────────────────────────


@dataclass
class _PackageEntry:
    """One known dispatch target. Either `target`+`python` (subprocess
    path) or `dispatcher` (in-process path) is set, never both."""

    package: str                     # python import path, e.g. "agentix.bash"

    # Subprocess fields
    target: str | None = None         # "module" or "module:attr"
    python: str | None = None         # path to interpreter for this venv
    bin_dir: Path | None = None       # bin/ prepended to worker PATH

    # In-process fields (tests)
    dispatcher: Dispatcher | None = None

    # Spawned worker state (lazy)
    worker: _WorkerLike | None = field(default=None, repr=False)
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


# ── worker variants ────────────────────────────────────────────────


class _InProcessWorker:
    """In-process worker — for tests. Routes through Dispatcher directly."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        return await self._dispatcher.dispatch(request)

    async def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        async for ev in self._dispatcher.dispatch_stream(request):
            yield ev

    async def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        # Coerce input items via the dispatcher's bound adapter so the
        # impl receives typed values, matching subprocess-worker semantics
        # (where the worker process owns the same coercion).
        adapter = self._dispatcher.input_adapter_for(request.method)  # type: ignore[arg-type]

        async def _coerced():
            async for raw in input_iter:
                if adapter is not None:
                    raw = adapter.validate_python(raw)
                yield raw

        async for ev in self._dispatcher.dispatch_bidi(request, _coerced()):
            yield ev

    async def shutdown(self) -> None:
        return


class _SubprocessWorker:
    """Subprocess worker — spawns `python -m agentix.runtime.server.worker`."""

    def __init__(
        self,
        package: str,
        target: str,
        python: str,
        ns_bin_dir: Path | None = None,
    ) -> None:
        self._package = package
        self._target = target
        self._python = python
        # If provided, prepended to the worker's PATH so user code can call
        # Nix-provided binaries (`git`, `claude`, …) by bare name without
        # knowing the absolute /nix/<short>/bin path.
        self._ns_bin_dir = ns_bin_dir

        self._proc: asyncio.subprocess.Process | None = None
        self._send_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._boot_error: dict[str, Any] | None = None
        self._read_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

        # Per-call state: futures for unary, queues for stream/bidi.
        self._unary: dict[str, asyncio.Future] = {}
        self._streams: dict[str, asyncio.Queue] = {}
        # Best-effort cancel sends kept alive (asyncio's task GC).
        self._cancel_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        env = dict(os.environ)
        if self._ns_bin_dir is not None:
            # Prepend the namespace's bin dir so subprocess.run("git", …)
            # in user code resolves to /nix/<short>/bin/git transparently.
            existing = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            env["PATH"] = f"{self._ns_bin_dir}:{existing}"
        self._proc = await asyncio.create_subprocess_exec(
            self._python, "-m", "agentix.runtime.server.worker", "--target", self._target,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,  # logs straight through to runtime stderr
            env=env,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        ready_task = asyncio.create_task(self._ready.wait())
        closed_task = asyncio.create_task(self._closed.wait())
        assert self._proc is not None
        proc_task = asyncio.create_task(self._proc.wait())
        try:
            done, pending = await asyncio.wait(
                {ready_task, closed_task, proc_task},
                timeout=_WORKER_START_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                await self.shutdown()
                raise TimeoutError(
                    f"worker for {self._package!r} did not become ready "
                    f"within {_WORKER_START_TIMEOUT:.0f}s"
                )
            if ready_task not in done:
                rc = self._proc.returncode
                await self.shutdown()
                detail = f"exit code {rc}" if rc is not None else "stdout closed"
                raise RuntimeError(
                    f"worker for {self._package!r} exited before ready ({detail})"
                )
        finally:
            for task in (ready_task, closed_task, proc_task):
                if not task.done():
                    task.cancel()
        if self._boot_error is not None:
            await self.shutdown()
            raise RuntimeError(
                f"worker for {self._package!r} failed to boot: "
                f"{self._boot_error.get('type')}: {self._boot_error.get('message')}"
            )

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                frame = await read_frame(self._proc.stdout)
                if frame is None:
                    break
                self._on_frame(frame)
        except Exception:
            logger.exception("worker %r read loop crashed", self._package)
        finally:
            self._closed.set()
            # Fail any pending calls.
            for fut in list(self._unary.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(f"worker {self._package!r} exited"))
            self._unary.clear()
            for q in list(self._streams.values()):
                q.put_nowait({"type": "error", "error": RemoteError(
                    type="WorkerExited", message=f"worker {self._package!r} exited",
                ).model_dump()})

    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == F.READY:
            self._ready.set()
        elif kind == F.BOOT_ERROR:
            self._boot_error = frame.get("error") or {"type": "Unknown", "message": ""}
            self._ready.set()
        elif kind == F.RESULT:
            cid = frame.get("call_id", "")
            fut = self._unary.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=True, value=frame.get("value")))
        elif kind == F.ERROR:
            cid = frame.get("call_id", "")
            err_payload = frame.get("error") or {"type": "Unknown", "message": ""}
            err = RemoteError(**err_payload)
            fut = self._unary.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=False, error=err))
                return
            q = self._streams.get(cid)
            if q is not None:
                q.put_nowait({"type": "error", "error": err_payload})
        elif kind == F.STREAM_ITEM:
            q = self._streams.get(frame.get("call_id", ""))
            if q is not None:
                q.put_nowait({"type": "item", "value": frame.get("value")})
        elif kind == F.STREAM_END:
            q = self._streams.get(frame.get("call_id", ""))
            if q is not None:
                q.put_nowait({"type": "end"})
        else:
            logger.warning("worker %r: unknown frame %r", self._package, kind)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        async with self._send_lock:
            await write_frame(self._proc.stdin, payload)

    def _call_frame(self, kind: str, cid: str, request: RemoteRequest) -> dict[str, Any]:
        return {
            "type": F.CALL, "kind": kind, "call_id": cid,
            "method": request.method, "args": request.args, "kwargs": request.kwargs,
        }

    def _schedule_cancel(self, cid: str) -> None:
        """Tell the worker to abort a call. Best-effort; the send is fired
        off as a background task tracked in `_cancel_tasks` so asyncio
        doesn't GC the reference before the frame lands."""
        t = asyncio.create_task(self._send_cancel(cid))
        self._cancel_tasks.add(t)
        t.add_done_callback(self._cancel_tasks.discard)

    async def _send_cancel(self, cid: str) -> None:
        try:
            await self._send({"type": F.CANCEL, "call_id": cid})
        except Exception:
            logger.debug("cancel send failed for call %r", cid)

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        cid = request.call_id or _new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._unary[cid] = fut
        try:
            await self._send(self._call_frame(F.KIND_UNARY, cid, request))
            return await fut
        finally:
            self._unary.pop(cid, None)
            # Caller bailed before the worker replied — tell it to stop.
            if not fut.done():
                self._schedule_cancel(cid)

    async def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        cid = request.call_id or _new_id()
        q: asyncio.Queue = asyncio.Queue()
        self._streams[cid] = q
        terminated = False
        try:
            await self._send(self._call_frame(F.KIND_STREAM, cid, request))
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in ("end", "error"):
                    terminated = True
                    return
        finally:
            self._streams.pop(cid, None)
            if not terminated:
                self._schedule_cancel(cid)

    async def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        cid = request.call_id or _new_id()
        q: asyncio.Queue = asyncio.Queue()
        self._streams[cid] = q
        input_task: asyncio.Task | None = None
        terminated = False
        try:
            await self._send(self._call_frame(F.KIND_BIDI, cid, request))

            async def _pump_input():
                try:
                    async for item in input_iter:
                        await self._send({"type": F.BIDI_IN, "call_id": cid, "item": item})
                finally:
                    await self._send({"type": F.BIDI_END_IN, "call_id": cid})

            input_task = asyncio.create_task(_pump_input())
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in ("end", "error"):
                    terminated = True
                    return
        finally:
            self._streams.pop(cid, None)
            if input_task is not None:
                input_task.cancel()
                with contextlib.suppress(BaseException):
                    await input_task
            if not terminated:
                self._schedule_cancel(cid)

    async def shutdown(self) -> None:
        if self._proc is None:
            return
        try:
            await self._send({"type": F.SHUTDOWN})
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except TimeoutError:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except TimeoutError:
                self._proc.kill()
                # SIGKILL is guaranteed; wait so we reap the zombie.
                await self._proc.wait()
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(BaseException):
                await self._read_task


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


# ── multiplexer ─────────────────────────────────────────────────────


class NamespaceMultiplexer:
    """Owns the package → worker mapping; routes dispatches.

    On first dispatch to a package not in `_entries`, the multiplexer
    auto-registers it: probe the runtime's Python (and any aux venvs)
    for whether `<package>` is importable; if so, spawn a worker. No
    entry-point declaration is required for any dispatch target — any
    importable Python module works.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _PackageEntry] = {}    # package → entry
        # Discovered venv interpreters. On unknown-package dispatch we
        # try each one in turn — first that can import the module wins.
        self._venv_pythons: list[tuple[str, Path | None]] = []  # (python, bin_dir)

    # ── discovery ───────────────────────────────────────────────────

    def discover_venvs(self) -> None:
        """Record the venv interpreter(s) we'll probe for unknown
        packages. In a bundle image that's `/nix/runtime/bin/python`;
        in dev / test it's `sys.executable`.

        Tests using `_register_inprocess()` skip this entirely.
        """
        nix_runtime = Path("/nix/runtime")
        if nix_runtime.is_dir():
            self._record_nix_runtime(nix_runtime)
        else:
            self._venv_pythons.append((sys.executable, None))

    def _record_nix_runtime(self, venv: Path) -> None:
        python = venv / "bin" / "python"
        bin_dir = venv / "bin"
        if python.exists():
            self._venv_pythons.append((str(python), bin_dir))

    # ── test-only registration paths ────────────────────────────────
    # Underscore-prefixed: production code uses `discover_venvs()` +
    # `_auto_register` (lazy). Tests use these to bypass discovery.

    def _register_inprocess(self, target: Any) -> None:
        """Bind `target` (a module or class) in-process via a Dispatcher
        held in this process. Bypasses subprocess + venv discovery.

        Used in pytest fixtures so test classes can act as dispatchable
        targets without needing an importable module + real subprocess.
        """
        package = target.__name__ if inspect.ismodule(target) else target.__module__
        self._entries[package] = _PackageEntry(
            package=package, dispatcher=Dispatcher(target),
        )

    def _register_subprocess(
        self,
        package: str,
        target: str,
        python: str,
        *,
        bin_dir: Path | None = None,
    ) -> None:
        """Register a subprocess-backed entry explicitly. Tests use this
        to exercise the real subprocess path; production code auto-
        registers on first dispatch."""
        self._entries[package] = _PackageEntry(
            package=package, target=target, python=python, bin_dir=bin_dir,
        )

    def has(self, package: str) -> bool:
        return package in self._entries

    # ── on-demand registration for arbitrary modules ────────────────

    def _auto_register(self, package: str) -> bool:
        """Try to register `package` against any known venv.

        Fast path: this Python tries `importlib.util.find_spec` in-process
        (no subprocess). Slow path: any aux venv gets a `python -c
        'import <pkg>'` probe.

        Returns True iff the module was registered.
        """
        # Fast path: this Python.
        try:
            if importlib.util.find_spec(package) is not None:
                self._entries[package] = _PackageEntry(
                    package=package, target=package, python=sys.executable,
                )
                return True
        except (ImportError, ValueError):
            pass

        # Slow path: aux venvs.
        import subprocess  # local: not used elsewhere in hot path
        for python, bin_dir in self._venv_pythons:
            if python == sys.executable:
                continue   # already tried above
            try:
                rc = subprocess.run(
                    [python, "-c", f"import {package}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                ).returncode
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
            if rc == 0:
                self._entries[package] = _PackageEntry(
                    package=package, target=package, python=python, bin_dir=bin_dir,
                )
                return True
        return False

    # ── worker lifecycle ────────────────────────────────────────────

    async def _get_worker(self, package: str):
        entry = self._entries.get(package)
        if entry is None and self._auto_register(package):
            entry = self._entries[package]
        if entry is None:
            raise KeyError(package)
        if entry.worker is not None:
            return entry.worker
        async with entry.spawn_lock:
            if entry.worker is not None:
                return entry.worker
            if entry.dispatcher is not None:
                entry.worker = _InProcessWorker(entry.dispatcher)
            else:
                assert entry.target is not None and entry.python is not None
                w = _SubprocessWorker(
                    package, entry.target, entry.python,
                    ns_bin_dir=entry.bin_dir,
                )
                await w.start()
                entry.worker = w
            return entry.worker

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(e.worker.shutdown() for e in self._entries.values() if e.worker is not None),
            return_exceptions=True,
        )

    # ── dispatch ────────────────────────────────────────────────────

    async def dispatch_unary(self, request: RemoteRequest) -> RemoteResponse:
        try:
            worker = await self._get_worker(request.package)
        except KeyError:
            return RemoteResponse(ok=False, error=RemoteError(
                type="PackageNotLoaded",
                message=f"namespace not loaded: {request.package!r}",
            ))
        return await worker.call_unary(request)

    async def dispatch_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        try:
            worker = await self._get_worker(request.package)
        except KeyError:
            yield {"type": "error", "error": RemoteError(
                type="PackageNotLoaded",
                message=f"namespace not loaded: {request.package!r}",
            ).model_dump()}
            return
        async for ev in worker.iter_stream(request):
            yield ev

    async def dispatch_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            worker = await self._get_worker(request.package)
        except KeyError:
            yield {"type": "error", "error": RemoteError(
                type="PackageNotLoaded",
                message=f"namespace not loaded: {request.package!r}",
            ).model_dump()}
            return
        async for ev in worker.iter_bidi(request, input_iter):
            yield ev


__all__ = ["NamespaceMultiplexer"]
