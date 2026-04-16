"""Closure loader: spawn closure processes, reverse proxy via Unix socket.

A closure is any executable that accepts --socket <path> and starts
an HTTP server on that Unix socket. The loader manages the lifecycle
and provides a reverse proxy function for the runtime server.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("agentix.runtime.loader")

SOCKET_DIR = Path(os.environ.get("AGENTIX_SOCKET_DIR", "/tmp/agentix"))


@dataclass
class LoadedClosure:
    """A running closure process."""
    name: str
    path: Path
    socket_path: Path
    process: asyncio.subprocess.Process
    client: httpx.AsyncClient
    manifest: dict = field(default_factory=dict)  # endpoint descriptions from GET /


class ClosureLoader:
    """Manages closure lifecycles: load, proxy, unload."""

    def __init__(self):
        self._closures: dict[str, LoadedClosure] = {}
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)

    def _find_entry(self, closure_path: Path) -> Path:
        """Find the executable entry point in a closure directory."""
        # Convention: look for 'serve' or 'main' or the first executable
        for name in ("serve", "main"):
            candidate = closure_path / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
            candidate = closure_path / "bin" / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate

        # Fallback: first executable in bin/
        bin_dir = closure_path / "bin"
        if bin_dir.is_dir():
            for f in sorted(bin_dir.iterdir()):
                if f.is_file() and os.access(f, os.X_OK):
                    return f

        # Fallback: first executable in root
        for f in sorted(closure_path.iterdir()):
            if f.is_file() and os.access(f, os.X_OK):
                return f

        raise FileNotFoundError(f"No executable found in {closure_path}")

    async def load(self, path: str, namespace: str | None = None) -> str:
        """Load a closure: spawn process, wait for socket.

        Args:
            path: Path to the closure directory (e.g. /nix/store/xxx)
            namespace: Optional namespace for endpoints (default: closure dir name)

        Returns:
            The namespace under which endpoints are registered
        """
        closure_path = Path(path)
        if not closure_path.is_dir():
            raise FileNotFoundError(f"Closure not found: {path}")

        name = namespace or closure_path.name
        if name in self._closures:
            logger.warning("Closure '%s' already loaded, unloading first", name)
            await self.unload(name)

        entry = self._find_entry(closure_path)
        socket_path = SOCKET_DIR / f"{name}.sock"

        # Remove stale socket
        if socket_path.exists():
            socket_path.unlink()

        # Spawn the closure process
        # Closures are self-contained: they bundle their own Python + deps.
        logger.info("Loading closure '%s' from %s (entry=%s)", name, path, entry)
        proc = await asyncio.create_subprocess_exec(
            str(entry), "--socket", str(socket_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for socket to appear
        for _ in range(100):  # 10 seconds max
            if socket_path.exists():
                break
            await asyncio.sleep(0.1)
        else:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(f"Closure '{name}' did not create socket within 10s")

        # Create HTTP client over Unix socket
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        client = httpx.AsyncClient(transport=transport, base_url="http://closure", timeout=300)

        # Health check + reflection: GET / returns closure manifest
        manifest = {}
        for _ in range(50):  # 5 seconds max
            try:
                r = await client.get("/")
                if r.status_code < 500:
                    try:
                        manifest = r.json()
                    except Exception:
                        manifest = {"status": "ok"}
                    break
            except (httpx.ConnectError, httpx.ReadError):
                await asyncio.sleep(0.1)
        else:
            proc.kill()
            await proc.communicate()
            await client.aclose()
            raise TimeoutError(f"Closure '{name}' not responding on socket")

        self._closures[name] = LoadedClosure(
            name=name, path=closure_path,
            socket_path=socket_path, process=proc, client=client,
            manifest=manifest,
        )
        logger.info("Closure '%s' loaded (manifest=%s)", name, manifest)
        return name

    async def unload(self, name: str) -> None:
        """Stop a closure process and clean up."""
        closure = self._closures.pop(name, None)
        if not closure:
            return

        logger.info("Unloading closure '%s'", name)
        await closure.client.aclose()
        closure.process.terminate()
        try:
            await asyncio.wait_for(closure.process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            closure.process.kill()
            await closure.process.communicate()

        if closure.socket_path.exists():
            closure.socket_path.unlink()

    async def proxy(self, name: str, path: str, method: str,
                    body: bytes | None = None, headers: dict | None = None) -> httpx.Response:
        """Forward a request to a loaded closure.

        Args:
            name: Closure namespace
            path: Request path (e.g. /setup)
            method: HTTP method
            body: Request body
            headers: Request headers

        Returns:
            Response from the closure
        """
        closure = self._closures.get(name)
        if not closure:
            raise KeyError(f"Closure not loaded: {name}")

        r = await closure.client.request(
            method=method,
            url=path,
            content=body,
            headers={k: v for k, v in (headers or {}).items()
                     if k.lower() not in ("host", "transfer-encoding")},
        )
        return r

    async def list_closures(self) -> list[dict]:
        """List all loaded closures with their manifests."""
        result = []
        for name, c in self._closures.items():
            result.append({
                "name": name,
                "path": str(c.path),
                "pid": c.process.pid,
                "socket": str(c.socket_path),
                "manifest": c.manifest,
            })
        return result

    async def shutdown(self) -> None:
        """Unload all closures."""
        names = list(self._closures.keys())
        for name in names:
            await self.unload(name)
