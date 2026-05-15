# Writing Agentix plugins

Agentix is extensible along six axes. Each axis is a Python entry-point
group; every plugin is a normal pip-installable distribution. To
contribute a plugin you write two things:

1. A class (or callable) that implements the axis's `Protocol`.
2. One `[project.entry-points."agentix.<axis>"]` block in your
   `pyproject.toml`.

`pip install your-plugin` makes the plugin live to every Agentix
installation in the same environment. No framework patch, no config
file, no decorator at import time. `agentix plugins` lists what's
installed; `agentix plugins --verbose` shows tracebacks for anything
that failed to load.

The framework's own builtins ship via the same pattern, so every
example below has an in-tree precedent.

---

## Axis index

| Axis | Group | Semantics | Built-ins |
|---|---|---|---|
| [Closures](#closures) | `agentix.closure` | discover, lazy-load on first call | (third-party only) |
| [Deployments](#deployments) | `agentix.deployment` | select-one by name | `local` / `daytona` / `e2b` |
| [Trace sinks](#trace-sinks) | `agentix.trace_sink` | fan-out, every sink receives | (third-party only) |
| [Spec resolvers](#spec-resolvers) | `agentix.spec_resolver` | chain by priority, first claim wins | `path` / `image` / `local_repo` / `pypi` |
| [Wire patterns](#wire-patterns) | `agentix.wire_pattern` | first-match by signature, user-registered ahead of built-ins | `unary` / `stream` / `bidi` |
| [CLI subcommands](#cli-subcommands) | `agentix.cli` | merged into `agentix --help` | `build` / `install` / `deploy` / `check` / `plugins` |

---

## Closures

A closure is a class whose `@staticmethod` methods are the callable
surface. Methods carry the real implementation; the class is a namespace.

```python
# src/agentix/myagent/__init__.py
from agentix.namespace import Namespace

class MyAgent(Namespace):
    @staticmethod
    async def run(instruction: str) -> str:
        ...
```

```toml
# pyproject.toml
[project]
name = "agentix-myagent"
version = "0.1.0"

[project.entry-points."agentix.closure"]
myagent = "agentix.myagent:MyAgent"

[tool.hatch.build.targets.wheel]
packages = ["src/agentix"]
```

After `pip install`, callers do
`from agentix.myagent import MyAgent` and use
`await c.remote(MyAgent.run, instruction=...)`. See
`primitives/bash` and `primitives/files` for working examples.

## Deployments

A deployment manages sandbox lifecycle. Structural type — implement
the three methods, no inheritance.

```python
# my_deploy/__init__.py
from agentix.deployment.base import Sandbox
from agentix.idents import SandboxId
from agentix.models import SandboxConfig, SandboxInfo

class FlyDeployment:
    def __init__(self) -> None:
        # Backends take NO constructor arguments. Read config from env.
        import os
        self._token = os.environ.get("FLY_API_TOKEN")

    async def create(self, config: SandboxConfig) -> Sandbox: ...
    async def delete(self, sandbox_id: SandboxId) -> None: ...
    async def get(self, sandbox_id: SandboxId) -> SandboxInfo: ...
```

```toml
[project.entry-points."agentix.deployment"]
fly = "my_deploy:FlyDeployment"
```

After install, `agentix deploy fly --image my-agent:0.1.0` works.
Conflicts (two dists registering the same name) raise
`PluginConflictError` with both dist labels.

## Trace sinks

Trace sinks are *installers* — small callables that register one or
more sink functions at runtime startup.

```python
# my_otel_sink/__init__.py
import os
from opentelemetry import trace as otel
from agentix.trace import register_sink

def install() -> None:
    tracer = otel.get_tracer("agentix")

    def sink(kind: str, payload: dict, call_id, source):
        with tracer.start_as_current_span(f"agentix.{kind}") as span:
            span.set_attribute("agentix.kind", kind)
            if call_id:
                span.set_attribute("agentix.call_id", call_id)
            if source:
                span.set_attribute("agentix.source", source)
            for k, v in payload.items():
                span.set_attribute(f"agentix.payload.{k}", v)

    register_sink(sink)
```

```toml
[project.entry-points."agentix.trace_sink"]
otel = "my_otel_sink:install"
```

`install()` runs once at lifespan startup; the sink it registers
receives every event from every closure for the rest of the runtime's
life. Sink errors are logged and swallowed — tracing never breaks a
rollout.

## Spec resolvers

Spec resolvers map CLI strings (what users type after `agentix build`)
to `ClosureSpec` objects. Resolvers are tried in priority desc order;
first non-`None` answer wins.

```python
# my_github_resolver/__init__.py
from agentix.cli._resolve import ClosureSpec

class GithubResolver:
    priority = 30  # tried before LocalRepoResolver(50) only if you bump it

    def resolve(self, spec: str) -> ClosureSpec | None:
        if not spec.startswith("github:"):
            return None
        org_repo = spec[len("github:"):]
        return ClosureSpec(
            short=org_repo.split("/")[-1],
            kind="pypi",
            pypi_dist=f"agentix-{org_repo.replace('/', '-')}",
        )
```

```toml
[project.entry-points."agentix.spec_resolver"]
github = "my_github_resolver:GithubResolver"
```

After install, `agentix build github:my-org/my-closure` runs through
your resolver. Built-in resolvers (`path` p=100, `image` p=90,
`local_repo` p=50, `pypi` p=10) handle every default case.

## Wire patterns

A wire pattern owns the framing for one signature shape (unary,
server-streaming, bidi, …). Implement the `WirePattern` ABC and
register the class.

```python
# my_pubsub_pattern/__init__.py
import inspect
from agentix.wire import WirePattern

class PubSubPattern(WirePattern):
    name = "pubsub"

    @classmethod
    def matches(cls, sig: inspect.Signature) -> bool:
        # detect your marker type on the return annotation
        ...

    def bind(self, sig: inspect.Signature) -> None: ...
    def client_invoke(self, client, fn, sig, args, kwargs): ...
```

```toml
[project.entry-points."agentix.wire_pattern"]
pubsub = "my_pubsub_pattern:PubSubPattern"
```

Entry-point patterns come ahead of the three built-ins (`unary`,
`stream`, `bidi`) in `select_pattern`. In-process
`register_pattern(...)` calls override entry-point patterns of the
same name — useful for tests.

## CLI subcommands

Every `agentix.cli` entry point becomes an `agentix <name>`
subcommand. The entry-point target is a `main(argv: list[str]) -> int`
callable.

```python
# my_agentix_extra/__init__.py
import argparse

def main(argv: list[str]) -> int:
    """`agentix extra` — do extra-cool things."""
    parser = argparse.ArgumentParser(prog="agentix extra")
    parser.add_argument("thing")
    args = parser.parse_args(argv)
    print(f"doing extra cool stuff with {args.thing}")
    return 0
```

```toml
[project.entry-points."agentix.cli"]
extra = "my_agentix_extra:main"
```

After install, `agentix extra widget` works. `agentix --help`
discovers the new command via the entry-point group and includes it in
the list. The subcommand's own `--help` is what `argparse` produces
inside `main()`.

---

## Testing your plugin

Production discovery happens via `importlib.metadata`. For unit tests,
every axis exposes an in-process `register_*` helper that the
framework's own tests use:

| Axis | Imperative helper |
|---|---|
| Deployments | `agentix.deployment.base.register_deployment(name, cls)` |
| Trace sinks | `agentix.trace.register_sink(fn)` |
| Spec resolvers | `agentix.cli._resolve.register_spec_resolver(name, cls)` |
| Wire patterns | `agentix.wire.register_pattern(cls)` |

These bypass entry-point discovery so tests don't have to actually
install distributions. `agentix._plugin.Registry[T].reset()` clears
any per-test state.

`agentix plugins` is the production health check: run it after `pip
install your-plugin` to confirm the framework discovered your entry
point. Use `--group agentix.<axis>` to filter; `--verbose` prints
tracebacks for plugins whose loader raised.
