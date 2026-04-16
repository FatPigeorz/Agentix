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
    After sandbox creation, closures are loaded via POST /load.
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

        # Build PATH: closure bins + runtime + system
        path_parts = [f"{p}/bin" for p in config.closures.values()]
        path_parts.append(f"{config.runtime_closure}/bin")
        path_parts.extend(["/usr/local/bin", "/usr/bin", "/bin"])
        path_env = ":".join(path_parts)

        cmd = [
            "docker", "run", "-d",
            "--name", sandbox_id,
            "--network", "host",
            "-v", "/nix/store:/nix/store:ro",
            "-e", f"PATH={path_env}",
            config.task_image,
            f"{config.runtime_closure}/bin/agentix-server", "--port", str(port),
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
            sandbox_id=sandbox_id, port=port, config=config,
        )

        logger.info("Created sandbox %s on port %d", sandbox_id, port)

        # Load closures via runtime server /load endpoint
        if config.closures:
            import httpx
            async with httpx.AsyncClient(base_url=f"http://localhost:{port}", timeout=60) as client:
                # Wait for server to be ready
                for _ in range(120):
                    try:
                        r = await client.get("/health")
                        if r.status_code == 200:
                            break
                    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                        pass
                    await asyncio.sleep(0.5)

                for namespace, closure_path in config.closures.items():
                    r = await client.post("/load", json={"path": closure_path, "namespace": namespace})
                    if r.status_code == 200:
                        logger.info("Loaded closure %s in sandbox %s", closure_path, sandbox_id)
                    else:
                        logger.error("Failed to load closure %s: %s", closure_path, r.text)

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

        if force_recreate or config.task_image != sb.config.task_image or config.runtime_closure != sb.config.runtime_closure:
            await self.delete(sandbox_id)
            return await self.create(config)

        # Closures changed — reload via /load
        if config.closures != sb.config.closures:
            import httpx
            async with httpx.AsyncClient(base_url=f"http://localhost:{sb.port}", timeout=60) as client:
                # Unload old closures
                for namespace in sb.config.closures:
                    await client.post("/unload", json={"namespace": namespace})

                # Load new closures
                for namespace, closure_path in config.closures.items():
                    await client.post("/load", json={"path": closure_path, "namespace": namespace})

            sb.config = config

        return await self.get(sandbox_id)

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
