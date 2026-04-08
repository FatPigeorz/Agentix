"""Core runtime logic: command execution and file operations."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("hnix")


class Runtime:
    """Command execution and file I/O inside a sandbox.

    Sandbox-agnostic — doesn't know or care whether it's running
    in Docker, Modal, Daytona, or bare metal. PATH and env are
    set at process startup by the deployment layer.
    """

    async def exec(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
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
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return -1, "", f"Command timed out after {timeout}s"

        return (
            proc.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )

    def upload(self, data: bytes, dest: str) -> int:
        """Write bytes to a path. Creates parent dirs.

        Returns:
            Number of bytes written.
        """
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return len(data)

    def download(self, path: str) -> bytes:
        """Read bytes from a path.

        Raises:
            FileNotFoundError: If path doesn't exist.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Not found: {path}")
        return p.read_bytes()
