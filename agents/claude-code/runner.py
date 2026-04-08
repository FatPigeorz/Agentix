"""Claude Code agent runner. Runs inside the sandbox."""

from __future__ import annotations

import asyncio
import os
import shlex


async def run(agent_input: dict) -> dict:
    """Run Claude Code agent.

    Args:
        agent_input: {
            "instruction": str,        # required
            "api_key": str,            # required
            "model": str,              # default "claude-sonnet-4-20250514"
            "output_format": str,      # default "text"
            "max_turns": int | None,   # optional
            "timeout": float | None,   # optional
        }

    Returns:
        {"exit_code": int, "stdout": str, "stderr": str}
    """
    instruction = agent_input["instruction"]
    model = agent_input.get("model", "claude-sonnet-4-20250514")
    output_format = agent_input.get("output_format", "text")
    max_turns = agent_input.get("max_turns")
    timeout = agent_input.get("timeout")

    cmd_parts = [
        "claude",
        "-p", shlex.quote(instruction),
        "--output-format", output_format,
        "-m", model,
    ]
    if max_turns is not None:
        cmd_parts.extend(["--max-turns", str(max_turns)])

    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = agent_input["api_key"]

    proc = await asyncio.create_subprocess_shell(
        " ".join(cmd_parts),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"exit_code": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}

    return {
        "exit_code": proc.returncode or 0,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }
