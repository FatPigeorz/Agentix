"""Claude Code agent runner. Runs inside the sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import shlex

from agentix.agents.protocol import AgentInput, AgentOutput
from agentix.parsers.claude_code import parse_stream_json


async def run(agent_input: AgentInput) -> AgentOutput:
    """Run Claude Code agent with trajectory collection.

    Args:
        agent_input: AgentInput with instruction, workdir, env.
            Also accepts extra keys:
            - api_key: str (required, or set ANTHROPIC_API_KEY in env)
            - model: str (default "claude-sonnet-4-20250514")
            - max_turns: int | None
            - timeout: float | None

    Returns:
        AgentOutput with exit_code, stdout, stderr, and atif_trajectory.
    """
    instruction = agent_input["instruction"]
    model = agent_input.get("model", "claude-sonnet-4-20250514")
    max_turns = agent_input.get("max_turns")
    timeout = agent_input.get("timeout")

    cmd_parts = [
        "claude",
        "-p", shlex.quote(instruction),
        "--output-format", "stream-json",
        "--verbose",
        "--bare",
        "-m", model,
    ]
    if max_turns is not None:
        cmd_parts.extend(["--max-turns", str(max_turns)])

    env = dict(os.environ)
    # API key from agent_input.env or direct key
    if "api_key" in agent_input:
        env["ANTHROPIC_API_KEY"] = agent_input["api_key"]
    if agent_input.get("env"):
        env.update(agent_input["env"])

    workdir = agent_input.get("workdir")

    proc = await asyncio.create_subprocess_shell(
        " ".join(cmd_parts),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=workdir,
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

    stdout_str = stdout.decode(errors="replace")
    stderr_str = stderr.decode(errors="replace")

    # Parse stream-json lines into ATIF trajectory
    lines = stdout_str.splitlines()
    trajectory = parse_stream_json(lines, agent_version=_get_agent_version())

    # Extract final result text from the last "result" event
    result_text = ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                result_text = event.get("result", "")
                break
        except json.JSONDecodeError:
            continue

    return AgentOutput(
        exit_code=proc.returncode or 0,
        stdout=result_text,
        stderr=stderr_str,
        atif_trajectory=trajectory,
    )


def _get_agent_version() -> str:
    """Try to detect Claude Code CLI version."""
    try:
        import subprocess
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"
