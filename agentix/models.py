"""Shared models for agentix runtime, client, and deployment."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Runtime API ───────────────────────────────────────────────────


class ExecRequest(BaseModel):
    command: str
    timeout: float | None = Field(default=None, description="Timeout in seconds")
    cwd: str | None = Field(default=None, description="Working directory")
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables")


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class UploadResponse(BaseModel):
    path: str
    size: int


class RunRequest(BaseModel):
    agent_input: dict


class RunResponse(BaseModel):
    result: dict


# ── Deployment ────────────────────────────────────────────────────


class SandboxConfig(BaseModel):
    task_image: str = Field(description="Docker image for the task environment")
    runtime_closure: str = Field(description="Nix store path for agentix runtime")
    agent_closure: str = Field(description="Nix store path for agent")


class SandboxInfo(BaseModel):
    sandbox_id: str
    runtime_url: str = Field(description="agentix-server URL, e.g. http://localhost:18000")
    status: str = "running"
