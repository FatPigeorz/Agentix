"""Run Claude Code on a SWE-bench instance.

Two sandboxes:
  1. Agent sandbox: swebench image + runtime + claude-code closure
     → run agent → collect patch
  2. Eval sandbox: swebench image + runtime + swebench closure
     → verify(instance, patch) → reward

Usage:
    python examples/claude-code-swebench/run.py \
        --instance instance.json \
        --runtime-closure /nix/store/xxx-runtime \
        --agent-closure /nix/store/xxx-claude-code \
        --dataset-closure /nix/store/xxx-swebench
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from agentix.deployment.docker import DockerDeployment
from agentix.models import SandboxConfig
from agentix.runtime.client import RuntimeClient

logger = logging.getLogger("example")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)


async def run_agent(
    instance: dict,
    deployment: DockerDeployment,
    runtime_closure: str,
    agent_closure: str,
    timeout: float,
    proxy_url: str | None,
) -> str:
    """Sandbox A: run agent, return patch."""
    instance_id = instance["instance_id"]
    logger.info("[%s] starting agent sandbox", instance_id)

    config = SandboxConfig(
        task_image=instance["image"],
        runtime_closure=runtime_closure,
        closures={"claude": agent_closure},
    )

    sandbox = await deployment.create(config)
    try:
        async with RuntimeClient(sandbox.runtime_url, timeout=timeout + 60) as client:
            await client.wait_until_alive(timeout=60)

            # Build env for claude
            agent_env = {
                "IS_SANDBOX": "1",
                "HOME": "/root",
                "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "dummy"),
            }
            if proxy_url:
                agent_env["ANTHROPIC_BASE_URL"] = proxy_url

            t = time.monotonic()
            result = await client.call("claude", "run", {
                "instruction": instance["problem_statement"],
                "workdir": "/testbed",
                "timeout": timeout,
                "env": agent_env,
            })
            logger.info("[%s] agent done (%.1fs): exit_code=%s, patch=%d chars",
                        instance_id, time.monotonic() - t,
                        result.get("exit_code"), len(result.get("patch", "")))

            return result.get("patch", "")
    finally:
        await deployment.delete(sandbox.sandbox_id)


async def run_eval(
    instance: dict,
    patch: str,
    deployment: DockerDeployment,
    runtime_closure: str,
    dataset_closure: str,
) -> dict:
    """Sandbox B: evaluate patch, return result."""
    instance_id = instance["instance_id"]
    logger.info("[%s] starting eval sandbox", instance_id)

    config = SandboxConfig(
        task_image=instance["image"],
        runtime_closure=runtime_closure,
        closures={"swebench": dataset_closure},
    )

    sandbox = await deployment.create(config)
    try:
        async with RuntimeClient(sandbox.runtime_url, timeout=600) as client:
            await client.wait_until_alive(timeout=60)

            t = time.monotonic()
            result = await client.call("swebench", "verify", {
                "instance": instance,
                "model_patch": patch,
            })
            logger.info("[%s] eval done (%.1fs): pass=%s, reason=%s",
                        instance_id, time.monotonic() - t,
                        result.get("pass"), result.get("reason", ""))

            return result
    finally:
        await deployment.delete(sandbox.sandbox_id)


async def main_async(args):
    instance = json.loads(Path(args.instance).read_text())
    instance_id = instance.get("instance_id", "unknown")
    t0 = time.monotonic()
    logger.info("[%s] starting", instance_id)

    deployment = DockerDeployment(host_port_start=args.port_start)

    # 1. Run agent → get patch
    patch = await run_agent(
        instance, deployment,
        args.runtime_closure, args.agent_closure,
        args.timeout, args.proxy_url,
    )

    # 2. Run eval → get reward
    verify_result = {}
    if patch.strip():
        verify_result = await run_eval(
            instance, patch, deployment,
            args.runtime_closure, args.dataset_closure,
        )
    else:
        verify_result = {"pass": False, "reason": "No patch produced"}
        logger.info("[%s] no patch, skipping eval", instance_id)

    # Write result
    result = {
        "instance_id": instance_id,
        "model_patch": patch,
        "verify": verify_result,
        "elapsed": round(time.monotonic() - t0, 1),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str))
    logger.info("[%s] done %.1fs → %s", instance_id, time.monotonic() - t0, args.output)


def main():
    parser = argparse.ArgumentParser(description="Run Claude Code on SWE-bench instance")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--runtime-closure", required=True)
    parser.add_argument("--agent-closure", required=True)
    parser.add_argument("--dataset-closure", required=True)
    parser.add_argument("--proxy-url", default=None, help="Anthropic proxy URL (e.g. http://localhost:8082)")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", default="result.json")
    parser.add_argument("--port-start", type=int, default=18000)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
