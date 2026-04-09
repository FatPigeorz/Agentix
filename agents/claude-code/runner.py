"""Claude Code agent runner. No agentix import required."""

from __future__ import annotations

import asyncio
import os
import shlex

async def run(ctx: dict) -> dict:
    """Run Claude Code.

    ctx keys (from dataset.setup or host):
        instruction: str        # required
        api_key: str            # required
        model: str              # default "claude-sonnet-4-20250514"
        max_turns: int | None
        timeout: float | None

    Returns plain dict. Trajectory collection is optional —
    import agentix helpers only if you want ATIF output.
    """
    instruction = ctx["instruction"]
    api_key = ctx["api_key"]
    model = ctx.get("model", "claude-sonnet-4-20250514")
    max_turns = ctx.get("max_turns")
    timeout = ctx.get("timeout")

    cmd_parts = [
        "claude",
        "-p", shlex.quote(instruction),
        "--output-format", "text",
        "-m", model,
    ]
    if max_turns is not None:
        cmd_parts.extend(["--max-turns", str(max_turns)])

    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = api_key

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
