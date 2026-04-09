"""Shared models for Agentix."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Runtime server API ────────────────────────────────────────────


class ExecRequest(BaseModel):
    command: str
    timeout: float | None = Field(default=None)
    cwd: str | None = Field(default=None)
    env: dict[str, str] | None = Field(default=None)
    max_output: int = Field(default=10_485_760, description="Max output bytes (default 10 MiB)")


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


# ── Deployment ────────────────────────────────────────────────────


class SandboxConfig(BaseModel):
    task_image: str = Field(description="Docker image for the task environment")
    runtime_closure: str = Field(description="Nix store path for agentix runtime")
    agent_closure: str = Field(description="Nix store path for agent binary")
    dataset_closure: str | None = Field(
        default=None, description="Nix store path for dataset eval code",
    )


class SandboxInfo(BaseModel):
    sandbox_id: str
    runtime_url: str
    status: str = "running"
