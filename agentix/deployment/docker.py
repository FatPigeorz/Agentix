"""Docker deployment: sandbox CRUD via local Docker."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from agentix.deployment.base import Deployment
from agentix.models import SandboxConfig, SandboxInfo

logger = logging.getLogger("agentix.deployment.docker")


class DockerDeployment(Deployment):
    """Manages sandboxes as local Docker containers.

    Injects closures via volume mount (-v /nix/store:/nix/store:ro).
    """

    def __init__(self, host_port_start: int = 18000):
        self._next_port = host_port_start
        self._sandboxes: dict[str, _DockerSandbox] = {}

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        sandbox_id = f"agentix-{uuid4().hex[:8]}"
        port = self._next_port
        self._next_port += 1

        cmd = [
            "docker", "run", "-d",
            "--name", sandbox_id,
            "-v", "/nix/store:/nix/store:ro",
            "-e", f"PATH={config.agent_closure}/bin:{config.runtime_closure}/bin:/usr/local/bin:/usr/bin:/bin",
            "-p", f"{port}:8000",
            config.task_image,
            f"{config.runtime_closure}/bin/agentix-server", "--port", "8000",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create sandbox: {stderr.decode(errors='replace')}"
            )

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{port}",
            status="running",
        )
        self._sandboxes[sandbox_id] = _DockerSandbox(
            sandbox_id=sandbox_id, port=port, config=config
        )

        logger.info("Created sandbox %s on port %d", sandbox_id, port)
        return info

    async def get(self, sandbox_id: str) -> SandboxInfo:
        sb = self._sandboxes.get(sandbox_id)
        if not sb:
            raise KeyError(f"Sandbox not found: {sandbox_id}")

        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Status}}", sandbox_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        status = stdout.decode().strip() if proc.returncode == 0 else "unknown"

        return SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{sb.port}",
            status=status,
        )

    async def update(self, sandbox_id: str, config: SandboxConfig) -> SandboxInfo:
        await self.delete(sandbox_id)
        return await self.create(config)

    async def delete(self, sandbox_id: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", sandbox_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        self._sandboxes.pop(sandbox_id, None)
        logger.info("Deleted sandbox %s", sandbox_id)


class _DockerSandbox:
    def __init__(self, sandbox_id: str, port: int, config: SandboxConfig):
        self.sandbox_id = sandbox_id
        self.port = port
        self.config = config
