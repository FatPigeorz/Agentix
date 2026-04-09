"""Agent adapter protocol.

runner.py must export:

    async def run(agent_input: AgentInput) -> AgentOutput
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, TypedDict

from agentix.trajectory import Trajectory


class Step(TypedDict, total=False):
    timestamp: str
    source: str               # "user" | "agent" | "system"
    message: str
    tool_calls: list[dict]
    observation: str
    tokens: dict[str, int]    # {"prompt": N, "completion": N}


class AgentInput(TypedDict, total=False):
    instruction: str          # required — task instruction
    workdir: str              # required — working directory
    env: dict[str, str]       # extra environment variables


class AgentOutput(TypedDict, total=False):
    exit_code: int            # required
    stdout: str               # raw output
    stderr: str               # raw errors
    trajectory: list[Step]    # structured steps (optional, lightweight)
    atif_trajectory: Trajectory | None  # full ATIF trajectory for training (optional)


RunFn = Callable[[AgentInput], Coroutine[Any, Any, AgentOutput]]
