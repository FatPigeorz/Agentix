"""`agentix check` — list installed closures and smoke-import them.

With entry-point-only discovery, stub↔impl drift can't happen — there's
only one class, signature and body share the same source line. The
useful thing the check still does is *exercise* the discovery:

  * walk `importlib.metadata.entry_points(group="agentix.closure")`
  * `ep.load()` each — fails fast if the closure's module imports break
  * print one line per closure so the user can see what's installed

Run as `agentix check`. Non-zero exit on any load failure.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agentix.dispatch import discover_entry_points
from agentix.namespace import Namespace


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)

    eps = discover_entry_points()
    if not eps:
        print("no closures found (no `agentix.closure` entry points installed)")
        return 0

    failures = 0
    for ep in eps:
        try:
            cls = ep.load()
        except Exception as exc:
            print(f"FAIL {ep.value}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if not isinstance(cls, type) or Namespace not in cls.__mro__:
            print(
                f"FAIL {ep.value}: {cls!r} is not a Namespace subclass",
                file=sys.stderr,
            )
            failures += 1
            continue
        dist = ep.dist
        dist_str = f"{dist.name}@{dist.version}" if dist else "(unknown dist)"
        print(f"OK   {ep.name:20s} {ep.value:40s} {dist_str}")

    if failures:
        print(f"\n{failures} closure(s) failed to load", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
