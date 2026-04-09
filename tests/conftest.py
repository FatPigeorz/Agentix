"""Shared fixtures for agentix tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def dummy_agent(tmp_path: Path) -> Path:
    """Create a minimal agent with async def run(ctx) -> dict."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "runner.py").write_text(textwrap.dedent("""\
        async def run(ctx: dict) -> dict:
            return {"answer": 42}
    """))
    return agent_dir
