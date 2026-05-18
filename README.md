<div align="center">

# Agentix

**Sandboxed rollouts you call like typed Python.**

Turn agents, tools, and scorers into Python callables. Package their
dependencies into runtime images. Call them from evaluators, trainers,
and orchestration code without writing a new runner for every pairing.

[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-agentiix.github.io-blue)](https://agentiix.github.io/)

[Documentation](https://agentiix.github.io/) | [Quickstart](https://agentiix.github.io/quickstart) | [Cookbook](https://github.com/Agentiix/agentix-cookbook) | [Architecture](https://agentiix.github.io/reference/architecture)

</div>

## The 10-Second Model

Agentix has two primitives:

- **Remote calls**: `client.remote(fn, *args, **kwargs)` runs a Python
  callable inside a sandbox worker. The callable is serialized with
  stdlib pickle, Python's native callable reference mechanism.
- **Bundles**: `agentix build [path]` packages a Python project and its
  declared dependencies into a deploy-ready runtime image.

```python
from agentix import RuntimeClient
from app import run

async with RuntimeClient(sandbox.runtime_url) as client:
    result = await client.remote(run, input="hello")
```

The unit of composition is not a bespoke benchmark runner or agent
adapter. It is a Python callable.

## Why Agentix Exists

Agent experiments sprawl quickly. One agent needs a CLI wrapper. Another
needs a Python harness. A benchmark needs repo setup, grading scripts,
and logs. A training loop needs the same pieces batched across many
sandboxes.

Agentix collapses that matrix into one execution contract: if Python can
serialize the callable and the sandbox has its dependencies, the host can
call it.

| You have | You expose | You call |
| --- | --- | --- |
| Claude Code, Codex, Aider, OpenHands, or an internal agent | `async def run(...) -> RunResult` | `await client.remote(run, ...)` |
| Shell, files, repo setup, or local tools | `async def run(command: str) -> BashResult` | `await client.remote(bash_run, ...)` |
| SWE-bench, MLE-Bench, or an internal evaluator | `async def score(...) -> Score` | `await client.remote(score, ...)` |
| Streaming or interactive workflows | `async def stream(...) -> AsyncIterator[Event]` | `async for event in client.remote(stream, ...)` |

## What Ships

- **Typed remote calls** across the host-to-sandbox boundary.
- **Unary, streaming, and bidirectional call shapes** inferred from
  callable signatures.
- **One runtime worker process today** behind an internal worker backend
  boundary, so future pools or per-call isolation can stay API-compatible.
- **Bundle builds** from normal Python projects and `pyproject.toml`
  dependencies.
- **Optional Nix system dependencies** when a project includes
  `default.nix`.
- **Deployment backend plugins** through the `agentix.deployment` entry
  point group.

## Quickstart

Install the host framework and a deployment backend:

```bash
pip install agentixx agentix-deployment-docker
```

Create a remote target:

```python
# src/hello_agentix/__init__.py
async def run(input: str) -> str:
    return f"sandbox saw: {input}"
```

Build a bundle:

```bash
agentix build ./hello-agentix -o hello-agentix:0.1.0
```

Deploy it and call the callable:

```python
import asyncio

from agentix import RuntimeClient
from agentix.deployment.base import SandboxConfig, session
from agentix.deployment.docker import DockerDeployment
from hello_agentix import run


async def main() -> None:
    deployment = DockerDeployment()
    config = SandboxConfig(image="hello-agentix:0.1.0")

    async with session(deployment, config) as sandbox:
        async with RuntimeClient(sandbox.runtime_url) as client:
            print(await client.remote(run, input="hello"))


asyncio.run(main())
```

Read the full [quickstart](https://agentiix.github.io/quickstart) for the
package layout and runtime-image prerequisites.

## Architecture

```text
Host process
  RuntimeClient.remote(fn, ...)
    serializes callable with pickle
    detects unary / stream / bidi
    encodes args and kwargs
        |
        v
Sandbox
  agentix-server
        |
        v
  worker subprocess
    unpickles callable
    validates args
    calls fn(*args, **kwargs)
```

Unary calls use HTTP `POST /_remote`. Streaming and bidirectional calls
use Socket.IO events. Errors stay in-band.

## Repository Map

- [`Agentix-Runtime-Basic`](https://github.com/Agentiix/Agentix-Runtime-Basic):
  sandbox primitives such as `bash` and file operations.
- [`Agentix-Deployment-Docker`](https://github.com/Agentiix/Agentix-Deployment-Docker):
  local Docker deployment backend.
- [`Agentix-Deployment-Daytona`](https://github.com/Agentiix/Agentix-Deployment-Daytona)
  and [`Agentix-Deployment-E2B`](https://github.com/Agentiix/Agentix-Deployment-E2B):
  hosted sandbox backend packages.
- [`agentix-cookbook`](https://github.com/Agentiix/agentix-cookbook):
  working integration recipes for agents and benchmarks.
- [`abridge`](https://github.com/Agentiix/abridge): rollout-to-RL-buffer
  bridge.

## Development

```bash
git clone https://github.com/Agentiix/Agentix
cd Agentix
pip install -e '.[dev]'
pytest
ruff check agentix/ tests/
```

Pair this repo with sibling backend/runtime repos checked out next to it
when testing full sandbox rollouts.

## Links

- [Docs](https://agentiix.github.io/)
- [Quickstart](https://agentiix.github.io/quickstart)
- [Remote calls](https://agentiix.github.io/concepts/remote-calls)
- [Bundles](https://agentiix.github.io/concepts/bundles)
- [Architecture](https://agentiix.github.io/reference/architecture)
- [Roadmap](ROADMAP.md)
