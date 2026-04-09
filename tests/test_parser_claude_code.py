"""Tests for agentix.parsers.claude_code — stream-json to ATIF parsing."""

import json

from agentix.parsers.claude_code import parse_stream_json


def _make_stream_lines(
    *,
    session_id: str = "sess-001",
    model: str = "claude-sonnet-4-6",
    tool_name: str = "Read",
    tool_input: dict | None = None,
    tool_result_content: str = "file contents",
    final_text: str = "The answer is 42.",
    total_cost: float = 0.05,
) -> list[str]:
    """Build a realistic stream-json sequence."""
    if tool_input is None:
        tool_input = {"file_path": "/app/main.py"}

    events = [
        # init
        json.dumps({
            "type": "system", "subtype": "init",
            "session_id": session_id,
            "model": model,
            "tools": ["Bash", "Read", "Edit"],
        }),
        # assistant: tool call
        json.dumps({
            "type": "assistant",
            "message": {
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_001",
                        "name": tool_name,
                        "input": tool_input,
                    }
                ],
                "usage": {
                    "input_tokens": 500,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 30,
                },
            },
        }),
        # user: tool result
        json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_001",
                        "content": tool_result_content,
                    }
                ],
            },
        }),
        # assistant: final text
        json.dumps({
            "type": "assistant",
            "message": {
                "model": model,
                "content": [{"type": "text", "text": final_text}],
                "usage": {
                    "input_tokens": 800,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 200,
                    "output_tokens": 50,
                },
            },
        }),
        # result
        json.dumps({
            "type": "result",
            "subtype": "success",
            "session_id": session_id,
            "total_cost_usd": total_cost,
            "usage": {
                "input_tokens": 1300,
                "cache_read_input_tokens": 300,
                "output_tokens": 80,
            },
        }),
    ]
    return events


class TestParseStreamJson:
    def test_basic_parsing(self):
        lines = _make_stream_lines()
        traj = parse_stream_json(lines, agent_version="2.1.96")

        assert traj.schema_version == "ATIF-v1.4"
        assert traj.session_id == "sess-001"
        assert traj.agent.name == "claude-code"
        assert traj.agent.version == "2.1.96"
        assert traj.agent.model_name == "claude-sonnet-4-6"

    def test_steps_structure(self):
        lines = _make_stream_lines()
        traj = parse_stream_json(lines)

        # 3 steps: agent (tool call), user (tool result), agent (text)
        assert len(traj.steps) == 3

        # Step 0: agent with tool call
        assert traj.steps[0].source == "agent"
        assert traj.steps[0].tool_calls is not None
        assert traj.steps[0].tool_calls[0].function_name == "Read"
        assert traj.steps[0].tool_calls[0].tool_call_id == "toolu_001"

        # Step 1: user with observation
        assert traj.steps[1].source == "user"
        assert traj.steps[1].observation is not None
        assert traj.steps[1].observation.results[0].source_call_id == "toolu_001"
        assert "file contents" in traj.steps[1].observation.results[0].content

        # Step 2: agent with text
        assert traj.steps[2].source == "agent"
        assert "42" in traj.steps[2].message

    def test_final_metrics(self):
        lines = _make_stream_lines(total_cost=0.123)
        traj = parse_stream_json(lines)

        assert traj.final_metrics.total_cost_usd == 0.123
        assert traj.final_metrics.total_steps == 3
        assert traj.final_metrics.total_prompt_tokens > 0
        assert traj.final_metrics.total_completion_tokens > 0

    def test_step_ids_sequential(self):
        lines = _make_stream_lines()
        traj = parse_stream_json(lines)

        for i, step in enumerate(traj.steps):
            assert step.step_id == i

    def test_skips_synthetic_messages(self):
        lines = [
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": "s1", "model": "test",
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "not real"}],
                    "usage": {},
                },
            }),
            json.dumps({
                "type": "result", "session_id": "s1",
                "total_cost_usd": 0, "usage": {},
            }),
        ]
        traj = parse_stream_json(lines)
        assert len(traj.steps) == 0

    def test_handles_empty_lines(self):
        lines = ["", "  ", *_make_stream_lines(), "", ""]
        traj = parse_stream_json(lines)
        assert len(traj.steps) == 3

    def test_handles_malformed_json(self):
        lines = ["not json", *_make_stream_lines(), "{broken"]
        traj = parse_stream_json(lines)
        assert len(traj.steps) == 3

    def test_multi_tool_calls_in_one_message(self):
        lines = [
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": "s1", "model": "test",
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "test",
                    "content": [
                        {"type": "tool_use", "id": "tc1", "name": "Read",
                         "input": {"path": "a.py"}},
                        {"type": "tool_use", "id": "tc2", "name": "Bash",
                         "input": {"command": "ls"}},
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            }),
            json.dumps({
                "type": "result", "session_id": "s1",
                "total_cost_usd": 0.01, "usage": {"input_tokens": 100, "output_tokens": 20},
            }),
        ]
        traj = parse_stream_json(lines)
        assert len(traj.steps) == 1
        assert len(traj.steps[0].tool_calls) == 2
        assert traj.steps[0].tool_calls[0].function_name == "Read"
        assert traj.steps[0].tool_calls[1].function_name == "Bash"
