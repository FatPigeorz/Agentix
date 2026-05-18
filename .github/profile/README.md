# Agentix

**Sandboxed rollouts you call like typed Python.**

Agentix turns agents, tools, and scorers into importable Python
functions that run inside isolated runtime containers. A trainer or
evaluator calls `await client.remote(fn, ...)`; Agentix handles the
sandbox boundary, argument validation, transport, and typed result.

The goal is simple: stop rebuilding the execution stack every time you
pair a new agent with a new benchmark or deployment backend.

## The Model

- **Remote calls** run an installed Python function inside a sandbox
  worker. The target is derived from the function object, not a hand-made
  RPC string.
- **Bundles** package a Python project and its dependencies into one
  deploy-ready image. Agent wrappers, scorers, primitives, and user code
  coexist in the same runtime venv.
- **Deployments** start that image locally or through a backend plugin
  and return a `runtime_url` for `RuntimeClient`.

```python
from agentix import RuntimeClient
from app import run

async with RuntimeClient(sandbox.runtime_url) as client:
    result = await client.remote(run, input="hello")
```

## What We Build

- [`Agentix`](https://github.com/Agentiix/Agentix): core remote-call
  runtime, build CLI, worker protocol, and deployment plugin interface
- [`Agentix-Runtime-Basic`](https://github.com/Agentiix/Agentix-Runtime-Basic):
  sandbox primitives such as shell and file operations
- [`Agentix-Deployment-Docker`](https://github.com/Agentiix/Agentix-Deployment-Docker):
  local Docker deployment backend
- [`Agentix-Deployment-Daytona`](https://github.com/Agentiix/Agentix-Deployment-Daytona)
  and [`Agentix-Deployment-E2B`](https://github.com/Agentiix/Agentix-Deployment-E2B):
  hosted sandbox backend packages
- [`agentix-cookbook`](https://github.com/Agentiix/agentix-cookbook):
  working recipes for agent and benchmark integrations

## Start Here

- [Documentation](https://agentiix.github.io/)
- [Quickstart](https://agentiix.github.io/quickstart)
- [Remote calls](https://agentiix.github.io/concepts/remote-calls)
- [Bundles](https://agentiix.github.io/concepts/bundles)
- [Cookbook](https://github.com/Agentiix/agentix-cookbook)
