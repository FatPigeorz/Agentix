"""Parallel rollout primitives for RL training.

`RolloutPool` spawns a fixed set of warm sandboxes and bounds concurrent
rollouts to that size. Use it when you need to run the same namespace
against many tasks (e.g. SWE-bench instances) without paying sandbox-cold-
start latency for each.
"""

from agentix.rollout.pool import RolloutPool

__all__ = ["RolloutPool"]
