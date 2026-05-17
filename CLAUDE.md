# Project conventions

## 组合优于继承 / Composition over inheritance

**Read this three times. Say it out loud once.**

1. **组合优于继承.** This framework chooses composition over inheritance, everywhere it has the choice. Don't introduce inheritance to share behaviour, to mark relationships, or to give pyright a typing hook. Compose instead — pass an instance, register a callback, declare a Protocol.
2. **组合优于继承.** The namespace ABI is the canonical example. A namespace's stub class (`class Bash(Namespace)`) and its impl class (`class BashImpl`) are **independent classes that share no inheritance edge**. `_register.py` composes them by handing both to `Dispatcher.bind_namespace(Bash, BashImpl())`. `BashImpl` provides the `Bash` interface; it isn't a kind of `Bash`.
3. **组合优于继承.** When you reach for a base class to "share code" or "enforce a contract", stop. Ask: would a free function, a protocol, a wire-pattern strategy, or a deployment configuration object work instead? It almost always does. The cost of inheritance is that the parent and child are forever co-evolving; composition lets each piece change independently.

The reverse — using inheritance — is allowed only when the relationship is genuinely is-a and there's no composition alternative (e.g. `DockerDeployment` implements `Deployment`'s abstract methods because backends must satisfy a fixed lifecycle interface). Even then, prefer the smallest possible inheritance footprint.

## No backward compatibility

This repo is in active design. **Breaking changes are fine; do not introduce backward-compat shims.**

- **No aliases.** Rename `foo` → `bar`: delete `foo`, don't accept both.
- **No deprecation warnings.** Delete the thing.
- **No `// removed ...` / `// kept for compat` comments.** Git history covers that.
- **No version-bump fences.** Update code, docs, tests, move on.
- **Tests:** update them to the new shape; don't keep a test that exercises removed behavior.

Downstream repos (`Agentix-Agents-Hub`, `Agentix-Datasets`) are updated in lockstep — assume they follow HEAD.

## Systems map

The framework's code is organised around a handful of crisp systems. Each
one is a single subpackage; nothing important lives anywhere else.

```
agentix/
├── idents.py           — branded NewType ids (CallId, PackageName, MethodName, SandboxId)
├── models.py           — host-side pydantic models (SandboxConfig, NamespaceManifest, …)
├── namespace.py        — discover_methods: duck-typed walk of a namespace target
├── rpc.py              — Channel + Unary/Stream/Bidi variants (caller-side wire shapes)
├── trace.py            — in-process pub/sub for trace events; contextvar-pinned call_id
├── dispatch/           — server-side dispatch: binding stubs to impls, coercing wire args
│   ├── shape.py            — detect_shape (unary | stream | bidi)
│   ├── bound.py            — _BoundMethod + arg coercion helper
│   ├── dispatcher.py       — the Dispatcher class itself
│   └── entry_points.py     — `agentix.namespace` entry-point discovery
├── runtime/            — host↔sandbox transport, split three ways:
│   ├── shared/             — wire types, codec, framing, event-name constants
│   │     (codec, models, events, frames, rpc, pump, dump_frame)
│   ├── client/             — orchestrator-side RuntimeClient (HTTP + Socket.IO)
│   └── server/             — sandbox-side multiplexer + FastAPI/SIO app + worker
│         (app, sio, llm_proxy, trace_bridge, multiplexer, worker)
├── deployment/         — Deployment Protocol + the entry-point loader. Backends
│   │                      (local / daytona / e2b / third-party) ship as separate
│   │                      wheels — see Ecosystem packages below.
│   ├── base.py             — Sandbox dataclass + Deployment Protocol
│   └── _plugin.py          — Registry[T] for the `agentix.deployment` entry-point group
├── rollout/            — RolloutPool: ephemeral sandbox pool for batched RL rollouts
└── cli/                — `agentix` command-line: build, deploy, check
```

One-line per system:

- **idents / models / namespace** — the typed glue everything else imports
  from. No behaviour, just shared shapes.
- **rpc** — caller-side variants (`Unary`/`Stream`/`Bidi`) and the
  `Channel` input helper for bidi. What `RuntimeClient.remote(fn, …)`
  returns.
- **trace** — `agentix.trace.emit(...)` from a namespace impl; subscribers
  receive it. The dispatcher pins `call_id` into a contextvar so emitting
  code inherits correlation automatically.
- **dispatch** — server-side. `Dispatcher.bind(stub, impl)` registers a
  method, `dispatch(...)` / `dispatch_stream(...)` / `dispatch_bidi(...)`
  route a wire request to its impl. Call-shape detection lives here.
- **runtime.shared** — every type, constant, and codec on the wire between
  client and server. Both sides import from here; neither imports from
  the other.
- **runtime.client** — `RuntimeClient`: one HTTP connection for unary,
  one Socket.IO connection multiplexing stream/bidi/logs/trace.
- **runtime.server** — the bundle image's process (`agentix-server` console
  script). The `NamespaceMultiplexer` spawns one worker subprocess per
  namespace (`python -m agentix.runtime.server.worker`) and routes
  frames; the FastAPI + Socket.IO app is the entry surface.
- **deployment** — `Deployment` Protocol + plugin loader. The concrete
  backends live in separate wheels (`agentix-deployment-docker`,
  `-daytona`, `-e2b`) that install `agentix/deployment/<backend>.py`
  next to core's `base.py`. This is plugin axis #2 — `pip install` a
  backend wheel and `agentix deploy <name>` picks it up with zero core
  changes.
- **rollout** — `RolloutPool` allocates/recycles sandboxes for RL training
  loops. Sits on top of `Deployment` + `RuntimeClient`.
- **cli** — `agentix build / deploy / check`. Hardcoded subcommands; no
  plugin surface (third-party verbs ship their own `console_scripts`).

## Ecosystem packages

Core `agentix` deliberately ships **no** backend implementations, no
shell/file primitives, and no Dockerfile templates. Those live in
sibling repos / wheels so users `pip install` exactly the set they
need:

| Wheel | Repo | Entry points |
|---|---|---|
| `agentix-runtime-basic` | [Agentix-Runtime-Basic](https://github.com/Agentiix/Agentix-Runtime-Basic) | `agentix.namespace: bash, files` |
| `agentix-deployment-docker` | [Agentix-Deployment-Docker](https://github.com/Agentiix/Agentix-Deployment-Docker) | `agentix.deployment: local` |
| `agentix-deployment-daytona` | [Agentix-Deployment-Daytona](https://github.com/Agentiix/Agentix-Deployment-Daytona) | `agentix.deployment: daytona` |
| `agentix-deployment-e2b` | [Agentix-Deployment-E2B](https://github.com/Agentiix/Agentix-Deployment-E2B) | `agentix.deployment: e2b` |

A typical dev install:

```bash
pip install agentix \
            agentix-runtime-basic \
            agentix-deployment-docker
```

Downstream repos (`Agentix-Agents-Hub`, `Agentix-Datasets`) follow the
same shape — each one is a Python project declaring `agentix.namespace`
entry points, installed alongside core.

## Architecture (dispatch + entry-point discovery)

`c.remote(fn, ...)` reads exactly two things off `fn`: `fn.__module__` (the wire's `package` routing key) and `fn.__name__` (the method). The runtime maintains a `package → worker subprocess` table; on dispatch, it finds (or lazily registers) the worker for that module, forwards the call, returns the typed result. Nothing in the dispatch path requires `agentix.*` import paths or entry-point declarations — any importable Python module is a valid target.

The framework distinguishes two cases that hit the same dispatch:

* **Plugins** — reusable Python packages distributed via PyPI that ship under `agentix.<short>` so every consumer can `from agentix import bash` uniformly. Declare an `agentix.namespace` entry point so they show up in `agentix check` and get pre-discovered at startup. In bundle images they share the framework's `/nix/runtime/` venv by default (merged mode) or get a dep-isolated `/nix/<short>/` of their own when the user passes `agentix build --isolated`.
* **User projects** — your own code at your own module path (`my_company.agents.tasks`). **No entry point required. No `agentix.*` import path required. No `src/agentix/` PEP 420 layout required.** The runtime auto-registers any importable module on first dispatch: it probes each known venv interpreter, finds one that can `import <module>`, and spawns a worker there. Just `pip install -e .` your project somewhere the runtime can see (alongside the framework in dev mode, or installed into the bundle in production) and call `c.remote(my_module.fn, ...)`.

The other plugin axis — deployments — is documented in `docs/deployment.mdx`; host-side hooks (trace pub/sub, wire patterns, spec resolvers, CLI verbs) are in `docs/extend-runtime.mdx`.

### The plugin contract (for reusable packages)

A plugin is a normal Python distribution that declares one `agentix.namespace` entry point pointing at the package:

```toml
# pyproject.toml — the entire framework-facing surface
[project.entry-points."agentix.namespace"]
bash = "agentix.bash"
```

That's it. Key (`bash`) is the short name for display; value (`agentix.bash`) is the Python import path of the namespace package. The framework imports that module and discovers its async functions on first dispatch. (A legacy `module:Class` form is also accepted — `discover_methods` is duck-typed — but module-as-namespace is the recommended shape.)

### Plugin source layout

A plugin is a **normal Python project** (the shape `uv init --lib` produces) that contributes to the `agentix.*` import namespace:

```
Agentix-Runtime-Basic/                 # one such project; the `bash` + `files`
├── pyproject.toml                     # primitives ship together as `agentix-runtime-basic`
└── src/agentix/                       # `agentix/` has no __init__.py (PEP 420 namespace package)
    ├── bash/__init__.py               # async def run(...), async def run_stream(...), …
    └── files/__init__.py              # async def upload(...), async def download(...), …
```

The framework's `agentix/__init__.py` extends its `__path__` via `pkgutil.extend_path`, so once a plugin dist installs files at `<site-packages>/agentix/bash/`, `from agentix import bash` resolves and `bash.run` is the remote-callable function. Multiple plugin dists can install peer entries under `agentix/` without colliding.

Reserved by the framework — plugin dists may not shadow: `agentix.cli`, `agentix.deployment`, `agentix.dispatch`, `agentix.idents`, `agentix.models`, `agentix.namespace`, `agentix.rollout`, `agentix.runtime`, `agentix.trace`. Everything else under `agentix.*` is fair game.

### User-project layout (the path of least resistance)

If you're just *using* Agentix — not authoring a reusable plugin — you don't need any of the above. Drop your code at your usual module path, declare nothing, dispatch:

```
my-rl-experiment/
├── pyproject.toml         # name = "my-rl-experiment", no `agentix.namespace` entry points
└── src/my_rl_experiment/
    └── tasks.py           # async def rollout(...): ..., async def score(...): ...
```

```python
from agentix import RuntimeClient
from my_rl_experiment import tasks   # your own import path; no agentix.* dance

async with RuntimeClient(sandbox.runtime_url) as c:
    traj = await c.remote(tasks.rollout, seed=42)
    reward = await c.remote(tasks.score, trajectory=traj)
```

The multiplexer auto-registers `my_rl_experiment.tasks` on the first call: it probes each known venv interpreter (the runtime's own in dev mode; each `/nix/<short>/` in bundle mode) for one that can import the module, then spawns a worker there. As long as `pip install -e .` succeeded somewhere on the runtime's path, dispatch works.

`agentix build .` packages the current project into the bundle image — no entry-point declaration required. The short name for the image tag defaults to the dist name (`my-rl-experiment` → `my-rl-experiment:0.1.0`) when no entry point is declared.

### The package IS the namespace

```python
# src/agentix/bash/__init__.py
from dataclasses import dataclass

@dataclass
class BashResult:               # type — caller imports it for return annotations
    exit_code: int
    stdout: str

DEFAULT_TIMEOUT = 30            # constant — caller imports it as a value

async def run(command: str, timeout: float = DEFAULT_TIMEOUT) -> BashResult:
    proc = await asyncio.create_subprocess_shell(command, ...)
    ...

def _helper():                  # private — framework skips it
    ...
```

* **Discovery is duck-typed.** The framework walks the package's top-level attributes and picks the public **async** functions (`async def` / `async def ... yield`). Sync helpers, dataclasses, constants, and `_private` names are ignored by the framework but available to callers via normal import.
* **Method bodies are the real implementation.** There's no stub vs impl split.
* **No marker base class.** Namespace authors don't import or inherit from anything framework-specific — the package's identity comes from its entry-point declaration.
* **Class-style targets still work.** If you prefer `class XYZ:` with `@staticmethod` methods (e.g. for IDE-grouped autocomplete), declare the entry point as `xyz = "agentix.xyz:XYZ"` and the dispatcher walks the class instead. Duck typing means the framework accepts either shape.

`pip install ./Agentix-Runtime-Basic` works as-is. `pytest`, `pyright`, `ruff`, `uv build` — every standard Python tool works against the namespace's source dir without further configuration.

Build infrastructure is shared, not per-namespace:

- `Agentix-Runtime-Basic/runtime/Dockerfile` — the runtime image's Dockerfile; bundle images extend it
- Per-namespace `default.nix` (optional) — only when the namespace needs native system deps

The runtime loads each namespace lazily — the worker subprocess for a namespace is spawned on first `/_remote` call to that namespace; subsequent calls reuse the same worker.

### Extension axes beyond namespaces

The framework has **two** plugin axes — only the things that cross the host↔sandbox boundary are entry-point discovered:

| Axis | Group | What it ships |
|---|---|---|
| Namespaces | `agentix.namespace` | Python class whose `@staticmethod` methods run **inside the sandbox** |
| Deployments | `agentix.deployment` | host-side backend that **provisions** the sandbox (`local`, `daytona`, `e2b`, …) |

Everything else (trace sinks, wire patterns, spec resolvers, CLI verbs) is pure host-side Python. The hooks are plain functions/classes you import — no entry points, no `Registry[T]`. See [feedback memory](../../.claude/projects/-apdcephfs-gy4-share-302774114-davejhwang-Agentix/memory/feedback_plugins_only_cross_sandbox.md) for the principle.

- `agentix.trace.subscribe(fn)` to add a trace consumer (OTel, Sentry, custom bus).
- Call shapes (`unary` / `stream` / `bidi`) are detected from the method signature by `agentix.dispatch.detect_shape`. No extension hook — add a fourth shape by editing that function plus the matching branches in `Dispatcher.bind` and `RuntimeClient.remote`.
- Spec resolvers live as an ordered list in `agentix/cli/_resolve.py`; new spec shapes mean editing that file, not shipping a wheel.
- A new `agentix <verb>` CLI: ship your own `agentix-yourcmd` `console_scripts` binary; the central CLI is not a plugin surface.

### CLI

Developer commands ship as the `agentix` console script (`pip install -e .[dev]` registers it). The four built-in subcommands are hardcoded in `agentix/cli/__init__.py`:

```
agentix build ./Agentix-Runtime-Basic                          # build one namespace image
agentix build ./ns-a ./ns-b ./ns-c -o my-agent:0.1.0           # bundle several namespaces
agentix deploy local --image my-agent:0.1.0                    # run a sandbox
agentix check                                                  # list installed namespaces, smoke-import each
```

Each command is a thin module under `agentix/cli/`; `agentix --help` lists them. The three subcommands are hardcoded — third-party verbs go through their own `console_scripts` binaries, not a plugin registry.

**`agentix build <spec>`** — builds a single namespace image. `<spec>` is an explicit path (a directory with `pyproject.toml`), or a PyPI dist (`agentix-runtime-basic`, currently stubbed). Stages source + shared Dockerfile/nix into a temp dir, runs `docker build`. The base runtime image (`agentix/runtime:<version>`) must already be present locally — build it from `Agentix-Runtime-Basic/runtime/Dockerfile` or pull it from your registry.

**`agentix build <names> -o <tag>`** — same command, just N specs. Bundles multiple namespaces into one image. The runtime discovers them via `importlib.metadata.entry_points`, so no bundle disposition file is needed.

**`agentix deploy <backend>`** — provisions a sandbox. The available backends are whatever you've `pip install`-ed: `agentix-deployment-docker` registers `local`, `agentix-deployment-daytona` registers `daytona`, etc. Backends are one of the two plugin axes — they register under `agentix.deployment`, so `pip install agentix-deployment-fly` is enough for `agentix deploy fly --image …` to work without framework changes.

Foreground by default: prints `runtime_url`, holds the sandbox alive until Ctrl-C, then deletes. `--detach` exits after `create()` and just prints the sandbox handle.

**`agentix check`** — list installed namespaces and smoke-import each one. Drift detection is a non-concern since one class can't drift from itself.

### Build + deploy pipeline

`agentix build` produces a deploy-ready bundle image in one of two modes; the default makes inline composition work, the flagged mode keeps strict isolation.

**Merged mode (default).** Every spec installs into the framework's own venv at `/nix/runtime/`. One Python, one site-packages, one `bin/`. `from agentix.claude_code import run` inside your `my_project` worker resolves because claude_code's Python is in the same venv your worker imports from; the `claude` binary is on PATH because every namespace's Nix derivation symlinked into `/nix/runtime/bin/`. The cost: if two specs need incompatible Python deps, pip's resolver fails the build with a clear `ResolutionImpossible` error, and the CLI suggests `--isolated`.

**Isolated mode (`agentix build --isolated`).** Each spec gets `/nix/<short>/` with its own uv-managed venv and bin/. Conflicting Python deps don't merge; namespaces stop being able to inline-import each other. Cross-namespace composition has to go through host-side `c.remote(...)`. Use when merged mode fails or when you genuinely need per-namespace dep isolation.

Pipeline stages:

1. **Runtime image** (`agentix/runtime:<version>`): `FROM python:3.11-slim`, framework wheel pre-installed into `/nix/runtime/`, plus `uv` for venv creation, plus the wheel stashed at `/nix/.wheels/` for bundle stages. The Dockerfile ships with `agentix-runtime-basic` (`runtime/Dockerfile`); `agentix build` fails fast if the runtime image isn't already present locally.
2. **Bundle image** (`agentix build a b c -o tag`): extends the runtime image. If any spec ships `default.nix`, a Nix builder stage runs first; the derivation closure is COPY'd into `/nix/store/`. Then:
   * **Merged:** one `pip install /src/a /src/b /src/c` into `/nix/runtime/`; every Nix `bin/*` symlinked into `/nix/runtime/bin/`.
   * **Isolated:** for each spec, `uv venv /nix/<short>` + `pip install /nix/.wheels/agentix-*.whl /src/<short>`; Nix `bin/*` symlinked into `/nix/<short>/bin/`.

The runtime process itself doesn't load namespace code — the multiplexer spawns one **worker subprocess per package** on first call. In merged mode every worker uses `/nix/runtime/bin/python` and inherits the merged PATH; in isolated mode each worker uses its namespace's venv interpreter and gets `/nix/<short>/bin` prepended to PATH. Workers stay alive for the sandbox's lifetime; the runtime forwards RPC frames over stdin/stdout.

### Sandbox layout at runtime

Merged mode (default):

```
/                                — bundle image rootfs
  nix/
    runtime/                     — framework + every namespace + user code
      bin/agentix-server         — Docker ENTRYPOINT
      bin/python                 — every worker's interpreter
      bin/claude                 — symlink → /nix/store/<hash>/bin/claude
      bin/git                    — symlink → /nix/store/<hash>/bin/git
      lib/python3.11/site-packages/
        agentix/...              — framework
        agentix/bash/            — from agentix-runtime-basic
        agentix/files/           — from agentix-runtime-basic
        agentix/claude_code/     — from agentix-claude-code
        my_project/              — user's project
    store/                       — content-addressed Nix store
    .wheels/                     — framework wheel, reused at bundle build time
```

Isolated mode (`--isolated`):

```
/                                — bundle image rootfs
  nix/
    runtime/                     — framework venv only
      bin/agentix-server
      bin/python
      lib/python3.11/site-packages/agentix/...
    bash/                        — one directory per namespace
      bin/python
      lib/python3.11/site-packages/agentix/bash/...
    claude_code/                 — namespace with default.nix
      bin/python
      bin/claude                 — symlink → /nix/store/<hash>/bin/claude
      bin/git                    — symlink → /nix/store/<hash>/bin/git
      lib/python3.11/site-packages/agentix/claude_code/...
    store/, .wheels/             — same as merged
```

`agentix-server` (the runtime entrypoint) binds to `AGENTIX_BIND_PORT` and starts the multiplexer; namespace workers spawn on first dispatch.

### Runtime startup (lazy)

On lifespan startup the multiplexer:

1. Walks each `/nix/<dir>/lib/python*/site-packages` for `agentix.namespace` entry points. In merged mode the relevant dir is `/nix/runtime/`; in isolated mode it's every `/nix/<short>/`. Dev/test mode walks the current Python env instead.
2. For each entry point, records `package → (worker_target, venv_python, bin_dir)` — **no imports, no subprocess yet**.
3. First `/_remote` for that namespace spawns `<venv_python> -m agentix.runtime.server.worker --target <module>` with `PATH=<bin_dir>:$PATH`, connects stdin/stdout.
4. First `/_remote` for an **unknown** package triggers on-demand registration: the multiplexer probes each discovered venv interpreter to see if the module imports there; first match registers + spawns a worker. This is how user projects without `agentix.namespace` entry points get dispatched.
5. Subsequent calls reuse the spawned worker process.

Two dists registering the same entry-point name raise `PluginConflictError` on first lookup. There are **no caller-chosen namespaces**; the Python import path is the identity.

### Wire

Two transports, used per call shape:

**Unary** — `POST /_remote` (HTTP, JSON):

```
POST /_remote
  { "package": "agentix.agent.claude_code",
    "method":  "run",
    "args":    [],
    "kwargs":  { "instruction": "fix the bug" } }

← { "ok": true, "value": { "exit_code": 0, "stdout": "...", "patch": "..." } }
```

Failures come back as `{ "ok": false, "error": {...} }`. Wire stays 200.

**Server-streaming, bidirectional, and log subscription** — Socket.IO at `/socket.io/`. One persistent Socket.IO connection per `RuntimeClient` multiplexes all such calls, demultiplexed by a caller-generated `call_id`. Event shapes:

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

logs:subscribe    {filter?: <logger-name prefix>}
log               {level, name, message, timestamp}
logs:unsubscribe  {}
```

Runtime built-ins (`/exec`, `/upload`, `/download`, `/health`, `/namespaces`) live alongside `/_remote` at the runtime root, unrelated to namespace dispatch.

### Caller side

```python
from agentix import RuntimeClient
from agentix.agent.claude_code import ClaudeCode

async with RuntimeClient(sandbox.runtime_url) as c:
    result = await c.remote(
        ClaudeCode.run,
        instruction="fix the bug",
        workdir="/workspace",
    )
    # `result: RunResult` — IDE / pyright infer from ClaudeCode.run's return type
```

`RuntimeClient.remote(fn, *args, **kwargs)` reads `fn.__module__` (routing key) + `fn.__name__` (method), serialises via pydantic `TypeAdapter` driven by `inspect.signature(fn)`, decodes the response into `fn`'s return type.

### PATH policy for the `bash` primitive

Shell exec is the `bash` namespace, shipped by the `agentix-runtime-basic` wheel, not a runtime built-in. Invoke via `c.remote(bash.run, command=...)` after installing the wheel.

User subprocess default `PATH=/usr/local/bin:/usr/bin:/bin`. Namespaces that ship native binaries via `default.nix` reference them by their absolute `/nix/store/<hash>/bin/<name>` path inside the impl — content-addressed paths are stable across the bundle's lifetime.

### What Nix buys us (when used)

- Content-addressed `/nix/store` paths → multiple namespaces' system deps never collide.
- Hermetic native binaries per namespace (claude, git, …) referenced via Nix-absolute shebangs + RPATH.
- Optional opt-in — a namespace with only Python deps doesn't ship a `default.nix` and `agentix build` skips Nix entirely.

### Deliberate non-choices

- **Subprocess per namespace** (not in-process). Each namespace runs in its own venv's Python, isolated for dep conflicts. The multiplexer in the runtime process routes RPC frames over stdin/stdout. The pre-isolation in-process model was reversed when per-extension venv was introduced.
- **No reverse proxy.** `POST /_remote` is direct dispatch into the multiplexer; namespaces don't expose arbitrary HTTP routes.
- **No caller-chosen namespaces.** Entry-point's module path is the routing identity. Two dists registering the same name raise `PluginConflictError`.
- **Streaming returns** via `async def f(...) -> AsyncIterator[T]: yield ...`: `async for x in c.remote(stream_fn, ...)`. Wire is Socket.IO `stream`/`stream:item`/`stream:end` events. Bidi (impl takes a `Channel[I]` parameter and returns `AsyncIterator[O]`; caller passes a `Channel[I]` and pushes via `inbox.send(...)`) is supported via the `bidi:*` event family. `c.remote(fn, ...)` returns a tagged variant (`Unary[R]` / `Stream[R]` / `Bidi[I, R]`) so each shape is awaited or iterated with its natural Python protocol; `match` over the variant for generic dispatch.
- **One bundle image per sandbox.** Not many namespace images mounted at deploy time — the bundle carries every namespace venv pre-built. Rebuilding the bundle is the way to change which namespaces a sandbox exposes.

## Implementation notes

- **One image at deploy.** `SandboxConfig.image` is the deploy-ready bundle produced by `agentix build`. The deployment just runs it; there are no per-namespace mounts or volumes to coordinate.
- **No local Nix required.** Namespace authors do `docker build`; Nix lives in the builder stage of the generated bundle Dockerfile only when at least one namespace ships `default.nix`.
- **Bundle venv strategy (default: merged).** `agentix build` merges every spec + its Python deps into the framework's own `/nix/runtime/` venv. Inline composition works (`from agentix.bash import run` inside your own worker just imports it). Nix binaries from every namespace's `default.nix` symlink into `/nix/runtime/bin/` so they're on PATH for every worker. The cost is that pip's resolver runs once over the union of dep sets — if two specs ship incompatible deps, the build fails at install time. Use `agentix build --isolated` to fall back to one venv per namespace; the framework warns + suggests the flag automatically when it sees a resolution error.
- **Sandbox starts fast.** Warm sandbox is `-v` mounts + tmpfs + symlink loop (shell-time, ~100 ms) + import of each namespace package (typically tens of ms each).
- **Populate is lock-serialised** in-process to avoid concurrent `docker run -v` races on the same image's volume. Cross-process coordination is not currently provided; documented as a single-orchestrator assumption.

## Typing conventions

The wire layer is loosely typed at the protocol level (strings, JSON), so we lean on the Python type system to keep the surrounding code honest. Four house rules:

### 1. Namespace stubs + composition impls (R1)

A namespace's typed surface is a `Namespace` subclass with `...`-bodied methods. The matching impl is a **separate, independent class** whose methods structurally match the stub. `_register.register()` composes them:

```python
# __init__.py
from agentix.namespace import Namespace

class Bash(Namespace):
    async def run(self, command: str) -> BashResult: ...
    async def run_stream(self, command: str) -> AsyncIterator[BashEvent]: ...

# _impl.py — no inheritance from Bash
class BashImpl:
    async def run(self, command: str) -> BashResult: ...
    async def run_stream(self, command: str) -> AsyncIterator[BashEvent]: ...

# _register.py
def register() -> Dispatcher:
    return Dispatcher.bind_namespace(Bash, BashImpl())
```

`Dispatcher.bind_namespace` walks the stub class via `agentix.namespace.discover_methods`, looks up the matching attribute on the impl instance, and calls `bind()` for each pair. Composition, not inheritance — re-read the rule three times above if tempted otherwise.

**Static type checking** is opt-in. `Namespace` is a `Protocol` so users who want pyright to verify the impl can declare:

```python
@runtime_checkable
class Bash(Namespace, Protocol):
    async def run(self, command: str) -> BashResult: ...

impl: Bash = BashImpl()  # pyright catches structural mismatch here
```

### 2. Call shapes (R2)

Three call shapes (`unary` / `stream` / `bidi`) cover every signature the framework supports. `agentix.dispatch.detect_shape(sig)` returns one of those strings:

* `unary`  — `async def f(...) -> T`
* `stream` — `async def f(...) -> AsyncIterator[T]: yield ...` (real async generator)
* `bidi`   — same as stream + a `Channel[U]` parameter (caller-pushed input channel)

Detection uses `inspect.isasyncgenfunction(fn)` as the source of truth (annotations are only a hint — a regular `async def` returning an iterator value is unary, not stream) plus a scan for `Channel[T]` parameters. Runs at `Dispatcher.bind` time and again on every `c.remote(...)` client-side; both branch on the resulting string. There is no plugin hook for new shapes — the assumption is the framework's three are exhaustive. If a fourth ever becomes necessary, edit `detect_shape` plus the two branch sites; the abstraction overhead of a swappable pattern hierarchy isn't paying for itself.

### 3. Branded identifiers from `agentix.idents`

There are four `str`s in the wire layer that are easy to confuse — a namespace's import path, a method name, the rollout correlation key, and the sandbox handle. They are `NewType`d in `agentix/idents.py` (`PackageName`, `MethodName`, `CallId`, `SandboxId`) and consumed everywhere the wire types appear:

- `NamespaceManifest.package: PackageName`
- `RemoteRequest.{package, method, call_id}`
- `TraceEvent.{call_id, source}` (source is also a `PackageName`)
- `Sandbox.sandbox_id` / `SandboxInfo.sandbox_id` / `DockerDeployment._ports`
- `Dispatcher._methods` keyed by `MethodName`; `NamespaceMultiplexer._entries` by `PackageName`
- `trace.set_call_context` / `trace.emit` / contextvars

When you write new wire-adjacent code, use the branded types — pyright treats them as distinct, so swapping `MethodName` for `PackageName` becomes a type error. Pydantic v2 understands `NewType`, so wire round-trip is unchanged.
