"""Command execution and file I/O inside a sandbox."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("agentix.runtime")

UPLOAD_ROOT = Path(os.environ.get("AGENTIX_UPLOAD_ROOT", "/workspace")).resolve()

MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MiB


class Executor:
    """Runs commands and handles files inside the sandbox.

    Sandbox-agnostic — doesn't know or care whether it's running
    in Docker, Modal, Daytona, or bare metal.
    """

    async def _read_capped(self, stream, limit: int) -> str:
        """Read from an async stream up to *limit* bytes.

        If the stream produces more data than *limit*, the output is
        truncated and a ``[truncated]`` marker is appended.
        """
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
                total += remaining
                chunks.append(b"\n[truncated at %d bytes]" % limit)
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode(errors="replace")

    async def exec(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        max_output: int = MAX_OUTPUT_BYTES,
    ) -> tuple[int, str, str]:
        """Execute a shell command.

        Returns:
            (exit_code, stdout, stderr)
        """
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            async def _collect():
                stdout = await self._read_capped(proc.stdout, max_output)
                stderr = await self._read_capped(proc.stderr, max_output)
                await proc.wait()
                return stdout, stderr

            stdout, stderr = await asyncio.wait_for(_collect(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return -1, "", f"Command timed out after {timeout}s"

        return (
            proc.returncode or 0,
            stdout,
            stderr,
        )

    def upload(self, data: bytes, dest: str) -> int:
        """Write bytes to a path. Creates parent dirs."""
        p = Path(dest).resolve()
        if not p.is_relative_to(UPLOAD_ROOT):
            raise PermissionError(f"Upload path {p} outside allowed root {UPLOAD_ROOT}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return len(data)

    def download(self, path: str) -> bytes:
        """Read bytes from a path."""
        p = Path(path).resolve()
        if not p.is_relative_to(UPLOAD_ROOT):
            raise PermissionError(f"Download path {p} outside allowed root {UPLOAD_ROOT}")
        if not p.exists():
            raise FileNotFoundError(f"Not found: {path}")
        return p.read_bytes()
