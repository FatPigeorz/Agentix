"""Tests for agentix.models — pydantic validation and serialization."""

from __future__ import annotations

import pytest

from agentix.models import NamespaceManifest, SandboxConfig
from agentix.runtime.models import RemoteError, RemoteRequest, RemoteResponse


def test_namespace_manifest_minimal():
    m = NamespaceManifest(name="core", version="0.1.0", package="agentix.core")
    assert m.package == "agentix.core"
    assert m.description is None


def test_namespace_manifest_extra_allow():
    m = NamespaceManifest.model_validate({
        "name": "agentix-mock-agent",
        "version": "0.1.0",
        "package": "agentix.mock_agent",
        "extra_field": "ignored-but-preserved",
    })
    assert m.name == "agentix-mock-agent"


def test_namespace_manifest_requires_name_version_package():
    with pytest.raises(Exception):
        NamespaceManifest(name="x", version="0.0.0")  # type: ignore[call-arg]


def test_remote_request_defaults():
    r = RemoteRequest(package="agentix.echo", method="echo")
    assert r.args == []
    assert r.kwargs == {}


def test_remote_response_ok_shape():
    resp = RemoteResponse(ok=True, value={"x": 1})
    assert resp.error is None


def test_remote_response_error_shape():
    err = RemoteError(type="ValueError", message="bad")
    resp = RemoteResponse(ok=False, error=err)
    assert resp.value is None
    assert resp.error.type == "ValueError"


def test_sandbox_config_image_only():
    cfg = SandboxConfig(image="my-agent:0.1.0")
    assert cfg.image == "my-agent:0.1.0"
    assert cfg.env is None


def test_sandbox_config_with_env():
    cfg = SandboxConfig(image="my-agent:0.1.0", env={"FOO": "bar"})
    assert cfg.env == {"FOO": "bar"}


def test_sandbox_config_requires_image():
    with pytest.raises(Exception):
        SandboxConfig()  # type: ignore[call-arg]
