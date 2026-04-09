"""Tests for agentix.trajectory — ATIF trajectory, serialization, validation."""

import tempfile
from pathlib import Path

from agentix.trajectory import (
    AgentInfo,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)


def _make_trajectory(n_steps: int = 3) -> Trajectory:
    """Helper to build a valid trajectory with n_steps."""
    traj = Trajectory(
        session_id="test-session-001",
        agent=AgentInfo(name="test-agent", version="1.0.0", model_name="test-model"),
    )
    for i in range(n_steps):
        source = "agent" if i % 2 == 0 else "user"
        step = Step(
            step_id=i,
            timestamp=f"2026-01-01T00:00:{i:02d}Z",
            source=source,
            message=f"step {i}",
            metrics=Metrics(
                prompt_tokens=100,
                completion_tokens=50,
                cached_tokens=10,
                cost_usd=0.01,
            ) if source == "agent" else None,
        )
        traj.add_step(step)
    return traj


class TestAddStep:
    def test_step_appended(self):
        traj = _make_trajectory(0)
        step = Step(step_id=0, timestamp="t", source="agent", message="hi")
        traj.add_step(step)
        assert len(traj.steps) == 1

    def test_metrics_accumulated(self):
        traj = _make_trajectory(3)
        # Steps 0, 2 are agent (have metrics), step 1 is user (no metrics)
        assert traj.final_metrics.total_prompt_tokens == 200
        assert traj.final_metrics.total_completion_tokens == 100
        assert traj.final_metrics.total_cached_tokens == 20
        assert abs(traj.final_metrics.total_cost_usd - 0.02) < 1e-9
        assert traj.final_metrics.total_steps == 3


class TestToolCallsAndObservations:
    def test_step_with_tool_calls(self):
        step = Step(
            step_id=0,
            timestamp="t",
            source="agent",
            message="reading file",
            tool_calls=[
                ToolCall(tool_call_id="tc1", function_name="Read", arguments={"path": "/a.py"}),
            ],
        )
        assert step.tool_calls[0].function_name == "Read"

    def test_step_with_observation(self):
        step = Step(
            step_id=1,
            timestamp="t",
            source="user",
            message="",
            observation=Observation(
                results=[ObservationResult(source_call_id="tc1", content="file contents")]
            ),
        )
        assert step.observation.results[0].content == "file contents"


class TestSerialization:
    def test_save_and_load(self):
        traj = _make_trajectory(2)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "traj.jsonl"
            traj.save(path)

            loaded = Trajectory.load(path)
            assert loaded.session_id == traj.session_id
            assert len(loaded.steps) == 2
            assert loaded.agent.name == "test-agent"
            assert loaded.final_metrics.total_steps == 2

    def test_save_creates_parent_dirs(self):
        traj = _make_trajectory(1)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "traj.jsonl"
            traj.save(path)
            assert path.exists()

    def test_roundtrip_preserves_data(self):
        traj = _make_trajectory(3)
        traj.steps[0].tool_calls = [
            ToolCall(tool_call_id="tc1", function_name="Bash", arguments={"command": "ls"}),
        ]
        traj.steps[1].observation = Observation(
            results=[ObservationResult(source_call_id="tc1", content="output")]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "traj.jsonl"
            traj.save(path)
            loaded = Trajectory.load(path)

            assert loaded.steps[0].tool_calls[0].function_name == "Bash"
            assert loaded.steps[1].observation.results[0].content == "output"


class TestValidation:
    def test_valid_trajectory_no_issues(self):
        traj = _make_trajectory(3)
        issues = traj.validate_trajectory()
        assert issues == []

    def test_missing_session_id(self):
        traj = _make_trajectory(1)
        traj.session_id = ""
        issues = traj.validate_trajectory()
        assert any("session_id" in i for i in issues)

    def test_missing_agent_name(self):
        traj = _make_trajectory(1)
        traj.agent.name = ""
        issues = traj.validate_trajectory()
        assert any("agent.name" in i for i in issues)

    def test_wrong_step_order(self):
        traj = _make_trajectory(2)
        traj.steps[1].step_id = 5  # should be 1
        issues = traj.validate_trajectory()
        assert any("step_id=5" in i for i in issues)

    def test_bad_source(self):
        traj = _make_trajectory(1)
        traj.steps[0].source = "invalid"
        issues = traj.validate_trajectory()
        assert any("invalid" in i for i in issues)

    def test_metrics_mismatch(self):
        traj = _make_trajectory(2)
        traj.final_metrics.total_prompt_tokens = 999
        issues = traj.validate_trajectory()
        assert any("total_prompt_tokens" in i for i in issues)

    def test_step_count_mismatch(self):
        traj = _make_trajectory(2)
        traj.final_metrics.total_steps = 10
        issues = traj.validate_trajectory()
        assert any("total_steps" in i for i in issues)
