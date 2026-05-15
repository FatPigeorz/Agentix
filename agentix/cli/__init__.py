"""`agentix` command-line interface.

A small argparse-based dispatcher for the developer-facing tools:

    agentix build   primitives/bash              # build one closure image
    agentix install bash files -o my-agent:0.1.0 # bundle several closures
    agentix deploy  local --image my-agent:0.1.0 # run a sandbox
    agentix check   primitives/                  # stub ↔ impl signature drift

Subcommands live in sibling modules so they can also be invoked
directly (`python -m agentix.cli.build …`). The CLI is intentionally
thin — most logic lives in those subcommand modules.

`tools/gen_manifest.py` is **not** exposed as a subcommand because it
runs inside the closure's nix build environment, which doesn't have
the `agentix` package available. Keep it standalone.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# Subcommand registry. Each entry pairs the user-typed name with a
# lazy resolver returning the subcommand's `main(argv)` callable. The
# resolver imports on demand so `agentix --help` doesn't pay for docker
# / pydantic schema generation it isn't going to use.
_COMMANDS: dict[str, tuple[str, callable]] = {
    "build":   ("build a single closure image",
                lambda: _import("agentix.cli.build").main),
    "install": ("bundle multiple closures into one image",
                lambda: _import("agentix.cli.install").main),
    "deploy":  ("deploy a bundle to a deployment backend",
                lambda: _import("agentix.cli.deploy").main),
    "check":   ("list installed closures + smoke-import each",
                lambda: _import("agentix.cli.check").main),
    "plugins": ("list installed plugins across every extension axis",
                lambda: _import("agentix.cli.plugins").main),
}


def _import(name: str):
    import importlib
    return importlib.import_module(name)


def _print_root_help() -> None:
    print("usage: agentix <command> [args...]\n")
    print("Agentix developer CLI\n")
    print("commands:")
    width = max(len(c) for c in _COMMANDS) + 2
    for cmd, (desc, _resolve) in _COMMANDS.items():
        print(f"  {cmd.ljust(width)}{desc}")
    print("\nRun `agentix <command> --help` for command-specific options.")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to one of the subcommand `main(argv)` functions.

    We deliberately *don't* use a single argparse subparser. argparse's
    `--help` is greedy: with subparsers, `agentix install --help` would
    be intercepted at the root level and never reach the install
    parser. Manual dispatch keeps each subcommand's `--help` intact.
    """
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_root_help()
        return 0
    cmd, *rest = argv
    entry = _COMMANDS.get(cmd)
    if entry is None:
        print(f"unknown command: {cmd!r}\n", file=sys.stderr)
        _print_root_help()
        return 2
    _desc, resolve = entry
    return resolve()(rest)


if __name__ == "__main__":
    sys.exit(main())
