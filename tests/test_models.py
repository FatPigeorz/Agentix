"""Tests for agentix.models — pydantic model validation and serialization."""

from __future__ import annotations

from agentix.models import (
    ExecRequest,
    ExecResponse,
    HealthResponse,
    SandboxConfig,
    SandboxInfo,
    UploadResponse,
)

# ── Upstream tests ────────────────────────────────────────────────


def test_exec_request_defaults():
    """ExecRequest has sensible defaults."""
    req = ExecRequest(command="echo hi")
    assert req.command == "echo hi"
    assert req.timeout is None
    assert req.cwd is None
    assert req.env is None
    assert req.max_output == 10_485_760


def test_sandbox_config():
    """SandboxConfig requires task_image, runtime_closure, agent_closure."""
    cfg = SandboxConfig(
        task_image="ubuntu:22.04",
        runtime_closure="/nix/store/abc-runtime",
        agent_closure="/nix/store/def-agent",
    )
    assert cfg.task_image == "ubuntu:22.04"
    assert cfg.agent_closure == "/nix/store/def-agent"


def test_sandbox_config_dataset_closure():
    """SandboxConfig supports optional dataset_closure."""
    cfg = SandboxConfig(
        task_image="ubuntu:22.04",
        runtime_closure="/nix/store/abc",
        agent_closure="/nix/store/def",
        dataset_closure="/nix/store/ghi",
    )
    assert cfg.dataset_closure == "/nix/store/ghi"


def test_round_trip():
    """Serialize to dict and back."""
    req = ExecRequest(command="ls", timeout=30.0, cwd="/tmp")
    data = req.model_dump()
    assert data["command"] == "ls"
    assert data["timeout"] == 30.0
    reconstructed = ExecRequest.model_validate(data)
    assert reconstructed == req


def test_exec_response_round_trip():
    """ExecResponse serialize/deserialize."""
    resp = ExecResponse(exit_code=0, stdout="ok", stderr="")
    json_str = resp.model_dump_json()
    back = ExecResponse.model_validate_json(json_str)
    assert back.exit_code == 0
    assert back.stdout == "ok"


# ── Additional model tests ────────────────────────────────────────


def test_health_response():
    resp = HealthResponse(version="0.1.0")
    assert resp.status == "ok"


def test_upload_response():
    resp = UploadResponse(path="/app/f.py", size=42)
    assert resp.size == 42


def test_sandbox_info_default_status():
    info = SandboxInfo(sandbox_id="sb-1", runtime_url="http://localhost:18000")
    assert info.status == "running"
