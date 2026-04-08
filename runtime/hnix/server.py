"""hnix runtime server.

Runs inside the sandbox. Three endpoints: upload, download, exec.
Closures are already in place (mounted or extracted by the deployment layer).
PATH is set at process startup.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from hnix import __version__
from hnix.models import (
    ExecRequest,
    ExecResponse,
    HealthResponse,
    UploadResponse,
)
from hnix.runtime import Runtime

logger = logging.getLogger("hnix")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI(title="hnix", version=__version__)
runtime = Runtime()


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(version=__version__)


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest):
    exit_code, stdout, stderr = await runtime.exec(
        command=req.command,
        timeout=req.timeout,
        cwd=req.cwd,
        extra_env=req.env,
    )
    return ExecResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)


@app.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    path: str = Form(...),
):
    data = await file.read()
    size = runtime.upload(data, path)
    return UploadResponse(path=path, size=size)


@app.get("/download")
async def download(path: str):
    try:
        data = runtime.download(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    return Response(content=data, media_type="application/octet-stream")
