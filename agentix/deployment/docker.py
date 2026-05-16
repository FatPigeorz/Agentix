"""Docker deployment: sandbox CRUD via local Docker.

Design:

  Bundle images are produced by `agentix build` and are self-contained:
  they ship the runtime + every namespace's Python package in one venv,
  plus any system deps under `/nix`. The deployment runs the bundle
  image directly — no volume populate, no mount-and-merge, no custom
  entrypoint.

  Sandbox create:
      docker run -d --name <sid> --network host \\
         -e AGENTIX_BIND_PORT=<port> \\
         <bundle-image>

  The container's `ENTRYPOINT` is `agentix-server`, which binds to the
  port from the env var. We pick a free host port, pass it through, and
  health-check `/health` on it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from uuid import uuid4

import httpx

from agentix.deployment.base import Deployment, Sandbox
from agentix.idents import SandboxId
from agentix.models import SandboxConfig, SandboxInfo

logger = logging.getLogger("agentix.deployment.docker")


async def _docker(*args: str, check: bool = True) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode or 0
    if check and rc != 0:
        raise RuntimeError(f"docker {args[0]} failed: {stderr.decode(errors='replace')}")
    return rc, stdout, stderr


class DockerDeployment(Deployment):
    """Sandbox CRUD via local Docker."""

    def __init__(self):
        self._ports: dict[SandboxId, int] = {}  # sandbox_id → host port

    @staticmethod
    def _allocate_port() -> int:
        # Ask the kernel for any free TCP port. There's still a small
        # TOCTOU window before the container binds, but no worse than a
        # linear probe and without the seed parameter.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def create(self, config: SandboxConfig) -> Sandbox:
        sandbox_id = SandboxId(f"agentix-{uuid4().hex[:8]}")
        port = self._allocate_port()

        env_args: list[str] = ["-e", f"AGENTIX_BIND_PORT={port}"]
        if config.env:
            for k, v in config.env.items():
                env_args.extend(["-e", f"{k}={v}"])

        await _docker(
            "run", "-d",
            "--name", sandbox_id,
            "--network", "host",
            *env_args,
            config.image,
        )

        self._ports[sandbox_id] = port
        logger.info("Created sandbox %s on port %d", sandbox_id, port)

        await self._wait_healthy(port)
        return Sandbox(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{port}",
            status="running",
        )

    async def _wait_healthy(self, port: int) -> None:
        base_url = f"http://localhost:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            for _ in range(120):
                try:
                    r = await client.get("/health")
                    if r.status_code == 200:
                        return
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError(f"Runtime server not alive at {base_url}")

    async def get(self, sandbox_id: SandboxId) -> SandboxInfo:
        port = self._ports.get(sandbox_id)
        if port is None:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        rc, stdout, _ = await _docker(
            "inspect", "-f", "{{.State.Status}}", sandbox_id, check=False,
        )
        status = stdout.decode().strip() if rc == 0 else "unknown"
        return SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{port}",
            status=status,
        )

    async def delete(self, sandbox_id: SandboxId) -> None:
        await _docker("rm", "-f", sandbox_id, check=False)
        self._ports.pop(sandbox_id, None)
        logger.info("Deleted sandbox %s", sandbox_id)
