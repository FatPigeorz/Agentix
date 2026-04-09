"""Example dataset: write a file, ask agent to read it, verify output."""

from pathlib import Path

async def setup(ctx: dict) -> dict:
    task_file = Path(ctx["workdir"]) / "task.txt"
    task_file.write_text("Say hello world")
    return {
        "instruction": "Read the file task.txt and print its contents exactly.",
        "api_key": ctx.get("api_key", "test-key"),
    }


async def verify(ctx: dict) -> dict:
    output = ctx.get("run_result", {}).get("stdout", "")
    passed = "hello world" in output.lower()
    return {
        "pass": passed,
        "output_length": len(output),
        "reason": "Output contains 'hello world'" if passed else "Expected 'hello world' in output",
    }
