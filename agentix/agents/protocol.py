"""Agent protocol: every agent runner must implement this interface.

Each agents/{name}/runner.py must export:

    async def run(agent_input: dict) -> dict

That's it. The runtime server loads runner.py and calls run().
"""

from typing import Callable, Coroutine, Any

# The type signature every runner.run must match.
RunFn = Callable[[dict], Coroutine[Any, Any, dict]]
