"""Tests for agentix.runtime.server — FastAPI endpoint integration tests."""

import io
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentix.runtime.server import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestExec:
    def test_exec_echo(self, client):
        resp = client.post("/exec", json={"command": "echo test123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "test123" in data["stdout"]

    def test_exec_with_timeout(self, client):
        resp = client.post("/exec", json={"command": "sleep 10", "timeout": 0.5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == -1


class TestFileTransfer:
    def test_upload_and_download(self, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            import agentix.runtime.executor as executor_mod
            orig_root = executor_mod.UPLOAD_ROOT
            executor_mod.UPLOAD_ROOT = Path(tmpdir).resolve()
            try:
                dest = str(Path(tmpdir) / "uploaded.txt")
                content = b"file content here"

                # Upload
                resp = client.post(
                    "/upload",
                    files={"file": ("test.txt", io.BytesIO(content))},
                    data={"path": dest},
                )
                assert resp.status_code == 200
                assert resp.json()["size"] == len(content)

                # Download
                resp = client.get("/download", params={"path": dest})
                assert resp.status_code == 200
                assert resp.content == content
            finally:
                executor_mod.UPLOAD_ROOT = orig_root

    def test_download_missing(self, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            import agentix.runtime.executor as executor_mod
            orig_root = executor_mod.UPLOAD_ROOT
            executor_mod.UPLOAD_ROOT = Path(tmpdir).resolve()
            try:
                resp = client.get(
                    "/download",
                    params={"path": str(Path(tmpdir) / "nonexistent")},
                )
                assert resp.status_code == 404
            finally:
                executor_mod.UPLOAD_ROOT = orig_root
