"""Eval CLI: runs inside the sandbox.

Usage:
    python -m agentix.eval --agent /opt/agent [--dataset /opt/dataset] [--output /output/result.json]

Orchestrates: dataset.setup(ctx) → runner.run(ctx) → dataset.verify(ctx)
All functions receive and return plain dicts. No agentix import required in plugins.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

logger = logging.getLogger("agentix.eval")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# ---------------------------------------------------------------------------
# D1: Plugin loading with actionable error messages
# ---------------------------------------------------------------------------

class PluginLoadError(Exception):
    """Raised when a plugin fails to load."""


def _load_module(path: Path, name: str):
    if not path.exists():
        raise PluginLoadError(f"Plugin file not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"Cannot import {path} — is it a valid Python file?")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginLoadError(f"Failed to load {path}: {exc}") from exc
    return module


def _validate_agent(module, path: Path):
    if not hasattr(module, "run"):
        raise PluginLoadError(f"{path} must define: async def run(ctx: dict) -> dict")
    if not asyncio.iscoroutinefunction(module.run):
        raise PluginLoadError(f"{path}: run() must be async (use 'async def run')")


def _validate_dataset(module, path: Path):
    for fn_name in ("setup", "verify"):
        if hasattr(module, fn_name) and not asyncio.iscoroutinefunction(getattr(module, fn_name)):
            raise PluginLoadError(f"{path}: {fn_name}() must be async")


# ---------------------------------------------------------------------------
# E2 + R6: Eval pipeline with lifecycle hooks and overall timeout
# ---------------------------------------------------------------------------

async def _run_eval_inner(agent_dir: str, dataset_dir: str | None, output_path: str) -> dict:
    run_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    logger.info("[%s] eval start agent=%s dataset=%s", run_id, agent_dir, dataset_dir)

    # Load agent plugin
    runner_path = Path(agent_dir) / "runner.py"
    runner = _load_module(runner_path, "agent_runner")
    _validate_agent(runner, runner_path)

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
            _validate_dataset(dataset, dataset_path)

    logger.info("[%s] plugins loaded in %.1fs", run_id, time.monotonic() - t0)

    # Build initial context
    ctx = {
        "agent_dir": agent_dir,
        "dataset_dir": dataset_dir,
        "workdir": os.getcwd(),
    }

    # Pipeline with lifecycle hooks
    metrics = {}
    try:
        # 1. Setup — dataset prepares environment, returns agent input
        if dataset and hasattr(dataset, "setup"):
            t_phase = time.monotonic()
            logger.info("[%s] dataset.setup()", run_id)
            setup_result = await dataset.setup(ctx)
            ctx.update(setup_result)
            logger.info("[%s] dataset.setup() done in %.1fs", run_id, time.monotonic() - t_phase)

        # 2. Run — agent executes
        t_phase = time.monotonic()
        logger.info("[%s] runner.run()", run_id)
        run_result = await runner.run(ctx)
        ctx["run_result"] = run_result
        logger.info("[%s] runner.run() done in %.1fs", run_id, time.monotonic() - t_phase)

        # 3. Verify — dataset collects metrics
        if dataset and hasattr(dataset, "verify"):
            t_phase = time.monotonic()
            logger.info("[%s] dataset.verify()", run_id)
            metrics = await dataset.verify(ctx)
            logger.info("[%s] dataset.verify() done in %.1fs", run_id, time.monotonic() - t_phase)
    except Exception as exc:
        # on_error hooks — optional, best-effort
        for mod in [runner, dataset]:
            if mod and hasattr(mod, "on_error"):
                try:
                    await mod.on_error(ctx, exc)
                except Exception:
                    logger.exception("on_error hook failed")
        raise
    finally:
        # teardown hooks — always run, reverse order
        for mod in [dataset, runner]:
            if mod and hasattr(mod, "teardown"):
                try:
                    await mod.teardown(ctx)
                except Exception:
                    logger.exception("teardown hook failed")

    # Build output
    result = {
        "output": run_result,
        "metrics": metrics,
    }

    # Write
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    logger.info("[%s] eval complete total=%.1fs", run_id, time.monotonic() - t0)

    return result


async def run_eval(
    agent_dir: str,
    dataset_dir: str | None,
    output_path: str,
    timeout: float = 3600,
) -> dict:
    return await asyncio.wait_for(
        _run_eval_inner(agent_dir, dataset_dir, output_path),
        timeout=timeout,
    )


def main():
    parser = argparse.ArgumentParser(description="agentix eval")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--output", default="/output/result.json")
    parser.add_argument("--timeout", type=float, default=3600,
                        help="Overall eval timeout in seconds (default: 3600)")
    args = parser.parse_args()

    asyncio.run(run_eval(args.agent, args.dataset, args.output, timeout=args.timeout))


if __name__ == "__main__":
    main()
