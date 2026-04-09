"""Async HTTP client for the agentix runtime server.

Pure sandbox interface: exec, upload, download, health.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from agentix.models import ExecRequest, ExecResponse, HealthResponse, UploadResponse


class RuntimeClient:
    """Async client for the agentix runtime server."""

    def __init__(self, base_url: str, timeout: float = 300):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def health(self) -> HealthResponse:
        r = await self._client.get("/health")
        r.raise_for_status()
        return HealthResponse.model_validate(r.json())

    async def wait_until_alive(self, timeout: float = 60, interval: float = 0.5) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await self.health()
                return
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                await asyncio.sleep(interval)
        raise TimeoutError(f"agentix server not alive after {timeout}s")

    async def exec(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResponse:
        req = ExecRequest(command=command, timeout=timeout, cwd=cwd, env=env)
        r = await self._client.post("/exec", json=req.model_dump(exclude_none=True))
        r.raise_for_status()
        return ExecResponse.model_validate(r.json())

    async def upload(self, local_path: str | Path, dest: str) -> UploadResponse:
        p = Path(local_path)
        with open(p, "rb") as f:
            r = await self._client.post(
                "/upload",
                files={"file": (p.name, f)},
                data={"path": dest},
            )
        r.raise_for_status()
        return UploadResponse.model_validate(r.json())

    async def download(self, path: str, local_path: str | Path) -> int:
        r = await self._client.get("/download", params={"path": path})
        r.raise_for_status()
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return len(r.content)
