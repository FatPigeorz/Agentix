"""`agentix deploy` — run a bundle image as a sandbox via a deployment backend.

Usage:

    agentix deploy local   --image my-agent:0.1.0
    agentix deploy local   --image my-agent:0.1.0 --base ubuntu:24.04 --detach
    agentix deploy daytona --image docker.io/me/my-agent:0.1.0    # stub
    agentix deploy e2b     --image docker.io/me/my-agent:0.1.0    # stub

`local` is the only backend fully wired today. `daytona` and `e2b` are
defined so the CLI surface stabilizes; calling them surfaces a clear
NotImplementedError pointing at the deploy roadmap.

By default the command stays in the foreground, prints the sandbox's
runtime URL, and tears the sandbox down on Ctrl-C. `--detach` exits
immediately after `create()` and prints the sandbox_id so the caller
can stop it later via the deployment API.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence

from agentix.deployment.base import Deployment, load_deployment, session
from agentix.models import SandboxConfig

logger = logging.getLogger("agentix.cli.deploy")

# Default base + runtime images. Override with `--base` / `--runtime`.
DEFAULT_BASE_IMAGE = "ubuntu:24.04"
DEFAULT_RUNTIME_IMAGE = "agentix/runtime:latest"


def _make_deployment(backend: str) -> Deployment:
    """Look up the deployment class via the `agentix.deployment` entry-point
    registry and instantiate it with no arguments. Backend-specific
    configuration (API keys, regions, etc.) is read from environment
    variables inside each backend's `__init__`.
    """
    try:
        cls = load_deployment(backend)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    return cls()


async def _run_async(backend: str, args: argparse.Namespace) -> int:
    deployment = _make_deployment(backend)
    config = SandboxConfig(
        image=args.base,
        runtime=args.runtime,
        namespaces=[args.image],
    )
    if args.detach:
        sandbox = await deployment.create(config)
        print(sandbox.sandbox_id)
        print(f"  runtime_url: {sandbox.runtime_url}")
        print(f"  status:      {sandbox.status}")
        print(f"# stop with `python -c \"...\" {sandbox.sandbox_id}` "
              f"(no `agentix stop` yet — TODO)")
        return 0

    # Foreground mode: stay alive until SIGINT, then tear down.
    print(f"creating sandbox from {args.image}…", file=sys.stderr)
    async with session(deployment, config) as sandbox:
        print(f"sandbox alive: {sandbox.sandbox_id}")
        print(f"  runtime_url: {sandbox.runtime_url}")
        print("  Ctrl-C to stop.")
        sys.stdout.flush()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            print("\ntearing down…", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix deploy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "backend",
        help="deployment backend name (any registered `agentix.deployment` "
             "entry point)",
    )
    parser.add_argument(
        "--image", required=True,
        help="namespace or bundle image tag (e.g. my-agent:0.1.0)",
    )
    parser.add_argument(
        "--base", default=DEFAULT_BASE_IMAGE,
        help=f"base task image (default: {DEFAULT_BASE_IMAGE})",
    )
    parser.add_argument(
        "--runtime", default=DEFAULT_RUNTIME_IMAGE,
        help=f"runtime image ref (default: {DEFAULT_RUNTIME_IMAGE})",
    )
    parser.add_argument(
        "--detach", action="store_true",
        help="exit after create; sandbox keeps running",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run_async(args.backend, args))
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
