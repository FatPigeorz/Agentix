"""Agentix runtime server. Pure sandbox interface."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from agentix import __version__
from agentix.models import ExecRequest, ExecResponse, HealthResponse, UploadResponse
from agentix.runtime.executor import Executor

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI(title="agentix", version=__version__)
executor = Executor()


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
