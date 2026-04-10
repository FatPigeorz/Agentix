"""Closure loader: spawn closure processes, reverse proxy via Unix socket.

A closure is any executable that accepts --socket <path> and starts
an HTTP server on that Unix socket. The loader manages the lifecycle
and provides a reverse proxy function for the runtime server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("agentix.runtime.loader")

SOCKET_DIR = Path(os.environ.get("AGENTIX_SOCKET_DIR", "/tmp/agentix"))


def _is_python_script(path: Path) -> bool:
    """Check if a file starts with a Python shebang."""
    try:
        with open(path, "rb") as f:
            first_line = f.readline(100)
            return b"python" in first_line
    except (OSError, UnicodeDecodeError):
        return False


@dataclass
class LoadedClosure:
    """A running closure process."""
    name: str
    path: Path
    socket_path: Path
    process: asyncio.subprocess.Process
    client: httpx.AsyncClient


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
        # Pass the runtime's sys.path as PYTHONPATH so closures can import
        # fastapi, uvicorn, etc. from the runtime closure's dependencies.
        logger.info("Loading closure '%s' from %s (entry=%s)", name, path, entry)
        cmd = [str(entry), "--socket", str(socket_path)]
        if str(entry).endswith(".py") or _is_python_script(entry):
            cmd = [sys.executable, str(entry), "--socket", str(socket_path)]

        env = dict(os.environ)
        # Export runtime's site-packages so closure subprocesses can find them
        site_packages = [p for p in sys.path if "site-packages" in p]
        if site_packages:
            env["PYTHONPATH"] = ":".join(site_packages) + ":" + env.get("PYTHONPATH", "")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

        # Health check
        for _ in range(50):  # 5 seconds max
            try:
                r = await client.get("/")
                if r.status_code < 500:
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
        )
        logger.info("Closure '%s' loaded, socket=%s, pid=%d", name, socket_path, proc.pid)
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
        """List all loaded closures."""
        result = []
        for name, c in self._closures.items():
            result.append({
                "name": name,
                "path": str(c.path),
                "pid": c.process.pid,
                "socket": str(c.socket_path),
            })
        return result

    async def shutdown(self) -> None:
        """Unload all closures."""
        names = list(self._closures.keys())
        for name in names:
            await self.unload(name)
