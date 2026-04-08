"""Request/response models for the hnix runtime server."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Requests ──────────────────────────────────────────────────────


class ExecRequest(BaseModel):
    command: str
    timeout: float | None = Field(default=None, description="Timeout in seconds")
    cwd: str | None = Field(default=None, description="Working directory")
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables")


# ── Responses ─────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class UploadResponse(BaseModel):
    path: str
    size: int
