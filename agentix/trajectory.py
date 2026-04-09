"""ATIF: Agent Trajectory Interchange Format (v1.4)

Standardized format for logging agent interaction histories.
Compatible with Harbor's ATIF spec. Useful for RL training,
evaluation analysis, and agent debugging.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    name: str
    version: str
    model_name: str
    extra: dict | None = None


class ToolCall(BaseModel):
    tool_call_id: str
    function_name: str
    arguments: dict


class ObservationResult(BaseModel):
    source_call_id: str
    content: str


class Observation(BaseModel):
    results: list[ObservationResult]


class Metrics(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int | None = None
    cost_usd: float = 0.0
    logprobs: list[float] | None = None
    completion_token_ids: list[int] | None = None
    prompt_token_ids: list[int] | None = None


class Step(BaseModel):
    step_id: int
    timestamp: str
    source: str = Field(description="user | agent | system")
    message: str
    reasoning_content: str | None = None
    model_name: str | None = None
    tool_calls: list[ToolCall] | None = None
    observation: Observation | None = None
    metrics: Metrics | None = None
    extra: dict | None = None


class FinalMetrics(BaseModel):
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    total_cost_usd: float = 0.0
    total_steps: int = 0


class Trajectory(BaseModel):
    schema_version: str = "ATIF-v1.4"
    session_id: str
    agent: AgentInfo
    steps: list[Step] = Field(default_factory=list)
    final_metrics: FinalMetrics = Field(default_factory=FinalMetrics)
    extra: dict | None = None

    def add_step(self, step: Step) -> None:
        self.steps.append(step)
        if step.metrics:
            self.final_metrics.total_prompt_tokens += step.metrics.prompt_tokens
            self.final_metrics.total_completion_tokens += step.metrics.completion_tokens
            self.final_metrics.total_cached_tokens += step.metrics.cached_tokens or 0
            self.final_metrics.total_cost_usd += step.metrics.cost_usd
        self.final_metrics.total_steps = len(self.steps)

    # ── Serialization ────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Save trajectory as a single-line JSON (JSONL-compatible)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Trajectory:
        """Load trajectory from a JSON file."""
        p = Path(path)
        return cls.model_validate_json(p.read_text(encoding="utf-8").strip())

    # ── Validation ───────────────────────────────────────────────

    def validate_trajectory(self) -> list[str]:
        """Check trajectory integrity. Returns list of issues (empty = valid)."""
        issues: list[str] = []

        if not self.session_id:
            issues.append("missing session_id")
        if not self.agent.name:
            issues.append("missing agent.name")
        if not self.agent.version:
            issues.append("missing agent.version")
        if not self.agent.model_name:
            issues.append("missing agent.model_name")

        # Step ordering
        for i, step in enumerate(self.steps):
            if step.step_id != i:
                issues.append(f"step[{i}].step_id={step.step_id}, expected {i}")
            if not step.source:
                issues.append(f"step[{i}] missing source")
            if step.source not in ("user", "agent", "system"):
                issues.append(f"step[{i}].source={step.source!r}, expected user|agent|system")

        # Metrics consistency
        expected_prompt = sum(s.metrics.prompt_tokens for s in self.steps if s.metrics)
        expected_completion = sum(s.metrics.completion_tokens for s in self.steps if s.metrics)
        expected_cost = sum(s.metrics.cost_usd for s in self.steps if s.metrics)

        if self.final_metrics.total_prompt_tokens != expected_prompt:
            issues.append(
                f"final_metrics.total_prompt_tokens={self.final_metrics.total_prompt_tokens}, "
                f"sum of steps={expected_prompt}"
            )
        if self.final_metrics.total_completion_tokens != expected_completion:
            issues.append(
                "final_metrics.total_completion_tokens="
                f"{self.final_metrics.total_completion_tokens}, "
                f"sum of steps={expected_completion}"
            )
        if abs(self.final_metrics.total_cost_usd - expected_cost) > 1e-6:
            issues.append(
                f"final_metrics.total_cost_usd={self.final_metrics.total_cost_usd}, "
                f"sum of steps={expected_cost}"
            )
        if self.final_metrics.total_steps != len(self.steps):
            issues.append(
                f"final_metrics.total_steps={self.final_metrics.total_steps}, "
                f"actual={len(self.steps)}"
            )

        return issues
