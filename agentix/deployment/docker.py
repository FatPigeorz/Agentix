"""Docker deployment: sandbox CRUD via local Docker."""

from __future__ import annotations

import asyncio
import logging
import socket
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
        self._port_lock = asyncio.Lock()
        self._sandboxes: dict[str, _DockerSandbox] = {}

    async def _allocate_port(self) -> int:
        """Allocate an available port, safe under concurrent access."""
        async with self._port_lock:
            while True:
                port = self._next_port
                self._next_port += 1
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if s.connect_ex(("127.0.0.1", port)) != 0:
                        return port

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        sandbox_id = f"agentix-{uuid4().hex[:8]}"
        port = await self._allocate_port()

        cmd = [
            "docker", "run", "-d",
            "--name", sandbox_id,
            "-v", "/nix/store:/nix/store:ro",
            "-e", (
                f"PATH={config.agent_closure}/bin:"
                f"{config.runtime_closure}/bin:"
                "/usr/local/bin:/usr/bin:/bin"
            ),
        ]
        if config.dataset_closure:
            cmd.extend([
                "-e",
                f"PYTHONPATH={config.dataset_closure}/lib/python3.12/site-packages",
            ])
        cmd.extend([
            "-p", f"{port}:8000",
            config.task_image,
            f"{config.runtime_closure}/bin/agentix-server", "--port", "8000",
        ])

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
            sandbox_id=sandbox_id, port=port, config=config,
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

    async def update(self, sandbox_id: str, config: SandboxConfig,
                     *, force_recreate: bool = False) -> SandboxInfo:
        sb = self._sandboxes.get(sandbox_id)
        if not sb:
            raise KeyError(f"Sandbox not found: {sandbox_id}")

        # Diff: what changed?
        image_changed = config.task_image != sb.config.task_image
        agent_changed = config.agent_closure != sb.config.agent_closure
        runtime_changed = config.runtime_closure != sb.config.runtime_closure
        dataset_changed = config.dataset_closure != sb.config.dataset_closure

        if force_recreate or image_changed or runtime_changed or dataset_changed:
            # Full recreate — can't update base image or runtime in-place
            logger.info("Recreating sandbox %s (force=%s image=%s runtime=%s)",
                        sandbox_id, force_recreate, image_changed, runtime_changed)
            await self.delete(sandbox_id)
            return await self.create(config)

        if agent_changed:
            # In-place: update PATH to point to new agent closure, restart server
            logger.info("In-place agent update for sandbox %s", sandbox_id)
            new_path = (
                f"{config.agent_closure}/bin:"
                f"{config.runtime_closure}/bin:"
                "/usr/local/bin:/usr/bin:/bin"
            )
            await self._exec_in_container(sandbox_id, f"export PATH={new_path}")
            # Restart agentix-server to pick up new PATH
            await self._exec_in_container(sandbox_id, "pkill -f agentix-server || true")
            await self._exec_in_container(
                sandbox_id,
                f"PATH={new_path} {config.runtime_closure}/bin/agentix-server --port 8000 &",
            )
            sb.config = config
            return await self.get(sandbox_id)

        # Nothing changed
        logger.info("No changes for sandbox %s, skipping update", sandbox_id)
        return await self.get(sandbox_id)

    async def _exec_in_container(self, sandbox_id: str, command: str) -> None:
        """Execute a shell command inside a running container."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", sandbox_id, "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("exec in %s failed (rc=%d): %s",
                           sandbox_id, proc.returncode, stderr.decode(errors="replace"))

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
