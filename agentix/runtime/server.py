"""Agentix runtime server.

In-process closure dispatch. The runtime is a single Python process serving:

- built-in operations (exec/upload/download) mounted at root
- `POST /_remote` — direct dispatch to a bound impl, body specifies the
  closure's Python package path + method name
- `GET /closures` — inventory (always cheap; does not force-load anything)
- `GET /health`

There are no caller-chosen namespaces: each closure's Python import path
(`manifest.package`) is its routing key. Two images shipping the same
package collide; the second is skipped with a warning.

Discovery on startup is cheap: scan `/mnt/*` for `entry/manifest.json`,
validate against ClosureManifest, prepend each closure's `entry/python`
to sys.path, and register the closure in the Registry as a pending entry.
The actual `importlib.import_module(<pkg>)` + `_register.register()` is
deferred until the first `POST /_remote` for that package — slow boots
no longer block the runtime, and a broken closure does not crash startup.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from agentix import __version__
from agentix.dispatch import Registry
from agentix.models import (
    AGENTIX_CLOSURE_ABI,
    ClosureInfo,
    ClosureManifest,
    HealthResponse,
    RemoteRequest,
    RemoteResponse,
)
from agentix.runtime.builtins import router as builtins_router

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

CLOSURE_MOUNT_ROOT = Path(os.environ.get("AGENTIX_CLOSURE_MOUNT_ROOT", "/mnt"))

registry = Registry()


async def _auto_load() -> None:
    """Scan /mnt for closures and register each one as a pending entry.

    Does NOT import any closure packages or build any Dispatchers — that
    work happens lazily on first `/_remote` call (see `Registry.get_or_load`).
    Cheap manifest validation is sufficient at boot to fail loudly on a
    malformed mount while keeping startup latency flat.

    `/mnt/runtime` is reserved (the runtime itself) and skipped.
    """
    if not CLOSURE_MOUNT_ROOT.is_dir():
        return
    for mount in sorted(CLOSURE_MOUNT_ROOT.iterdir()):
        if mount.name == "runtime" or not mount.is_dir():
            continue
        manifest = _read_manifest(mount)
        if manifest is None:
            continue
        if manifest.package in registry:
            logger.error(
                "skip mount %s: package %r already registered from %s",
                mount, manifest.package, registry.mount_for(manifest.package),
            )
            continue
        registry.register(manifest.package, manifest, mount)
        logger.info("registered closure '%s' from %s (deferred)",
                    manifest.package, mount)


def _read_manifest(mount: Path) -> ClosureManifest | None:
    """Read and validate <mount>/entry/manifest.json."""
    mf_path = mount / "entry" / "manifest.json"
    if not mf_path.is_file():
        logger.warning("skip mount %s: missing entry/manifest.json", mount)
        return None
    try:
        manifest = ClosureManifest.model_validate_json(mf_path.read_text())
    except ValidationError as exc:
        logger.error("skip mount %s: invalid manifest.json: %s", mount, exc)
        return None
    if manifest.abi != AGENTIX_CLOSURE_ABI:
        logger.warning(
            "skip mount %s: abi=%d, runtime supports %d",
            mount, manifest.abi, AGENTIX_CLOSURE_ABI,
        )
        return None
    return manifest


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _auto_load()
    yield


app = FastAPI(title="agentix", version=__version__, lifespan=lifespan)
app.state.registry = registry
app.include_router(builtins_router)


# ── Health & inventory ──────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/closures")
async def list_closures() -> list[ClosureInfo]:
    """All registered closures (loaded or not). Doesn't force-load."""
    out: list[ClosureInfo] = []
    for pkg in registry.packages():
        manifest = registry.manifest_for(pkg)
        mount = registry.mount_for(pkg)
        if manifest is None or mount is None:
            continue
        out.append(ClosureInfo(path=str(mount), manifest=manifest))
    return out


# ── Remote dispatch ─────────────────────────────────────────────


@app.post("/_remote")
async def remote_call(request: RemoteRequest):
    """Single dispatch endpoint. Triggers lazy import on first use.

    - For unary impls (signature returns `R`): responds 200 application/json
      with a `RemoteResponse` body.
    - For streaming impls (signature returns `AsyncIterator[T]`): responds
      200 application/x-ndjson, one JSON event per line.
    - If the closure failed to import on a prior call, every subsequent
      request to the same package re-raises the cached error as 500.
    """
    try:
        dispatcher = await registry.get_or_load(request.package)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"closure '{request.package}' failed to load: {type(exc).__name__}: {exc}",
        ) from exc
    if dispatcher is None:
        raise HTTPException(
            status_code=404,
            detail=f"closure not loaded: package={request.package!r}",
        )
    if dispatcher.is_streaming(request.method):
        return StreamingResponse(
            dispatcher.dispatch_stream(request),
            media_type="application/x-ndjson",
        )
    return await dispatcher.dispatch(request)


# ── Entry point (invoked as /mnt/runtime/entry/bin/start) ───────


def main() -> None:
    """Entry point the closure convention expects at
    /mnt/runtime/entry/bin/start. Port via AGENTIX_BIND_PORT (env, default
    8000); dev shell can override via --port.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="agentix runtime server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENTIX_BIND_PORT", "8000")),
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-port", type=int, default=5678)
    parser.add_argument("--debug-wait", action="store_true")
    args = parser.parse_args()

    if args.debug:
        import debugpy

        debugpy.listen(("0.0.0.0", args.debug_port))
        print(f"debugpy listening on 0.0.0.0:{args.debug_port}")
        if args.debug_wait:
            print("Waiting for debugger to attach...")
            debugpy.wait_for_client()

    uvicorn.run("agentix.runtime.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
