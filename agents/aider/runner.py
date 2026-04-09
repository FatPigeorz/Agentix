"""Aider agent runner. Runs inside the sandbox.

Aider is a pip-installable AI coding assistant. It reads the instruction,
applies edits to files, and can run tests.

Expects `aider` binary available in PATH (from Nix closure or pip install).
"""

from __future__ import annotations

import asyncio
import os

from agentix.agents.protocol import AgentInput, AgentOutput


async def run(agent_input: AgentInput) -> AgentOutput:
    """Run Aider agent.

    Args:
        agent_input: AgentInput plus:
            - api_key: str (required, or set in env)
            - model: str (default "claude-sonnet-4-20250514")
            - timeout: float | None
    """
    instruction = agent_input["instruction"]
    workdir = agent_input.get("workdir", "/workspace")
    model = agent_input.get("model", "claude-sonnet-4-20250514")
    timeout = agent_input.get("timeout")

    env = dict(os.environ)
    if "api_key" in agent_input:
        env["ANTHROPIC_API_KEY"] = agent_input["api_key"]
    if agent_input.get("env"):
        env.update(agent_input["env"])

    cmd = [
        "aider",
        "--message", instruction,
        "--model", model,
        "--yes-always",          # auto-confirm file edits
        "--no-auto-commits",     # don't auto-commit
        "--no-git",              # don't init git
        "--no-pretty",           # plain output
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return AgentOutput(
            exit_code=-1,
            stdout="",
            stderr=f"Timed out after {timeout}s",
        )

    return AgentOutput(
        exit_code=proc.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )
