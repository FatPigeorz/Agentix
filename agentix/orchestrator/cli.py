"""CLI entry point for agentix orchestrator.

Usage:
    agentix-run --task-image IMG --runtime-closure PATH --agent-closure PATH \
        --agent-input '{"instruction": "fix the bug"}' \
        [--output-dir results] [--concurrency 4] [--timeout 600]

    agentix-run --batch batch.jsonl \
        [--output-dir results] [--concurrency 4]

    agentix-run --summary --output-dir results
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agentix.deployment.docker import DockerDeployment
from agentix.orchestrator.orchestrator import Orchestrator, RunConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentix-run",
        description="Run agents in sandboxes and collect trajectories",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── run: single or batch ──────────────────────────────────
    run_p = sub.add_parser("run", help="Execute agent run(s)")
    run_p.add_argument("--task-image", help="Docker image for task env")
    run_p.add_argument("--runtime-closure", help="Nix store path for runtime")
    run_p.add_argument("--agent-closure", help="Nix store path for agent")
    run_p.add_argument("--dataset-closure", default=None, help="Nix store path for dataset")
    run_p.add_argument("--agent-input", help="JSON string for agent_input")
    run_p.add_argument("--batch", help="Path to JSONL file with RunConfig per line")
    run_p.add_argument("--output-dir", default="results", help="Output directory")
    run_p.add_argument("--concurrency", type=int, default=4)
    run_p.add_argument("--timeout", type=float, default=600)

    # ── summary ───────────────────────────────────────────────
    sum_p = sub.add_parser("summary", help="Print run summary")
    sum_p.add_argument("--output-dir", default="results")

    args = parser.parse_args()

    if args.command == "summary":
        deployment = DockerDeployment()
        orch = Orchestrator(deployment, output_dir=args.output_dir)
        summary = orch.summary()
        print(json.dumps(summary, indent=2))
        return

    # command == "run"
    if args.batch:
        configs = _load_batch(args.batch)
    elif args.task_image and args.agent_input:
        agent_input = json.loads(args.agent_input)
        run_id = RunConfig.make_run_id(args.task_image, args.agent_closure, agent_input)
        configs = [RunConfig(
            run_id=run_id,
            task_image=args.task_image,
            runtime_closure=args.runtime_closure,
            agent_closure=args.agent_closure,
            dataset_closure=args.dataset_closure,
            agent_input=agent_input,
            timeout=args.timeout,
        )]
    else:
        parser.error("Provide --batch or (--task-image + --agent-input)")
        return

    deployment = DockerDeployment()
    orch = Orchestrator(
        deployment,
        output_dir=args.output_dir,
        concurrency=args.concurrency,
    )
    records = asyncio.run(orch.run_batch(configs))

    # Print summary
    success = sum(1 for r in records if r.status == "success")
    print(f"\nDone: {success}/{len(records)} succeeded")
    for r in records:
        if r.status != "success":
            print(f"  FAIL [{r.run_id}]: {r.error or r.stderr[:100]}")


def _load_batch(path: str) -> list[RunConfig]:
    """Load RunConfig list from a JSONL file."""
    configs = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                configs.append(RunConfig.model_validate_json(line))
            except Exception as e:
                print(f"Warning: skipping line {i}: {e}", file=sys.stderr)
    return configs


if __name__ == "__main__":
    main()
