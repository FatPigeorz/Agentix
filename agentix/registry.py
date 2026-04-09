"""Plugin discovery and registry.

Scans directories for agent plugins. Reads manifest.json if present,
otherwise detects runner.py.

CLI usage:
    python -m agentix.registry list --search-dir ./agents
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PluginInfo:
    name: str
    path: Path  # directory containing the plugin
    entry: str = "runner.py"
    version: str | None = None
    description: str | None = None


def discover(search_dirs: list[Path]) -> list[PluginInfo]:
    """Scan directories for agent plugins.

    For each subdirectory, reads manifest.json if present. Otherwise falls back
    to detecting runner.py.
    """
    plugins: list[PluginInfo] = []
    for d in search_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if manifest_path.exists():
                m = json.loads(manifest_path.read_text())
                plugins.append(PluginInfo(
                    name=m["name"],
                    path=child,
                    entry=m.get("entry", "runner.py"),
                    version=m.get("version"),
                    description=m.get("description"),
                ))
            elif (child / "runner.py").exists():
                plugins.append(PluginInfo(
                    name=child.name, path=child,
                ))
    return plugins


def find(name: str, search_dirs: list[Path]) -> PluginInfo:
    """Find a specific plugin by name. Raises KeyError if not found."""
    for p in discover(search_dirs):
        if p.name == name:
            return p
    raise KeyError(f"Agent plugin '{name}' not found in {search_dirs}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agentix.registry",
        description="Discover and list agentix plugins.",
    )
    sub = parser.add_subparsers(dest="command")

    list_cmd = sub.add_parser("list", help="List discovered plugins")
    list_cmd.add_argument(
        "--search-dir",
        action="append",
        dest="search_dirs",
        type=Path,
        required=True,
        help="Directory to scan for plugins (can be repeated)",
    )
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        plugins = discover(args.search_dirs)
        for p in plugins:
            version = p.version or "-"
            desc = p.description or ""
            print(f"{p.name:<20} {version:<8} {desc}")


if __name__ == "__main__":
    main()
