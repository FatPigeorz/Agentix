"""Async HTTP client for the agentix runtime server.

Sandbox interface: exec, upload, download, health, load, call.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from agentix.models import ExecRequest, ExecResponse, HealthResponse, UploadResponse

logger = logging.getLogger("agentix.runtime.client")


class RuntimeClient:
    """Async client for the agentix runtime server."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 300,
        retries: int = 3,
        retry_backoff: float = 1.0,
    ):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._retries = retries
        self._retry_backoff = retry_backoff

    async def _with_retry(self, fn, *args, **kwargs):
        """Retry on transient errors with exponential backoff."""
        last_exc = None
        for attempt in range(self._retries):
            try:
                return await fn(*args, **kwargs)
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self._retries - 1:
                    wait = self._retry_backoff * (2 ** attempt)
                    logger.warning(
                        "Retry %d/%d after %.1fs: %s",
                        attempt + 1, self._retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
        raise last_exc

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Core endpoints ──────────────────────────────────────────

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

        async def _do():
            r = await self._client.post("/exec", json=req.model_dump(exclude_none=True))
            r.raise_for_status()
            return ExecResponse.model_validate(r.json())

        return await self._with_retry(_do)

    async def upload(self, local_path: str | Path, dest: str) -> UploadResponse:
        p = Path(local_path)

        async def _do():
            with open(p, "rb") as f:
                r = await self._client.post(
                    "/upload",
                    files={"file": (p.name, f)},
                    data={"path": dest},
                )
            r.raise_for_status()
            return UploadResponse.model_validate(r.json())

        return await self._with_retry(_do)

    async def download(self, path: str, local_path: str | Path) -> int:
        async def _do():
            r = await self._client.get("/download", params={"path": path})
            r.raise_for_status()
            lp = Path(local_path)
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_bytes(r.content)
            return len(r.content)

        return await self._with_retry(_do)

    # ── Closure management ──────────────────────────────────────

    async def load(self, path: str, namespace: str | None = None) -> str:
        """Load a closure into the runtime server.

        Returns the namespace under which endpoints are available.
        """
        body = {"path": path}
        if namespace:
            body["namespace"] = namespace

        async def _do():
            r = await self._client.post("/load", json=body)
            r.raise_for_status()
            return r.json()["namespace"]

        return await self._with_retry(_do)

    async def unload(self, namespace: str) -> None:
        """Unload a closure."""
        await self._client.post("/unload", json={"namespace": namespace})

    async def closures(self) -> list[dict]:
        """List loaded closures."""
        r = await self._client.get("/closures")
        r.raise_for_status()
        return r.json()

    # ── Closure proxy ───────────────────────────────────────────

    async def call(self, namespace: str, endpoint: str,
                   data: dict | None = None, method: str = "POST") -> dict:
        """Call an endpoint on a loaded closure.

        Example:
            result = await client.call("swebench", "setup", data={"instance_id": "..."})
        """
        url = f"/{namespace}/{endpoint}"

        async def _do():
            if method.upper() == "GET":
                r = await self._client.get(url, params=data)
            else:
                r = await self._client.post(url, json=data)
            r.raise_for_status()
            return r.json()

        return await self._with_retry(_do)
