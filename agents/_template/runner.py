"""Agent runner template.

Copy this file to agents/<your-agent>/runner.py and implement run().

The runner is loaded inside the sandbox and called by the orchestrator via:
    python3 -c "... import runner; result = asyncio.run(runner.run(input)) ..."

Protocol:
    Input:  AgentInput (TypedDict with instruction, workdir, env)
    Output: AgentOutput (TypedDict with exit_code, stdout, stderr, atif_trajectory)
"""

from __future__ import annotations

import asyncio
import os

from agentix.agents.protocol import AgentInput, AgentOutput


async def run(agent_input: AgentInput) -> AgentOutput:
    """Run the agent on the given task.

    Args:
        agent_input: {
            "instruction": str,    # required — task description
            "workdir": str,        # working directory inside sandbox
            "env": {"KEY": "VAL"}, # extra environment variables
            # ... agent-specific keys below ...
        }

    Returns:
        AgentOutput with exit_code, stdout, stderr.
        Optionally set atif_trajectory for training data collection.
    """
    instruction = agent_input["instruction"]
    workdir = agent_input.get("workdir", "/workspace")
    env = {**os.environ, **(agent_input.get("env") or {})}

    # TODO: Replace with your agent's CLI command
    proc = await asyncio.create_subprocess_shell(
        f"echo 'TODO: implement agent for: {instruction}'",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
    )
    stdout, stderr = await proc.communicate()

    return AgentOutput(
        exit_code=proc.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )
