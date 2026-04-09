"""Parse Claude Code stream-json output into ATIF Trajectory.

Claude Code CLI with `--output-format stream-json --verbose` emits one JSON
object per line. Key event types:

    system/init   — session metadata (tools, model, session_id)
    assistant     — agent messages (tool_use or text content blocks)
    user          — tool results (tool_result content blocks)
    result        — final summary (cost, usage, session_id)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agentix.trajectory import (
    AgentInfo,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)


def parse_stream_json(lines: list[str], agent_version: str = "unknown") -> Trajectory:
    """Parse Claude Code stream-json lines into an ATIF Trajectory.

    Args:
        lines: Raw lines from `claude -p ... --output-format stream-json --verbose`.
        agent_version: Version string for the agent (e.g. "2.1.96").

    Returns:
        Populated Trajectory with steps, metrics, and agent info.
    """
    session_id = ""
    model_name = ""
    steps: list[Step] = []
    step_id = 0
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cached = 0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id", session_id)
            model_name = event.get("model", model_name)

        elif event_type == "assistant":
            msg = event.get("message", {})
            content_blocks = msg.get("content", [])
            usage = msg.get("usage", {})
            msg_model = msg.get("model", model_name)

            # Skip synthetic messages (model="<synthetic>")
            if msg_model == "<synthetic>":
                continue

            prompt_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
            cached_tokens = usage.get("cache_read_input_tokens", 0)

            # Extract tool calls and text from content blocks
            tool_calls: list[ToolCall] = []
            text_parts: list[str] = []

            for block in content_blocks:
                if block.get("type") == "tool_use":
                    tool_calls.append(ToolCall(
                        tool_call_id=block.get("id", ""),
                        function_name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    ))
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            message_text = "\n".join(text_parts) if text_parts else ""

            # Estimate cost from usage (Claude pricing approximation)
            cost = _estimate_cost(usage, msg_model)

            steps.append(Step(
                step_id=step_id,
                timestamp=datetime.now(UTC).isoformat(),
                source="agent",
                message=message_text,
                model_name=msg_model if msg_model != model_name else None,
                tool_calls=tool_calls if tool_calls else None,
                metrics=Metrics(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                    cost_usd=cost,
                ),
            ))
            step_id += 1

        elif event_type == "user":
            msg = event.get("message", {})
            content_blocks = msg.get("content", [])

            # Collect tool results as observations
            results: list[ObservationResult] = []
            text_parts: list[str] = []

            for block in content_blocks:
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "") for b in content if b.get("type") == "text"
                        )
                    results.append(ObservationResult(
                        source_call_id=block.get("tool_use_id", ""),
                        content=str(content)[:2000],  # truncate large outputs
                    ))
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            observation = Observation(results=results) if results else None
            message_text = "\n".join(text_parts) if text_parts else ""

            # Only add step if there's actual content
            if observation or message_text:
                steps.append(Step(
                    step_id=step_id,
                    timestamp=datetime.now(UTC).isoformat(),
                    source="user",
                    message=message_text,
                    observation=observation,
                ))
                step_id += 1

        elif event_type == "result":
            session_id = event.get("session_id", session_id)
            total_cost = event.get("total_cost_usd", total_cost)
            usage = event.get("usage", {})
            total_input = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            total_output = usage.get("output_tokens", 0)
            total_cached = usage.get("cache_read_input_tokens", 0)

    # Build final trajectory
    trajectory = Trajectory(
        session_id=session_id or "unknown",
        agent=AgentInfo(
            name="claude-code",
            version=agent_version,
            model_name=model_name or "unknown",
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_input,
            total_completion_tokens=total_output,
            total_cached_tokens=total_cached,
            total_cost_usd=total_cost,
            total_steps=len(steps),
        ),
    )

    return trajectory


def _estimate_cost(usage: dict, model: str) -> float:
    """Rough cost estimate from usage dict. Uses result-level total_cost when available."""
    # Cost estimation is approximate; the authoritative cost comes from
    # the final "result" event's total_cost_usd field.
    input_tokens = usage.get("input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Default to Sonnet pricing (per million tokens)
    input_rate = 3.0 / 1_000_000
    output_rate = 15.0 / 1_000_000
    cache_write_rate = 3.75 / 1_000_000
    cache_read_rate = 0.30 / 1_000_000

    if "opus" in model:
        input_rate = 15.0 / 1_000_000
        output_rate = 75.0 / 1_000_000
        cache_write_rate = 18.75 / 1_000_000
        cache_read_rate = 1.50 / 1_000_000
    elif "haiku" in model:
        input_rate = 0.80 / 1_000_000
        output_rate = 4.0 / 1_000_000
        cache_write_rate = 1.0 / 1_000_000
        cache_read_rate = 0.08 / 1_000_000

    return (
        input_tokens * input_rate
        + cache_creation * cache_write_rate
        + cache_read * cache_read_rate
        + output_tokens * output_rate
    )
