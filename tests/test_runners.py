"""Tests for agent runners — contract verification without real CLIs.

Each runner must:
1. Accept AgentInput and return AgentOutput
2. Handle timeout gracefully
3. Pass through env variables
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _load_runner(name: str):
    """Load a runner module by name without importing as a package."""
    path = Path(__file__).parent.parent / "agents" / name / "runner.py"
    spec = importlib.util.spec_from_file_location(f"runner_{name}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Template runner ───────────────────────────────────────────────


class TestTemplateRunner:
    async def test_returns_agent_output(self):
        runner = _load_runner("_template")
        result = await runner.run({
            "instruction": "test instruction",
            "workdir": "/tmp",
        })
        assert "exit_code" in result
        assert "stdout" in result
        assert "stderr" in result
        assert result["exit_code"] == 0
        assert "TODO" in result["stdout"]


# ── Aider runner ──────────────────────────────────────────────────


class TestAiderRunner:
    async def test_builds_correct_command(self):
        """Verify aider runner constructs the right CLI args."""
        runner = _load_runner("aider")

        captured_cmd = []

        async def mock_create(*args, **kwargs):
            captured_cmd.extend(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            result = await runner.run({
                "instruction": "fix the bug",
                "workdir": "/workspace",
                "api_key": "sk-test",
                "model": "claude-sonnet-4-6",
            })

        assert result["exit_code"] == 0
        # Check command args
        cmd = captured_cmd
        assert "aider" in cmd
        assert "--message" in cmd
        assert "fix the bug" in cmd
        assert "claude-sonnet-4-6" in cmd
        assert "--yes-always" in cmd

    async def test_timeout_handling(self):
        runner = _load_runner("aider")

        async def slow_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        async def mock_create(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = slow_communicate
            proc.kill = MagicMock()
            # After kill, need a real coroutine for the second communicate()
            proc.returncode = -1
            return proc

        import asyncio

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            result = await runner.run({
                "instruction": "test",
                "timeout": 0.1,
            })

        assert result["exit_code"] == -1
        assert "Timed out" in result["stderr"]


# ── Codex runner ──────────────────────────────────────────────────


class TestCodexRunner:
    async def test_builds_correct_command(self):
        runner = _load_runner("codex")

        captured_cmd = []

        async def mock_create(*args, **kwargs):
            captured_cmd.extend(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"done", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            result = await runner.run({
                "instruction": "add tests",
                "workdir": "/workspace",
                "api_key": "sk-openai",
                "model": "o3-mini",
            })

        assert result["exit_code"] == 0
        cmd = captured_cmd
        assert "codex" in cmd
        assert "--quiet" in cmd
        assert "--approval-mode" in cmd
        assert "full-auto" in cmd
        assert "o3-mini" in cmd

    async def test_timeout_handling(self):
        runner = _load_runner("codex")

        async def slow_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        async def mock_create(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = slow_communicate
            proc.kill = MagicMock()
            proc.returncode = -1
            return proc

        import asyncio

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            result = await runner.run({
                "instruction": "test",
                "timeout": 0.1,
            })

        assert result["exit_code"] == -1
        assert "Timed out" in result["stderr"]


# ── Claude Code runner ────────────────────────────────────────────


class TestClaudeCodeRunner:
    async def test_builds_correct_command(self):
        runner = _load_runner("claude-code")

        captured_cmd = []

        async def mock_create(cmd, **kwargs):
            captured_cmd.append(cmd)
            proc = AsyncMock()
            # Return valid stream-json output
            import json
            lines = [
                json.dumps({"type": "system", "subtype": "init",
                            "session_id": "s1", "model": "test"}),
                json.dumps({"type": "result", "session_id": "s1",
                            "result": "done", "total_cost_usd": 0,
                            "usage": {}}),
            ]
            proc.communicate = AsyncMock(
                return_value=(("\n".join(lines) + "\n").encode(), b""),
            )
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_shell", side_effect=mock_create):
            with patch.object(runner, "_get_agent_version", return_value="2.1.96"):
                result = await runner.run({
                    "instruction": "fix bug",
                    "api_key": "sk-test",
                    "model": "claude-sonnet-4-6",
                })

        assert result["exit_code"] == 0
        assert result["stdout"] == "done"
        # Check command contains expected flags
        cmd_str = captured_cmd[0]
        assert "--output-format" in cmd_str
        assert "stream-json" in cmd_str
        assert "--verbose" in cmd_str
        assert "--bare" in cmd_str
