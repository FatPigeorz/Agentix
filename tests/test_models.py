"""Tests for agentix.models — pydantic model validation and serialization."""

from __future__ import annotations

from agentix.models import ExecRequest, ExecResponse, SandboxConfig


def test_exec_request_defaults():
    """ExecRequest has sensible defaults."""
    req = ExecRequest(command="echo hi")
    assert req.command == "echo hi"
    assert req.timeout is None
    assert req.cwd is None
    assert req.env is None
    assert req.max_output == 10_485_760


def test_sandbox_config():
    """SandboxConfig requires task_image and runtime_closure."""
    cfg = SandboxConfig(
        task_image="ubuntu:22.04",
        runtime_closure="/nix/store/abc-runtime",
        closures={"claude": "/nix/store/def-agent", "swebench": "/nix/store/ghi-dataset"},
    )
    assert cfg.task_image == "ubuntu:22.04"
    assert len(cfg.closures) == 2
    assert cfg.closures["claude"] == "/nix/store/def-agent"


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
