"""Run Claude Code on SWE-bench Verified via agentix.

Usage:
    python benchmarks/swebench/run.py \
        --runtime-closure /nix/store/xxx-runtime \
        --agent-closure /nix/store/xxx-claude-code \
        --output predictions.jsonl \
        --max-instances 5 \
        --timeout 300

Outputs predictions.jsonl compatible with:
    python -m swebench.harness.run_evaluation \
        --dataset_name princeton-nlp/SWE-bench_Verified \
        --predictions_path predictions.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from agentix.deployment.docker import DockerDeployment
from agentix.models import SandboxConfig
from agentix.runtime.client import RuntimeClient

logger = logging.getLogger("agentix.swebench")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)

# SWE-bench docker image naming convention
SWEBENCH_IMAGE = "ghcr.io/swe-bench/swe-bench-eval:{instance_id}"


def load_dataset(dataset_name: str, split: str, max_instances: int | None = None):
    """Load SWE-bench dataset from HuggingFace."""
    from datasets import load_dataset as hf_load
    ds = hf_load(dataset_name, split=split)
    if max_instances:
        ds = ds.select(range(min(max_instances, len(ds))))
    return ds


def build_prompt(instance: dict) -> str:
    """Build the instruction prompt from a SWE-bench instance."""
    return (
        f"You are solving a GitHub issue. Here is the problem statement:\n\n"
        f"{instance['problem_statement']}\n\n"
        f"The repository is already cloned in the current directory. "
        f"Please fix the issue by editing the relevant files. "
        f"Do not run tests yourself — just make the code changes."
    )


async def run_instance(
    instance: dict,
    deployment: DockerDeployment,
    runtime_closure: str,
    agent_closure: str,
    timeout: float,
) -> dict:
    """Run agent on a single SWE-bench instance, return prediction dict."""
    instance_id = instance["instance_id"]
    t0 = time.monotonic()
    logger.info("[%s] starting", instance_id)

    # SWE-bench provides per-instance Docker images
    task_image = SWEBENCH_IMAGE.format(instance_id=instance_id)

    config = SandboxConfig(
        task_image=task_image,
        runtime_closure=runtime_closure,
        agent_closure=agent_closure,
    )

    sandbox = None
    try:
        # 1. Create sandbox
        sandbox = await deployment.create(config)
        async with RuntimeClient(sandbox.runtime_url, timeout=timeout) as client:
            await client.wait_until_alive(timeout=60)

            # 2. Find the repo directory inside the container
            repo_name = instance["repo"].replace("/", "__")
            find_result = await client.exec(
                f"find / -maxdepth 3 -type d -name '{repo_name}' 2>/dev/null | head -1",
                timeout=30,
            )
            workdir = find_result.stdout.strip() or "/testbed"

            # 3. Reset to base commit
            await client.exec(
                f"cd {workdir} && git checkout -f {instance['base_commit']}",
                timeout=60,
            )

            # 4. Run the agent
            prompt = build_prompt(instance)
            result = await client.exec(
                f"cd {workdir} && claude -p {json.dumps(prompt)} --output-format text",
                timeout=timeout,
                env={"ANTHROPIC_API_KEY": "", "CLAUDE_MODEL": "claude-sonnet-4-20250514"},
            )
            logger.info("[%s] agent finished (exit=%d, %.1fs)",
                        instance_id, result.exit_code, time.monotonic() - t0)

            # 5. Collect the git diff as the model patch
            diff_result = await client.exec(
                f"cd {workdir} && git diff",
                timeout=30,
            )
            model_patch = diff_result.stdout

    except Exception as e:
        logger.error("[%s] failed: %s", instance_id, e)
        model_patch = ""
    finally:
        if sandbox:
            try:
                await deployment.delete(sandbox.sandbox_id)
            except Exception:
                pass

    elapsed = time.monotonic() - t0
    logger.info("[%s] done in %.1fs (patch: %d chars)", instance_id, elapsed, len(model_patch))

    return {
        "instance_id": instance_id,
        "model_patch": model_patch,
        "model_name_or_path": "claude-code",
    }


async def main_async(args):
    logger.info("Loading dataset: %s", args.dataset)
    ds = load_dataset(args.dataset, args.split, args.max_instances)
    logger.info("Loaded %d instances", len(ds))

    deployment = DockerDeployment(host_port_start=args.port_start)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Run instances with concurrency limit
    semaphore = asyncio.Semaphore(args.workers)

    async def run_with_limit(instance):
        async with semaphore:
            return await run_instance(
                instance, deployment, args.runtime_closure, args.agent_closure, args.timeout,
            )

    tasks = [run_with_limit(inst) for inst in ds]
    predictions = await asyncio.gather(*tasks, return_exceptions=True)

    # Write predictions.jsonl
    with open(output_path, "w") as f:
        for pred in predictions:
            if isinstance(pred, Exception):
                logger.error("Task failed: %s", pred)
                continue
            f.write(json.dumps(pred) + "\n")

    # Summary
    total = len(predictions)
    success = sum(1 for p in predictions if not isinstance(p, Exception) and p["model_patch"])
    logger.info("Done: %d/%d instances produced patches → %s", success, total, output_path)


def main():
    parser = argparse.ArgumentParser(description="Run Claude Code on SWE-bench Verified")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    parser.add_argument("--split", default="test")
    parser.add_argument("--runtime-closure", required=True, help="Nix store path for agentix runtime")
    parser.add_argument("--agent-closure", required=True, help="Nix store path for claude-code agent")
    parser.add_argument("--output", default="predictions.jsonl")
    parser.add_argument("--max-instances", type=int, default=None, help="Limit number of instances")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent instances")
    parser.add_argument("--timeout", type=float, default=300, help="Per-instance timeout (seconds)")
    parser.add_argument("--port-start", type=int, default=18000)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
