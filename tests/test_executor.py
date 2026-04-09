"""Tests for agentix.runtime.executor — exec, path guards, output capping."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentix.runtime.executor import Executor


@pytest.fixture
def executor():
    return Executor()


# ── Exec ──────────────────────────────────────────────────────────


async def test_exec_simple(executor):
    """Basic command execution."""
    code, stdout, stderr = await executor.exec("echo hello")
    assert code == 0
    assert "hello" in stdout


async def test_exec_failing(executor):
    code, stdout, stderr = await executor.exec("false")
    assert code != 0


async def test_exec_timeout(executor):
    """Command timeout returns -1 and error message."""
    code, stdout, stderr = await executor.exec("sleep 60", timeout=0.2)
    assert code == -1
    assert "timed out" in stderr.lower()


async def test_exec_cwd(executor):
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = await executor.exec("pwd", cwd=tmpdir)
        assert code == 0
        assert Path(stdout.strip()).resolve() == Path(tmpdir).resolve()


async def test_exec_extra_env(executor):
    code, stdout, stderr = await executor.exec(
        "echo $MY_TEST_VAR",
        extra_env={"MY_TEST_VAR": "hello_from_test"},
    )
    assert code == 0
    assert "hello_from_test" in stdout


async def test_read_capped(executor):
    """Output truncation works when output exceeds limit."""
    code, stdout, stderr = await executor.exec(
        "python3 -c \"print('A' * 500)\"",
        max_output=100,
    )
    assert code == 0
    assert len(stdout) <= 150  # 100 bytes + truncation marker
    assert "[truncated" in stdout


# ── File I/O with path guards ────────────────────────────────────


def test_upload_within_root(tmp_path, monkeypatch):
    """Upload within allowed root succeeds."""
    import agentix.runtime.executor as executor_mod
    monkeypatch.setattr(executor_mod, "UPLOAD_ROOT", tmp_path.resolve())
    ex = Executor()
    dest = str(tmp_path / "subdir" / "file.txt")
    size = ex.upload(b"hello world", dest)
    assert size == 11
    assert Path(dest).read_bytes() == b"hello world"


def test_upload_outside_root(tmp_path, monkeypatch):
    """Upload outside root raises PermissionError."""
    import agentix.runtime.executor as executor_mod
    monkeypatch.setattr(executor_mod, "UPLOAD_ROOT", tmp_path.resolve())
    ex = Executor()
    with pytest.raises(PermissionError, match="outside allowed root"):
        ex.upload(b"evil", "/tmp/evil.txt")


def test_download_within_root(tmp_path, monkeypatch):
    """Download within root works."""
    import agentix.runtime.executor as executor_mod
    monkeypatch.setattr(executor_mod, "UPLOAD_ROOT", tmp_path.resolve())
    ex = Executor()
    f = tmp_path / "test.txt"
    f.write_bytes(b"data")
    result = ex.download(str(f))
    assert result == b"data"


def test_download_outside_root(tmp_path, monkeypatch):
    """Download outside root raises PermissionError."""
    import agentix.runtime.executor as executor_mod
    monkeypatch.setattr(executor_mod, "UPLOAD_ROOT", tmp_path.resolve())
    ex = Executor()
    with pytest.raises(PermissionError, match="outside allowed root"):
        ex.download("/etc/passwd")
