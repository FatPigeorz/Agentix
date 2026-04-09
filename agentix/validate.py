"""Validate plugins without running them (dry-run).

Usage:
    python -m agentix.validate --agent ./agents/claude-code
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path


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


def validate_plugin(path: Path) -> list[str]:
    """Validate an agent plugin without running it. Returns list of issues (empty = OK)."""
    issues: list[str] = []

    entry_path = path / "runner.py"
    if not entry_path.exists():
        return [f"Missing runner.py in {path}"]

    try:
        module = _load_module(entry_path, "agent")
    except PluginLoadError as e:
        return [str(e)]

    try:
        _validate_agent(module, entry_path)
    except PluginLoadError as e:
        issues.append(str(e))

    # Check manifest.json if present
    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text())
            if "name" not in m:
                issues.append("manifest.json missing 'name' field")
            if "kind" not in m:
                issues.append("manifest.json missing 'kind' field")
        except json.JSONDecodeError as e:
            issues.append(f"Invalid manifest.json: {e}")

    return issues


def _print_result(kind: str, path: Path, issues: list[str]) -> None:
    """Print OK or ERR line for a plugin."""
    name = path.resolve().name
    if not issues:
        print(f"OK  {kind}  {name}")
    else:
        for issue in issues:
            print(f"ERR {kind}  {name}  {issue}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate agentix agent plugins without running them",
    )
    parser.add_argument("--agent", type=Path, required=True,
                        help="Path to agent plugin directory")
    args = parser.parse_args()

    issues = validate_plugin(args.agent)
    _print_result("agent", args.agent, issues)
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
