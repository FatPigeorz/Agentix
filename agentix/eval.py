"""Eval CLI: runs inside the sandbox.

Usage:
    python -m agentix.eval --agent /opt/agent --dataset /opt/dataset [--output /output/result.json]

Orchestrates: dataset.setup → runner.run → dataset.verify
Writes result to --output as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("agentix.eval")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_eval(agent_dir: str, dataset_dir: str | None, output_path: str) -> dict:
    # Load agent plugin
    runner_path = Path(agent_dir) / "runner.py"
    if not runner_path.exists():
        raise FileNotFoundError(f"No runner.py in {agent_dir}")
    runner = _load_module(runner_path, "agent_runner")

    # Add agent bin/ to PATH
    bin_dir = Path(agent_dir) / "bin"
    if bin_dir.exists():
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

    # Load dataset plugin (optional)
    dataset = None
    if dataset_dir:
        dataset_path = Path(dataset_dir) / "dataset.py"
        if dataset_path.exists():
            dataset = _load_module(dataset_path, "dataset_plugin")

    # 1. Setup
    agent_input = {}
    if dataset and hasattr(dataset, "setup"):
        logger.info("Running dataset.setup()")
        agent_input = await dataset.setup()

    # 2. Run agent
    logger.info("Running agent")
    run_result = await runner.run(agent_input)

    # 3. Verify
    metrics = {}
    if dataset and hasattr(dataset, "verify"):
        logger.info("Running dataset.verify()")
        metrics = await dataset.verify()

    # Build result
    result = {
        "output": run_result.output,
        "trajectory": run_result.trajectory.model_dump() if run_result.trajectory else None,
        "metrics": metrics,
    }

    # Write output
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Result written to %s", output_path)

    return result


def main():
    parser = argparse.ArgumentParser(description="agentix eval")
    parser.add_argument("--agent", required=True, help="Agent plugin path")
    parser.add_argument("--dataset", default=None, help="Dataset plugin path")
    parser.add_argument("--output", default="/output/result.json", help="Output JSON path")
    args = parser.parse_args()

    result = asyncio.run(run_eval(args.agent, args.dataset, args.output))

    if result.get("output", {}).get("exit_code", 1) != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
