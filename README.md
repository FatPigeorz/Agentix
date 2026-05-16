<div align="center">

# Agentix

**Typed Python namespaces for sandbox-based agent workflows.**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

</div>

## What it is

A small framework that lets you compose agent / dataset / primitive code into a sandbox and call it from your trainer or harness as if it were local typed Python:

```python
from agentix import RuntimeClient
from agentix.bash import Bash
from agentix.claude_code import ClaudeCode      # pip install agentix-claude-code
from agentix.swebench import SWEBench           # pip install agentix-swebench

async with RuntimeClient(sandbox_url) as c:
    task   = await c.remote(SWEBench.get_task, idx=42)
    patch  = await c.remote(ClaudeCode.run, instruction=task.problem)
    reward = await c.remote(SWEBench.score, idx=42, patch=patch)
```

Every extension is a normal pip-installable distribution. There is no custom config file, no decorator at import time, no per-framework registry call: the user installs a wheel and the framework discovers it via Python entry points.

Two plugin axes — `agentix.namespace` for things that run *inside* the sandbox, and `agentix.deployment` for backends that decide *where* the sandbox runs. Everything else (trace sinks, wire patterns, spec resolvers) is plain Python you import and call (see "Extending Agentix" below).

## Install

```bash
pip install agentix
# Plus whichever namespaces you actually need:
pip install agentix-bash agentix-files
pip install agentix-claude-code agentix-swebench   # examples — not yet on PyPI
```

For local development of the framework itself:

```bash
git clone https://github.com/Agentiix/Agentix.git
cd Agentix
pip install -e '.[dev]'
pip install -e primitives/bash -e primitives/files   # the bundled primitives
```

## CLI

```bash
agentix build primitives/bash                              # build a single namespace image
agentix build bash files claude-code -o my-agent:0.1.0     # bundle several namespaces
agentix deploy local --image my-agent:0.1.0                # run a sandbox + connect
agentix check                                              # smoke-import every installed namespace
```

The four subcommands are framework built-ins. Third parties that want their own `agentix-foo` verb should ship a separate `console_scripts` binary — the `agentix` dispatcher itself is not a plugin surface.

## Writing a namespace

A namespace is a class whose `@staticmethod` methods are the remote-callable surface. The class is a pure namespace — methods carry no `self`, no instance state.

```python
# src/agentix/myagent/__init__.py
from agentix.namespace import Namespace

class MyAgent(Namespace):
    @staticmethod
    async def run(instruction: str) -> str:
        ...
```

Ship it with one entry-point declaration:

```toml
# pyproject.toml
[project]
name = "agentix-myagent"
version = "0.1.0"

[project.entry-points."agentix.namespace"]
myagent = "agentix.myagent:MyAgent"

[tool.hatch.build.targets.wheel]
packages = ["src/agentix"]
```

`pip install agentix-myagent` is the entire setup. Caller-side:

```python
from agentix.myagent import MyAgent
result = await c.remote(MyAgent.run, instruction="...")
```

The framework's `agentix/__init__.py` extends `__path__` so `agentix.<your-namespace>` resolves natively; PEP 420 namespace packages mean multiple dists can install peer entries under `agentix/` without colliding. Reserved framework subpackages (`agentix.cli`, `agentix.dispatch`, `agentix.deployment`, …) are listed in [CLAUDE.md](CLAUDE.md).

## Extending Agentix

Two plugin axes — only the things that cross the host↔sandbox boundary deserve entry-point discovery:

| Axis | Entry-point group | What it ships | Built-ins |
|---|---|---|---|
| Namespaces | `agentix.namespace` | Python class whose code runs **inside the sandbox** | (third-party only) |
| Deployments | `agentix.deployment` | host-side backend that **provisions** the sandbox | `local` / `daytona` / `e2b` |

```toml
[project.entry-points."agentix.namespace"]
my-thing = "agentix.my_thing:MyThing"
```

> The quotes around the group name are TOML syntax — `agentix.namespace` contains a dot, and TOML treats dots in `[a.b.c]` as table-key separators. Quoting forces it to be a single key. Every framework with a dotted group name does this (`flask.commands`, `mkdocs.plugins`, `sphinx.builders`, …).

Everything else lives entirely on the host:

- **Trace sinks** — `agentix.trace.register_sink(fn)` to add a trace consumer (OTel, Sentry, custom bus).
- **Wire patterns** — three built-ins (`unary` / `stream` / `bidi`) cover every call shape; not user-extensible. Add a fourth by editing `agentix/wire.py` directly.
- **Spec resolvers** — internal ordered list in `agentix/cli/_resolve.py`; new spec shapes mean editing that file.
- **CLI verbs** — ship your own `agentix-yourcmd` `console_scripts` binary; the central CLI is not a plugin surface.

See [`docs/plugin-authors.mdx`](docs/plugin-authors.mdx) for the full plugin authors guide. Rendered site: [agentiix.github.io](https://agentiix.github.io/).

## Architecture

```
Orchestrator ──HTTP /_remote──► Runtime Server ──in-process call──► Namespace impl
                  (or)                            (Dispatcher)
            Socket.IO /socket.io/  ◄─── streams, bidi, logs, traces ───►
```

| Component | Role |
|---|---|
| Runtime server | `/health`, `/namespaces`, `/_remote` (unary), `/socket.io/` (streams/bidi/logs/traces), `/_llm/<provider>/<path>` (LLM-proxy fan-in) |
| Namespace | Python class registered under `agentix.namespace` entry point; methods called via `c.remote(...)` |
| Deployment | Sandbox CRUD plugin under `agentix.deployment`; `local` (Docker) is built in |
| WirePattern | Pluggable call-shape strategy — built-ins are unary / stream / bidi |
| Trace sink | Optional observability hook — receives every `trace.emit(...)` event |

Discovery is lazy: namespace `ep.load()` is deferred until the first `/_remote` call for that namespace; one broken namespace doesn't block sandbox boot. See [`docs/reference/architecture.mdx`](docs/reference/architecture.mdx) and [`docs/reference/namespace-protocol.mdx`](docs/reference/namespace-protocol.mdx) for protocol details.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## Contributing

See [docs/development.mdx](docs/development.mdx). Project conventions in [CLAUDE.md](CLAUDE.md) — read the "组合优于继承 / Composition over inheritance" section. Docs site is built with Mintlify; see [`docs/DEPLOY.md`](docs/DEPLOY.md) for the one-time GitHub Pages setup.

## License

[MIT License](LICENSE)
