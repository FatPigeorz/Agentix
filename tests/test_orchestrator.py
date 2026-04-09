"""Tests for agentix.orchestrator — batch runs, checkpoint, result collection."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentix.models import SandboxConfig, SandboxInfo
from agentix.orchestrator.orchestrator import Orchestrator, RunConfig, RunRecord

# ── RunConfig ─────────────────────────────────────────────────────


class TestRunConfig:
    def test_make_run_id_deterministic(self):
        id1 = RunConfig.make_run_id("img:1", "/nix/agent", {"instruction": "fix"})
        id2 = RunConfig.make_run_id("img:1", "/nix/agent", {"instruction": "fix"})
        assert id1 == id2
        assert len(id1) == 16

    def test_make_run_id_differs_on_input_change(self):
        id1 = RunConfig.make_run_id("img:1", "/nix/agent", {"instruction": "fix"})
        id2 = RunConfig.make_run_id("img:1", "/nix/agent", {"instruction": "test"})
        assert id1 != id2

    def test_serialization(self):
        cfg = RunConfig(
            run_id="abc123",
            task_image="img:1",
            runtime_closure="/nix/rt",
            agent_closure="/nix/ag",
            agent_input={"instruction": "fix"},
        )
        j = cfg.model_dump_json()
        cfg2 = RunConfig.model_validate_json(j)
        assert cfg2.run_id == "abc123"
        assert cfg2.agent_input["instruction"] == "fix"


class TestRunRecord:
    def test_defaults(self):
        r = RunRecord(run_id="r1", status="success")
        assert r.exit_code == 0
        assert r.has_trajectory is False
        assert r.error is None

    def test_roundtrip(self):
        r = RunRecord(
            run_id="r1", status="error", exit_code=1,
            stderr="boom", error="crash", duration_s=12.5,
        )
        j = r.model_dump_json()
        r2 = RunRecord.model_validate_json(j)
        assert r2.status == "error"
        assert r2.error == "crash"


# ── Checkpoint ────────────────────────────────────────────────────


class TestCheckpoint:
    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = AsyncMock()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            assert orch._load_checkpoint() == set()

            orch._save_checkpoint({"a", "b", "c"})
            loaded = orch._load_checkpoint()
            assert loaded == {"a", "b", "c"}

    def test_checkpoint_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = AsyncMock()
            orch1 = Orchestrator(deployment, output_dir=tmpdir)
            orch1._save_checkpoint({"done1", "done2"})

            orch2 = Orchestrator(deployment, output_dir=tmpdir)
            assert orch2._load_checkpoint() == {"done1", "done2"}


# ── Mock deployment for integration tests ─────────────────────────


class MockDeployment:
    """Fake deployment that tracks create/delete calls."""

    def __init__(self):
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._counter = 0

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        self._counter += 1
        sid = f"mock-{self._counter}"
        self.created.append(sid)
        return SandboxInfo(
            sandbox_id=sid,
            runtime_url="http://localhost:19999",
            status="running",
        )

    async def get(self, sandbox_id: str) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url="http://localhost:19999",
            status="running",
        )

    async def update(self, sandbox_id: str, config: SandboxConfig,
                     *, force_recreate: bool = False) -> SandboxInfo:
        return await self.get(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)


def _make_config(run_id: str = "test-run-1", instruction: str = "fix") -> RunConfig:
    return RunConfig(
        run_id=run_id,
        task_image="ubuntu:22.04",
        runtime_closure="/nix/store/rt",
        agent_closure="/nix/store/ag",
        agent_input={"instruction": instruction},
        timeout=30,
    )


# ── run_single ────────────────────────────────────────────────────


class TestRunSingle:
    async def test_success_flow(self):
        """Test _parse_exec_output with a successful runner output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            runner_output = json.dumps({
                "output": {"exit_code": 0, "stdout": "fixed!", "stderr": ""},
                "atif_trajectory": None,
            })

            record = orch._parse_exec_output("test-run-1", runner_output, "", 5.0)

            assert record.status == "success"
            assert record.exit_code == 0
            assert record.stdout == "fixed!"

    async def test_parse_exec_output_with_trajectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            traj_data = {
                "schema_version": "ATIF-v1.4",
                "session_id": "s1",
                "agent": {"name": "test", "version": "1.0", "model_name": "m"},
                "steps": [],
                "final_metrics": {
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "total_cached_tokens": 0,
                    "total_cost_usd": 0,
                    "total_steps": 0,
                },
            }
            stdout = json.dumps({
                "output": {"exit_code": 0, "stdout": "ok", "stderr": ""},
                "atif_trajectory": traj_data,
            })

            record = orch._parse_exec_output("r1", stdout, "", 3.0)
            assert record.status == "success"
            assert record.has_trajectory is True

    async def test_parse_exec_output_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            record = orch._parse_exec_output("r1", "not json", "err", 1.0)
            assert record.status == "error"
            assert "Failed to parse" in record.error

    async def test_sandbox_cleanup_on_error(self):
        """Sandbox should be deleted even if run fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)
            config = _make_config()

            # Patch RuntimeClient to fail
            with patch(
                "agentix.orchestrator.orchestrator.RuntimeClient",
            ) as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.wait_until_alive.side_effect = TimeoutError("dead")
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                record = await orch.run_single(config)

            assert record.status == "error"
            # Sandbox should still be cleaned up
            assert len(deployment.deleted) == 1


# ── save/load ─────────────────────────────────────────────────────


class TestSaveLoad:
    def test_save_and_load_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)
            run_dir = Path(tmpdir) / "r1"
            run_dir.mkdir()

            record = RunRecord(
                run_id="r1", status="success", exit_code=0, duration_s=5.0,
            )
            orch._save_record(run_dir, record)

            loaded = orch._load_record("r1")
            assert loaded is not None
            assert loaded.run_id == "r1"
            assert loaded.status == "success"

    def test_save_record_with_trajectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)
            run_dir = Path(tmpdir) / "r2"
            run_dir.mkdir()

            traj_data = {"schema_version": "ATIF-v1.4", "steps": []}
            raw_stdout = json.dumps({
                "output": {"exit_code": 0},
                "atif_trajectory": traj_data,
            })
            record = RunRecord(
                run_id="r2", status="success", has_trajectory=True,
            )
            orch._save_record(run_dir, record, raw_stdout)

            traj_path = run_dir / "trajectory.jsonl"
            assert traj_path.exists()
            saved_traj = json.loads(traj_path.read_text().strip())
            assert saved_traj["schema_version"] == "ATIF-v1.4"


# ── summary ───────────────────────────────────────────────────────


class TestSummary:
    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            # Create some records
            for i, status in enumerate(["success", "success", "error"]):
                run_dir = Path(tmpdir) / f"r{i}"
                run_dir.mkdir()
                record = RunRecord(
                    run_id=f"r{i}", status=status,
                    duration_s=10.0, has_trajectory=(status == "success"),
                )
                (run_dir / "record.json").write_text(record.model_dump_json())

            s = orch.summary()
            assert s["total"] == 3
            assert s["success"] == 2
            assert s["errors"] == 1
            assert s["with_trajectory"] == 2
            assert s["total_duration_s"] == 30.0


# ── batch with checkpoint ─────────────────────────────────────────


class TestBatchCheckpoint:
    async def test_skip_completed(self):
        """Already-completed runs should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment = MockDeployment()
            orch = Orchestrator(deployment, output_dir=tmpdir)

            # Pre-create checkpoint and record
            orch._save_checkpoint({"done-1"})
            run_dir = Path(tmpdir) / "done-1"
            run_dir.mkdir()
            record = RunRecord(run_id="done-1", status="success", duration_s=5.0)
            (run_dir / "record.json").write_text(record.model_dump_json())

            configs = [
                _make_config(run_id="done-1"),
                _make_config(run_id="new-1"),
            ]

            # Mock run_single for new-1 only
            async def patched_run_single(config: RunConfig) -> RunRecord:
                if config.run_id == "done-1":
                    raise AssertionError("Should not run done-1")
                # Just return a fake record
                r = RunRecord(run_id=config.run_id, status="success", duration_s=1.0)
                rd = Path(tmpdir) / config.run_id
                rd.mkdir(exist_ok=True)
                (rd / "record.json").write_text(r.model_dump_json())
                (rd / "config.json").write_text(config.model_dump_json())
                return r

            orch.run_single = patched_run_single

            results = await orch.run_batch(configs)
            assert len(results) == 2
            assert {r.run_id for r in results} == {"done-1", "new-1"}
