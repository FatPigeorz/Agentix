"""FastAPI server running inside the sandbox."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from agentix import __version__
from agentix.models import (
    ExecRequest, ExecResponse,
    HealthResponse,
    RunRequest, RunResponse,
    UploadResponse,
)
from agentix.runtime.executor import Executor

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI(title="agentix", version=__version__)
executor = Executor()

# Agent runner — loaded once at startup from /opt/agentix/agent/runner.py
_agent_runner = None


@app.on_event("startup")
async def _load_agent_runner():
    global _agent_runner
    runner_path = Path("/opt/agentix/agent/runner.py")
    if runner_path.exists():
        spec = importlib.util.spec_from_file_location("agent_runner", runner_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _agent_runner = module
        logger.info("Loaded agent runner from %s", runner_path)
    else:
        logger.warning("No agent runner found at %s", runner_path)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(version=__version__)


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """Run the agent's run() function with agent_input."""
    if _agent_runner is None:
        raise HTTPException(status_code=503, detail="No agent runner loaded")
    if not hasattr(_agent_runner, "run"):
        raise HTTPException(status_code=503, detail="Agent runner has no run() function")

    try:
        result = await _agent_runner.run(req.agent_input)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return RunResponse(result=result)


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
