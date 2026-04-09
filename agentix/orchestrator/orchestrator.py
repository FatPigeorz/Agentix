"""Orchestrator: batch agent runs with trajectory collection.

Manages the full lifecycle:
    build → create sandbox → exec agent → collect results → destroy sandbox

Supports concurrent runs with checkpoint-based resume.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

from agentix.deployment.base import Deployment
from agentix.models import SandboxConfig, SandboxInfo
from agentix.runtime.client import RuntimeClient

logger = logging.getLogger("agentix.orchestrator")


class RunConfig(BaseModel):
    """Configuration for a single agent run."""

    run_id: str = Field(description="Unique identifier for this run")
    task_image: str = Field(description="Docker image for the task environment")
    runtime_closure: str = Field(description="Nix store path for agentix runtime")
    agent_closure: str = Field(description="Nix store path for agent")
    dataset_closure: str | None = Field(default=None)
    agent_input: dict = Field(description="Opaque input passed to runner.run()")
    runner_path: str = Field(
        default="/opt/agentix/agent/runner.py",
        description="Path to runner.py inside sandbox",
    )
    timeout: float = Field(default=600, description="Max seconds for the entire run")

    @staticmethod
    def make_run_id(task_image: str, agent_closure: str, agent_input: dict) -> str:
        """Deterministic run_id from inputs — used for checkpoint dedup."""
        key = f"{task_image}|{agent_closure}|{json.dumps(agent_input, sort_keys=True)}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


class RunRecord(BaseModel):
    """Result of a single agent run, persisted to disk."""

    run_id: str
    status: str = Field(description="success | error | timeout")
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0
    has_trajectory: bool = False
    error: str | None = None


# ── Runner invocation script ─────────────────────────────────────
# Executed inside the sandbox via /exec to call runner.run()
_RUNNER_SCRIPT = r"""
import asyncio, json, sys, importlib.util

runner_path = sys.argv[1]
agent_input = json.loads(sys.argv[2])

spec = importlib.util.spec_from_file_location("runner", runner_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

result = asyncio.run(mod.run(agent_input))

# result is an AgentOutput TypedDict — serialize it
# atif_trajectory is a Pydantic model, needs special handling
output = dict(result)
traj = output.pop("atif_trajectory", None)
print(json.dumps({
    "output": output,
    "atif_trajectory": traj.model_dump() if traj else None,
}))
"""


class Orchestrator:
    """Batch agent execution with checkpoint and trajectory collection."""

    def __init__(
        self,
        deployment: Deployment,
        output_dir: str | Path = "results",
        concurrency: int = 4,
    ):
        self._deployment = deployment
        self._output_dir = Path(output_dir)
        self._concurrency = concurrency
        self._checkpoint_path = self._output_dir / ".checkpoint.json"

    def _load_checkpoint(self) -> set[str]:
        """Load set of completed run_ids."""
        if self._checkpoint_path.exists():
            data = json.loads(self._checkpoint_path.read_text())
            return set(data.get("completed", []))
        return set()

    def _save_checkpoint(self, completed: set[str]) -> None:
        """Save completed run_ids to checkpoint file."""
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path.write_text(
            json.dumps({"completed": sorted(completed)}),
        )

    async def run_batch(self, configs: list[RunConfig]) -> list[RunRecord]:
        """Run a batch of agent tasks concurrently with checkpoint resume.

        Args:
            configs: List of RunConfig describing each run.

        Returns:
            List of RunRecord results.
        """
        completed = self._load_checkpoint()
        pending = [c for c in configs if c.run_id not in completed]

        if not pending:
            logger.info("All %d runs already completed (checkpoint)", len(configs))
            return self._load_all_records(configs)

        logger.info(
            "Running %d tasks (%d already done, %d pending)",
            len(configs), len(completed), len(pending),
        )

        sem = asyncio.Semaphore(self._concurrency)
        results: list[RunRecord] = []

        async def _run_with_sem(config: RunConfig) -> RunRecord:
            async with sem:
                record = await self.run_single(config)
                completed.add(config.run_id)
                self._save_checkpoint(completed)
                return record

        tasks = [_run_with_sem(c) for c in pending]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # Load records for already-completed runs
        all_records = []
        for config in configs:
            if config.run_id in {r.run_id for r in results}:
                all_records.append(next(r for r in results if r.run_id == config.run_id))
            else:
                record = self._load_record(config.run_id)
                if record:
                    all_records.append(record)

        return all_records

    async def run_single(self, config: RunConfig) -> RunRecord:
        """Run a single agent task: create sandbox → exec → collect → destroy."""
        run_dir = self._output_dir / config.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Save run config
        (run_dir / "config.json").write_text(config.model_dump_json(indent=2))

        sandbox_info: SandboxInfo | None = None
        start = time.monotonic()

        try:
            # 1. Create sandbox
            sandbox_config = SandboxConfig(
                task_image=config.task_image,
                runtime_closure=config.runtime_closure,
                agent_closure=config.agent_closure,
                dataset_closure=config.dataset_closure,
            )
            sandbox_info = await self._deployment.create(sandbox_config)
            logger.info("[%s] Sandbox %s at %s",
                        config.run_id, sandbox_info.sandbox_id, sandbox_info.runtime_url)

            # 2. Wait for server
            async with RuntimeClient(sandbox_info.runtime_url) as client:
                await client.wait_until_alive(timeout=60)

                # 3. Execute agent runner inside sandbox
                agent_input_json = json.dumps(config.agent_input)
                exec_result = await client.exec(
                    command=(
                        f"python3 -c {_shell_quote(_RUNNER_SCRIPT)}"
                        f" {_shell_quote(config.runner_path)}"
                        f" {_shell_quote(agent_input_json)}"
                    ),
                    timeout=config.timeout,
                )

            duration = time.monotonic() - start

            # 4. Parse result
            if exec_result.exit_code != 0:
                record = RunRecord(
                    run_id=config.run_id,
                    status="error",
                    exit_code=exec_result.exit_code,
                    stdout=exec_result.stdout,
                    stderr=exec_result.stderr,
                    duration_s=duration,
                )
            else:
                record = self._parse_exec_output(
                    config.run_id, exec_result.stdout, exec_result.stderr, duration,
                )

            # 5. Save results
            self._save_record(run_dir, record, exec_result.stdout)
            logger.info("[%s] Done: status=%s duration=%.1fs",
                        config.run_id, record.status, duration)
            return record

        except Exception as e:
            duration = time.monotonic() - start
            record = RunRecord(
                run_id=config.run_id,
                status="error",
                error=str(e),
                duration_s=duration,
            )
            self._save_record(run_dir, record)
            logger.error("[%s] Failed: %s", config.run_id, e)
            return record

        finally:
            # 6. Cleanup sandbox
            if sandbox_info:
                try:
                    await self._deployment.delete(sandbox_info.sandbox_id)
                except Exception as e:
                    logger.warning("[%s] Cleanup failed: %s", config.run_id, e)

    def _parse_exec_output(
        self, run_id: str, stdout: str, stderr: str, duration: float,
    ) -> RunRecord:
        """Parse the JSON output from the runner script."""
        try:
            data = json.loads(stdout)
            output = data.get("output", {})
            has_traj = data.get("atif_trajectory") is not None
            return RunRecord(
                run_id=run_id,
                status="success",
                exit_code=output.get("exit_code", 0),
                stdout=output.get("stdout", ""),
                stderr=output.get("stderr", stderr),
                duration_s=duration,
                has_trajectory=has_traj,
            )
        except (json.JSONDecodeError, KeyError) as e:
            return RunRecord(
                run_id=run_id,
                status="error",
                stdout=stdout[:2000],
                stderr=stderr[:2000],
                duration_s=duration,
                error=f"Failed to parse runner output: {e}",
            )

    def _save_record(
        self, run_dir: Path, record: RunRecord, raw_stdout: str = "",
    ) -> None:
        """Save run record and trajectory to disk."""
        (run_dir / "record.json").write_text(record.model_dump_json(indent=2))

        # Extract and save ATIF trajectory if present
        if raw_stdout and record.has_trajectory:
            try:
                data = json.loads(raw_stdout)
                traj_data = data.get("atif_trajectory")
                if traj_data:
                    (run_dir / "trajectory.jsonl").write_text(
                        json.dumps(traj_data) + "\n",
                    )
            except (json.JSONDecodeError, KeyError):
                pass

    def _load_record(self, run_id: str) -> RunRecord | None:
        """Load a saved RunRecord from disk."""
        path = self._output_dir / run_id / "record.json"
        if path.exists():
            return RunRecord.model_validate_json(path.read_text())
        return None

    def _load_all_records(self, configs: list[RunConfig]) -> list[RunRecord]:
        """Load all records for given configs."""
        records = []
        for config in configs:
            record = self._load_record(config.run_id)
            if record:
                records.append(record)
        return records

    def summary(self) -> dict:
        """Generate summary stats from all saved records."""
        records = []
        for record_path in self._output_dir.glob("*/record.json"):
            try:
                records.append(RunRecord.model_validate_json(record_path.read_text()))
            except Exception:
                continue

        total = len(records)
        success = sum(1 for r in records if r.status == "success")
        errors = sum(1 for r in records if r.status == "error")
        with_traj = sum(1 for r in records if r.has_trajectory)
        total_duration = sum(r.duration_s for r in records)

        return {
            "total": total,
            "success": success,
            "errors": errors,
            "with_trajectory": with_traj,
            "total_duration_s": round(total_duration, 1),
            "avg_duration_s": round(total_duration / total, 1) if total else 0,
        }


def _shell_quote(s: str) -> str:
    """Shell-quote a string for use in exec commands."""
    import shlex
    return shlex.quote(s)
