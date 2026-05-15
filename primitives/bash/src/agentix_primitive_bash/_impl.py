"""Bash primitive impl — subprocess wrapper with env scrub + PATH composition.

`BashImpl` is an **independent class** that provides the `Bash` interface;
it does NOT inherit from `Bash`. `_register.py` composes them via
`Dispatcher.bind_namespace(Bash, BashImpl())`.

Ports the env scrubbing rules + closure-bin PATH prepending that used to
live in `agentix.runtime.server.builtins`. Closures share the runtime's
process, so `_resolve_closure_bins` reads the runtime's Registry directly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from . import BashError, BashEvent, BashExit, BashResult, BashStderr, BashStdout

# Env vars stripped before forking a user-space subprocess.
# The runtime is a Nix-built binary; os.environ is pre-loaded with Nix
# runtime paths (LD_LIBRARY_PATH pointing at Nix-store libs, NIX_*,
# PYTHONPATH, FONTCONFIG_*). Leaking these into a host-image subprocess
# causes glibc ABI mismatches and silent library override bugs.
_RUNTIME_ONLY_ENV = {
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONPATH",
    "PYTHONHOME",
    "LOCALE_ARCHIVE",
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
}


def _clean_env(
    extra: dict[str, str] | None,
    prepend_path: list[str] | None = None,
) -> dict[str, str]:
    """Build a subprocess env: scrubbed base + optional PATH prefixes +
    caller-supplied overrides.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _RUNTIME_ONLY_ENV and not k.startswith("NIX_")
    }
    if prepend_path:
        base_path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["PATH"] = ":".join([*prepend_path, base_path])
    if extra:
        env.update(extra)
    return env


def _resolve_closure_bins(packages: list[str]) -> list[str]:
    """Resolve closure package paths to their `entry/bin/` directories.

    Unknown packages are silently dropped. `["*"]` expands to every
    currently-registered closure.
    """
    # Late import: closures share the runtime's Python process, so the
    # runtime is already loaded by the time any bash.run call lands.
    from agentix.runtime.server.app import registry

    pkg_list = registry.packages() if packages == ["*"] else packages
    out: list[str] = []
    for pkg in pkg_list:
        entry = registry.entry_for(pkg)
        if entry is not None:
            out.append(str(entry / "bin"))
    return out


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> str:
    """Read from a subprocess stream up to `limit` bytes, then truncate."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = limit - total
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            chunks.append(b"\n[truncated at %d bytes]" % limit)
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode(errors="replace")


class BashImpl:
    """Bash primitive implementation. Composed with `Bash` via
    `Dispatcher.bind_namespace`; not a subclass of `Bash`."""

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        max_output: int = 10 * 1024 * 1024,
        paths_from: list[str] | None = None,
    ) -> BashResult:
        prepend = _resolve_closure_bins(paths_from) if paths_from else None
        sub_env = _clean_env(env, prepend_path=prepend)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=sub_env,
        )
        try:
            async def _collect():
                stdout = await _read_capped(proc.stdout, max_output)
                stderr = await _read_capped(proc.stderr, max_output)
                await proc.wait()
                return stdout, stderr

            stdout, stderr = await asyncio.wait_for(_collect(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return BashResult(
                exit_code=-1, stdout="", stderr=f"Command timed out after {timeout}s",
            )
        return BashResult(exit_code=proc.returncode or 0, stdout=stdout, stderr=stderr)

    async def run_stream(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        paths_from: list[str] | None = None,
    ) -> AsyncIterator[BashEvent]:
        prepend = _resolve_closure_bins(paths_from) if paths_from else None
        sub_env = _clean_env(env, prepend_path=prepend)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=sub_env,
        )

        async def _pump(stream, tag, queue):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                await queue.put((tag, chunk))
            await queue.put((tag, None))

        queue: asyncio.Queue = asyncio.Queue()
        tasks = [
            asyncio.create_task(_pump(proc.stdout, "stdout", queue)),
            asyncio.create_task(_pump(proc.stderr, "stderr", queue)),
        ]
        open_streams = {"stdout", "stderr"}

        try:
            deadline = None
            if timeout is not None:
                deadline = asyncio.get_event_loop().time() + timeout
            while open_streams:
                remaining = None
                if deadline is not None:
                    remaining = max(deadline - asyncio.get_event_loop().time(), 0)
                    if remaining == 0:
                        proc.kill()
                        yield BashError(message=f"Command timed out after {timeout}s")
                        return
                try:
                    tag, chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    proc.kill()
                    yield BashError(message=f"Command timed out after {timeout}s")
                    return
                if chunk is None:
                    open_streams.discard(tag)
                    continue
                text = chunk.decode(errors="replace")
                if tag == "stdout":
                    yield BashStdout(data=text)
                else:
                    yield BashStderr(data=text)
            await proc.wait()
            yield BashExit(exit_code=proc.returncode or 0)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
