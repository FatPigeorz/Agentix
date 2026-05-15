"""`agentix plugins` — list installed plugins across every axis.

Walks every `agentix.<axis>` entry-point group the framework knows
about, plus the framework's own builtins, and prints a one-line
summary per plugin: name, fully-qualified target, source dist, load
status. Useful for "did my `pip install agentix-deployment-fly`
actually take?" — failing silently here is the worst case so we make
discovery visible.

The list of groups is intentionally hardcoded — these are the framework's
extension axes. Third-party axes (if a downstream framework builds on
top of agentix) can be added by editing this file.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from collections.abc import Sequence

# Known framework axes. Each entry: (group name, one-line description).
_KNOWN_GROUPS: tuple[tuple[str, str], ...] = (
    ("agentix.closure", "closures — runtime-mounted Namespace classes"),
    ("agentix.deployment", "deployment backends — sandbox lifecycle"),
    ("agentix.trace_sink", "trace sinks — fan-out trace event consumers"),
    ("agentix.spec_resolver", "spec resolvers — agentix build/install input lookups"),
    ("agentix.wire_pattern", "wire patterns — call shape extensions"),
    ("agentix.cli", "CLI subcommands — agentix <name> entry points"),
)


def _iter_entry_points(group: str):
    eps = importlib.metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=group))
    return list(eps.get(group, []))  # type: ignore[attr-defined]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix plugins",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--group", default=None,
        help="show only one axis (e.g. agentix.deployment)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="show entry-point load errors verbatim",
    )
    args = parser.parse_args(argv)

    groups = (
        [(args.group, "(filtered)")]
        if args.group
        else list(_KNOWN_GROUPS)
    )

    any_errors = False
    for group, description in groups:
        eps = _iter_entry_points(group)
        if not eps:
            print(f"{group}  (no plugins installed)")
            print(f"  → {description}\n")
            continue
        print(f"{group}")
        print(f"  → {description}")
        for ep in eps:
            dist = ep.dist
            dist_str = f"{dist.name}@{dist.version}" if dist else "(unknown dist)"
            # Touch ep.load() so failures surface here.
            try:
                ep.load()
                status = "ok"
            except Exception as exc:
                status = f"FAIL: {type(exc).__name__}: {exc}"
                any_errors = True
            print(f"  {ep.name:20s} → {ep.value:50s} [{dist_str}] {status}")
            if args.verbose and status.startswith("FAIL"):
                import traceback
                traceback.print_exc(file=sys.stderr)
        print()

    return 1 if any_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
