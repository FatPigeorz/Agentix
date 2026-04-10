"""Agentix runtime server.

Pure sandbox interface + closure loading via Unix socket reverse proxy.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response

from agentix import __version__
from agentix.models import ExecRequest, ExecResponse, HealthResponse, UploadResponse
from agentix.runtime.executor import Executor
from agentix.runtime.loader import ClosureLoader

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

loader = ClosureLoader()
executor = Executor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await loader.shutdown()


app = FastAPI(title="agentix", version=__version__, lifespan=lifespan)


# ── Core endpoints ──────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(version=__version__)


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest):
    exit_code, stdout, stderr = await executor.exec(
        command=req.command,
        timeout=req.timeout,
        cwd=req.cwd,
        extra_env=req.env,
        max_output=req.max_output,
    )
    return ExecResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)


@app.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    path: str = Form(...),
):
    data = await file.read()
    size = executor.upload(data, path)
    return UploadResponse(path=path, size=size)


@app.get("/download")
async def download(path: str):
    try:
        data = executor.download(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    return Response(content=data, media_type="application/octet-stream")


# ── Closure management ──────────────────────────────────────────


@app.post("/load")
async def load_closure(request: Request):
    """Load a closure: spawn its process, register reverse proxy.

    Body: {"path": "/nix/store/xxx", "namespace": "swebench"}
    """
    body = await request.json()
    path = body.get("path")
    namespace = body.get("namespace")

    if not path:
        raise HTTPException(status_code=400, detail="'path' is required")

    try:
        name = await loader.load(path, namespace)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))

    return {"status": "loaded", "namespace": name}


@app.post("/unload")
async def unload_closure(request: Request):
    """Unload a closure: stop its process, remove proxy.

    Body: {"namespace": "swebench"}
    """
    body = await request.json()
    name = body.get("namespace")
    if not name:
        raise HTTPException(status_code=400, detail="'namespace' is required")
    await loader.unload(name)
    return {"status": "unloaded", "namespace": name}


@app.get("/closures")
async def list_closures():
    """List all loaded closures."""
    return await loader.list_closures()


# ── Closure reverse proxy (catch-all) ──────────────────────────


@app.api_route("/{namespace}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_closure(namespace: str, path: str, request: Request):
    """Reverse proxy: /{namespace}/{path} → closure's Unix socket."""
    body = await request.body()

    try:
        resp = await loader.proxy(
            name=namespace,
            path=f"/{path}",
            method=request.method,
            body=body if body else None,
            headers=dict(request.headers),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Closure '{namespace}' not loaded")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Closure error: {e}")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )
