"""OpenAI Codex CLI agent runner. Runs inside the sandbox.

Codex is an npm-installable AI coding assistant from OpenAI.
Uses `codex` CLI in non-interactive (quiet) mode.

Expects `codex` binary available in PATH (from Nix closure or npm install).
"""

from __future__ import annotations

import asyncio
import os
import shlex

from agentix.agents.protocol import AgentInput, AgentOutput


async def run(agent_input: AgentInput) -> AgentOutput:
    """Run Codex CLI agent.

    Args:
        agent_input: AgentInput plus:
            - api_key: str (required, or set OPENAI_API_KEY in env)
            - model: str (default "o3-mini")
            - approval_mode: str (default "full-auto")
            - timeout: float | None
    """
    instruction = agent_input["instruction"]
    workdir = agent_input.get("workdir", "/workspace")
    model = agent_input.get("model", "o3-mini")
    approval_mode = agent_input.get("approval_mode", "full-auto")
    timeout = agent_input.get("timeout")

    env = dict(os.environ)
    if "api_key" in agent_input:
        env["OPENAI_API_KEY"] = agent_input["api_key"]
    if agent_input.get("env"):
        env.update(agent_input["env"])

    cmd = [
        "codex",
        "--quiet",
        "--approval-mode", approval_mode,
        "--model", model,
        shlex.quote(instruction),
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
