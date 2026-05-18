# Project conventions

## Two concepts, no more

Agentix has exactly two ideas. Everything in the repo serves one of them:

1. **RPC** — `c.remote(fn, *args, **kwargs)` dispatches any importable
   Python function to a worker subprocess in a sandbox. Routing key is
   `fn.__module__`; the wire shape (unary / server-stream / bidi) is
   detected from `fn`'s signature; the return value is decoded into
   `fn`'s return type.
2. **Bundle** — `agentix build [path]` packages a Python project + its
   declared dependencies into a deploy-ready Docker image. The
   project's `[project].dependencies` is the bundle's plugin set; pip
   resolves transitively.

There is no "namespace" mechanism, no entry-point registration
required, no Stub/Impl ABI, no closures, no plugin axes beyond
**deployments** (and that's only because backends need a
name-to-class lookup for `agentix deploy <name>`). The framework
deliberately stops there.

## 组合优于继承 / Composition over inheritance

**Read this three times.**

1. Inheritance is reserved for genuine is-a relationships with a fixed
   lifecycle interface (`DockerDeployment` implementing `Deployment`'s
   abstract methods, say). Everywhere else, prefer composition — pass
   an instance, register a callback, declare a Protocol.
2. The dispatch target is just a Python module. Its async functions
   are the remote-callable methods. No base class to inherit from, no
   marker Protocol to import. `Dispatcher(target)` duck-types whatever
   you hand it.
3. When you reach for a base class to "share code" or "enforce a
   contract", stop. Ask: would a free function, a Protocol, or a
   composition object work instead? It usually does.

## No backward compatibility

This repo is in active design. **Breaking changes are fine; do not
introduce backward-compat shims.**

- **No aliases.** Rename `foo` → `bar`: delete `foo`, don't accept both.
- **No deprecation warnings.** Delete the thing.
- **No `// removed ...` / `// kept for compat` comments.** Git history covers that.
- **Tests:** update them to the new shape; don't keep a test that
  exercises removed behavior.

Downstream sibling repos (`Agentix-Runtime-Basic`, `Agentix-Deployment-*`,
`agentix-cookbook`) are updated in lockstep — assume they follow HEAD.

## Systems map

```
agentix/
├── idents.py            — branded NewType wire ids (CallId, PackageName, MethodName)
├── rpc.py               — Channel + Unary/Stream/Bidi variants (caller-side)
├── dispatch/            — server-side dispatch
│   ├── shape.py             — detect_shape (unary | stream | bidi)
│   ├── bound.py             — internal: _BoundMethod + arg coercion
│   └── dispatcher.py        — Dispatcher (lazy binds methods on first call)
├── runtime/             — host↔sandbox transport
│   ├── shared/              — wire types, codec, framing (used by both sides)
│   ├── client/              — RuntimeClient
│   └── server/              — FastAPI + Socket.IO + multiplexer + worker
├── deployment/          — Deployment Protocol + backend plugin loader
│   ├── base.py              — Sandbox + SandboxConfig + SandboxInfo + Deployment
│   └── _plugin.py           — Registry[T] for the `agentix.deployment` group
└── cli/                 — `agentix` command-line: build, deploy
```

One line per system:

- **idents** — branded `NewType` strings on the wire. Pure types, zero behavior.
- **rpc** — what `RuntimeClient.remote(fn, …)` returns. `Channel[T]`
  marks bidi input params.
- **dispatch** — `Dispatcher(target)` binds public functions of `target`
  lazily (TypeAdapter compile happens once per method, cached). The
  worker process owns one Dispatcher per spawned package.
- **runtime.shared** — wire bytes: msgpack codec, length-prefixed frame
  protocol, Socket.IO event-name constants, pydantic wire types. Both
  client and server import from here; neither imports from the other.
- **runtime.client** — `RuntimeClient`: one HTTP connection for unary,
  one Socket.IO connection multiplexing stream + bidi by `call_id`.
- **runtime.server** — sandbox-side. `agentix-server` (the Docker
  ENTRYPOINT) starts FastAPI + Socket.IO + the `NamespaceMultiplexer`.
  On first `/_remote` for a package, the multiplexer auto-probes
  whether the module is importable and spawns
  `python -m agentix.runtime.server.worker --target <pkg>`.
- **deployment** — `Deployment` Protocol with three methods
  (`create` / `delete` / `get`). Backends live in separate wheels
  (`agentix-deployment-docker`, `-daytona`, `-e2b`) registered via the
  `agentix.deployment` entry-point group.
- **cli** — `agentix build [path]` and `agentix deploy <backend>`. Two
  subcommands, hardcoded. Third-party verbs ship their own `console_scripts`.

## Ecosystem packages

Core `agentix` deliberately ships **no** backend implementations and
**no** sandbox primitives. Those live in sibling repos / wheels:

| Wheel | Repo |
|---|---|
| `agentix-runtime-basic` | [Agentix-Runtime-Basic](https://github.com/Agentiix/Agentix-Runtime-Basic) |
| `agentix-deployment-docker` | [Agentix-Deployment-Docker](https://github.com/Agentiix/Agentix-Deployment-Docker) |
| `agentix-deployment-daytona` | [Agentix-Deployment-Daytona](https://github.com/Agentiix/Agentix-Deployment-Daytona) |
| `agentix-deployment-e2b` | [Agentix-Deployment-E2B](https://github.com/Agentiix/Agentix-Deployment-E2B) |

A typical install:

```bash
pip install agentixx agentix-runtime-basic agentix-deployment-docker
```

## How dispatch works (the RPC mechanism)

`c.remote(fn, ...)` reads exactly two attributes of `fn`:

- `fn.__module__` → wire's `package` field (the routing key)
- `fn.__name__`   → wire's `method` field

Nothing else. **There is no entry-point declaration. There is no
`agentix.<short>` import-path requirement. Any importable Python
module is a valid dispatch target.** On first call to an unseen
package, the multiplexer probes the runtime's Python for whether
`<package>` imports there; if yes, it spawns a worker.

```python
# my_project/tasks.py
async def run(seed: int) -> dict:
    ...

# anywhere in caller code
from agentix import RuntimeClient
from my_project import tasks

async with RuntimeClient(sandbox.runtime_url) as c:
    result = await c.remote(tasks.run, seed=42)
```

`pip install -e .` (host-side) + `agentix build .` (sandbox image) is
the only ceremony.

### Plugin convention (optional, for reusable packages)

Plugins distributed via PyPI conventionally ship under `agentix.<short>`
(e.g. `agentix-runtime-basic` installs `agentix/bash/` and
`agentix/files/`) so consumers can `from agentix import bash` uniformly.
The convention is enabled by `pkgutil.extend_path` in
`agentix/__init__.py`. It's a style choice, not a framework
requirement — your own project can live at whatever module path you
prefer.

### Call shapes

Three shapes, detected from `fn`'s signature by
`agentix.dispatch.detect_shape`:

- `async def f(...) -> T`                                    → **unary**
- `async def f(...) -> AsyncIterator[T]: yield ...`          → **stream**
- `async def f(..., inbox: Channel[I]) -> AsyncIterator[T]`  → **bidi**

`c.remote(...)` returns a tagged variant matching the shape (`Unary[T]`,
`Stream[T]`, `Bidi[I, T]`). Await unary; `async for` over stream / bidi.

Sync functions work for unary too — the dispatcher checks
`isawaitable(result)` at runtime. Streams and bidi structurally need
async generators (`async for` can't iterate sync generators).

## How bundle works (the build mechanism)

`agentix build [path]` packages one project root into a deploy-ready
image. The CLI never enumerates plugins — they arrive transitively via
pip from your `[project].dependencies`.

```toml
# your-project/pyproject.toml
[project]
name = "my-agent"
version = "0.1.0"
dependencies = [
    "agentixx>=0.1.0",
    "agentix-runtime-basic>=0.1.0",     # bash + files
    "agentix-deployment-docker>=0.1.0", # the local backend
]
```

```bash
agentix build              # current dir's pyproject
agentix build path/to/proj # explicit
agentix build . -o my:dev  # explicit tag
agentix build . --dry-run  # stage Dockerfile, no docker invoke
```

Pipeline stages:

1. **Optional Nix stage** — if the project ships `default.nix`, a
   `nixos/nix` builder stage runs first; derivation closure is copied
   to `/export/nix/store`.
2. **Final image** — `FROM agentix/runtime:<version>`, then
   `COPY project/` and one `pip install /src/project` into
   `/nix/runtime/`. If a Nix derivation was built, `bin/*` symlinks
   into `/nix/runtime/bin/`.

The result: every plugin (including the user's own code) lives in one
shared `/nix/runtime/` venv. Workers all use `/nix/runtime/bin/python`
with `/nix/runtime/bin/` on PATH. Inline composition is regular Python
(`from agentix.bash import run` inside your worker just works).

The runtime base image (`agentix/runtime:<version>`) must exist locally.
Build it from `Agentix-Runtime-Basic/runtime/Dockerfile` or pull from a
registry.

## How deploy works

`agentix deploy <backend>` looks up `<backend>` in the
`agentix.deployment` entry-point group, instantiates the registered
class (which must satisfy the `Deployment` Protocol), creates a
sandbox via `await deployment.create(SandboxConfig(image=...))`, and
prints the sandbox's `runtime_url`. Foreground by default; `--detach`
exits after `create()`.

## Wire protocol

Two transports:

**Unary** — `POST /_remote` (HTTP):

```
POST /_remote
  msgpack({"package": "my_project.tasks",
           "method":  "run",
           "args":    [],
           "kwargs":  {"seed": 42},
           "call_id": null})

← msgpack({"ok": true, "value": {...}, "error": null})
```

Failures come back as `{"ok": false, "error": {...}}`. Wire stays 200.

**Server-streaming, bidirectional** — Socket.IO at `/socket.io/`.
One persistent Socket.IO connection per `RuntimeClient` multiplexes
all such calls, demultiplexed by a caller-generated `call_id`. Event
shapes:

```
stream            {call_id, package, method, args, kwargs}
stream:item       {call_id, value}
stream:end        {call_id}
stream:error      {call_id, error}

bidi:start        {call_id, package, method, args, kwargs}
bidi:in           {call_id, item}
bidi:end_in       {call_id}
bidi:out          {call_id, value}
bidi:end          {call_id}
bidi:error        {call_id, error}
```

## Typing conventions

**Branded identifiers from `agentix.idents`.** The wire's `str`s that
are easy to confuse — `PackageName`, `MethodName`, `CallId` — are
`NewType`d. Pyright treats them as distinct, so swapping one for
another becomes a type error. Pydantic v2 understands `NewType`, so
wire round-trip is unchanged. `SandboxId` lives next to
`Deployment` in `agentix.deployment.base` (deployment-side concern,
not wire).
