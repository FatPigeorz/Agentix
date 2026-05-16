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

## Architecture (typed Python namespaces, entry-point discovery)

A namespace is a **Python package** (`agentix.<short>`) whose top-level async functions are the remote-callable surface. Caller-side, `c.remote(bash.run, ...)` reads `bash.run.__module__` as the routing key. Server-side, the runtime walks `importlib.metadata.entry_points(group="agentix.namespace")` to discover packages, then **spawns one worker subprocess per namespace** (using each namespace's own venv interpreter) and forwards RPC frames over stdin/stdout — full per-extension dep isolation, no in-process import of namespace code.

The other plugin axis — deployments — is documented in `docs/deployment.mdx`; host-side hooks (trace pub/sub, wire patterns, spec resolvers, CLI verbs) are in `docs/extend-runtime.mdx`.

### The extension contract

A namespace is a normal Python distribution that declares one `agentix.namespace` entry point pointing at the package:

```toml
# pyproject.toml — the entire framework-facing surface
[project.entry-points."agentix.namespace"]
bash = "agentix.bash"
```

That's it. Key (`bash`) is the short name for display; value (`agentix.bash`) is the Python import path of the namespace package. The framework imports that module and discovers its async functions on first dispatch. (A legacy `module:Class` form is also accepted — `discover_methods` is duck-typed — but module-as-namespace is the recommended shape.)

### Namespace source layout

A namespace is a **normal Python project** (the shape `uv init --lib` produces) that contributes to the `agentix.*` import namespace:

```
primitives/bash/
├── pyproject.toml                  # name = "agentix-bash", [project.entry-points."agentix.namespace"]
└── src/agentix/bash/               # `agentix/` has no __init__.py (PEP 420 namespace package)
    └── __init__.py                 # async def run(...), async def run_stream(...), dataclasses, …
```

The framework's `agentix/__init__.py` extends its `__path__` via `pkgutil.extend_path`, so once a namespace dist installs files at `<site-packages>/agentix/bash/`, `from agentix import bash` resolves and `bash.run` is the remote-callable function. Multiple namespace dists can install peer entries under `agentix/` without colliding.

Reserved by the framework — namespace dists may not shadow: `agentix.cli`, `agentix.deployment`, `agentix.dispatch`, `agentix.idents`, `agentix.models`, `agentix.namespace`, `agentix.rollout`, `agentix.runtime`, `agentix.trace`. Everything else under `agentix.*` is fair game.

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

`pip install ./primitives/bash` works as-is. `pytest`, `pyright`, `ruff`, `uv build` — every standard Python tool works against the namespace's source dir without further configuration.

Build infrastructure is shared, not per-namespace:

- `primitives/_template/Dockerfile` — the runtime image's Dockerfile; bundle images extend it
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
agentix build primitives/bash                              # build one namespace image
agentix build bash files claude-code -o my-agent:0.1.0     # bundle several namespaces
agentix deploy local --image my-agent:0.1.0                # run a sandbox
agentix check                                              # list installed namespaces, smoke-import each
```

Each command is a thin module under `agentix/cli/`; `agentix --help` lists them. The four subcommands are hardcoded — third-party verbs go through their own `console_scripts` binaries, not a plugin registry.

**`agentix build <spec>`** — builds a single namespace image. `<spec>` is an explicit path (`primitives/bash`), a short name resolved against the repo (`bash`), or a PyPI dist (`agentix-bash`, currently stubbed). Stages source + shared Dockerfile/nix into a temp dir, runs `docker build`.

**`agentix build <names> -o <tag>`** — same command, just N specs. Bundles multiple namespaces into one image. The runtime discovers them via `importlib.metadata.entry_points`, so no bundle disposition file is needed.

**`agentix deploy <backend>`** — provisions a sandbox. `local` is wired through `DockerDeployment`; `daytona`/`e2b` are CLI surfaces awaiting their managed-sandbox integrations. Backends are one of the two plugin axes — they register under `agentix.deployment`, so `pip install agentix-deployment-fly` is enough for `agentix deploy fly --image …` to work without framework changes.

Foreground by default: prints `runtime_url`, holds the sandbox alive until Ctrl-C, then deletes. `--detach` exits after `create()` and just prints the sandbox handle.

**`agentix check`** — list installed namespaces and smoke-import each one. Drift detection is a non-concern since one class can't drift from itself.

### Build + deploy pipeline

Every namespace gets its **own venv** for full dep isolation — two
namespaces can pull incompatible versions of the same Python dep
without conflict. System deps (claude CLI, git, libffi, …) live under
optional per-namespace `default.nix`. `agentix build` produces a
single deploy-ready bundle image:

1. **Runtime image** (`agentix/runtime:<version>`): `FROM python:3.11-slim`, framework wheel pre-installed into `/nix/runtime/`, plus `uv` for per-namespace venv creation, plus the wheel stashed at `/nix/.wheels/` for bundle stages. `agentix build` auto-builds this image from `primitives/_template/Dockerfile` if missing locally — users never run `docker build` directly.
2. **Bundle image** (`agentix build a b c -o tag`): extends the runtime image. If any spec ships `default.nix`, a Nix builder stage runs first; the derivation closure is COPY'd into `/nix/store/`. For each spec, `uv venv /nix/<short>` + `pip install` the namespace into that venv (alongside the framework wheel). For specs with `default.nix`, the derivation's `bin/*` is then symlinked into `/nix/<short>/bin/`. Bundles with no system deps anywhere skip Nix entirely.

The runtime process itself doesn't load namespace code — the multiplexer spawns one **worker subprocess per namespace** on first call, using that namespace's venv interpreter. When spawning, the multiplexer **prepends `/nix/<short>/bin` to the worker's PATH** so user code can `subprocess.run("git", ...)` without knowing the absolute path. Workers stay alive for the sandbox's lifetime; the runtime forwards RPC frames over stdin/stdout.

### Sandbox layout at runtime

```
/                                — bundle image rootfs
  nix/
    runtime/                     — framework venv (uv-managed)
      bin/agentix-server         — Docker ENTRYPOINT; the multiplexer process
      bin/python
      lib/python3.11/site-packages/agentix/...
    bash/                        — one directory per namespace; venv + sys-deps merge here
      bin/python                 — worker interpreter for `agentix.bash`
      lib/python3.11/site-packages/agentix/bash/...
    claude_code/                 — namespace with default.nix
      bin/python                 — venv interpreter
      bin/claude                 — symlink → /nix/store/<hash>/bin/claude
      bin/git                    — symlink → /nix/store/<hash>/bin/git
      lib/python3.11/site-packages/agentix/claude_code/...
    files/
      bin/python
      lib/python3.11/site-packages/agentix/files/...
    store/                       — content-addressed Nix store; only present if at
      <hash>-claude-*/bin/claude   least one namespace shipped default.nix
      <hash>-git-*/bin/git
    .wheels/                     — framework wheel; reused at bundle build time
      agentix-<version>-py3-none-any.whl
```

`agentix-server` (the runtime entrypoint) binds to `AGENTIX_BIND_PORT` and starts the multiplexer; namespace workers spawn on first dispatch.

### Runtime startup (lazy)

On lifespan startup the multiplexer:

1. Walks `/nix/<short>/lib/python*/site-packages` for `agentix.namespace` entry points (skipping `/nix/runtime`, `/nix/store`, `/nix/.wheels`). Dev/test mode walks the current Python env instead.
2. For each entry point, records `package → (worker_target, venv_python, bin_dir)` — **no imports, no subprocess yet**.
3. First `/_remote` for that namespace spawns `<venv_python> -m agentix.runtime.worker --target <module>` with `PATH=<bin_dir>:$PATH`, connects stdin/stdout. The worker binds the package via Dispatcher, sends a `ready` frame, then serves frames until shutdown.
4. Subsequent calls reuse the same worker process.

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

Shell exec is the `bash` primitive namespace (`primitives/bash/`), not a runtime built-in. Invoke via `c.remote(Bash.run, command=...)`.

User subprocess default `PATH=/usr/local/bin:/usr/bin:/bin`. Namespaces that ship native binaries via `default.nix` reference them by their absolute `/nix/store/<hash>/bin/<name>` path inside the impl — content-addressed paths are stable across the bundle's lifetime.

### What Nix buys us (when used)

- Content-addressed `/nix/store` paths → multiple namespaces' system deps never collide.
- Hermetic native binaries per namespace (claude, git, …) referenced via Nix-absolute shebangs + RPATH.
- Optional opt-in — a namespace with only Python deps doesn't ship a `default.nix` and `agentix build` skips Nix entirely.

### Deliberate non-choices

- **Subprocess per namespace** (not in-process). Each namespace runs in its own venv's Python, isolated for dep conflicts. The multiplexer in the runtime process routes RPC frames over stdin/stdout. The pre-isolation in-process model was reversed when per-extension venv was introduced.
- **No reverse proxy.** `POST /_remote` is direct dispatch into the multiplexer; namespaces don't expose arbitrary HTTP routes.
- **No caller-chosen namespaces.** Entry-point's module path is the routing identity. Two dists registering the same name raise `PluginConflictError`.
- **Streaming returns** via `AsyncIterator[T]` annotation on the stub: `async for x in c.remote(stream_fn, ...)`. Wire is Socket.IO `stream`/`stream:item`/`stream:end` events. Bidi (stub takes one `AsyncIterator[T]` parameter and returns `AsyncIterator[U]`) is supported via the `bidi:*` event family.
- **One bundle image per sandbox.** Not many namespace images mounted at deploy time — the bundle carries every namespace venv pre-built. Rebuilding the bundle is the way to change which namespaces a sandbox exposes.

## Implementation notes

- **One image at deploy.** `SandboxConfig.image` is the deploy-ready bundle produced by `agentix build`. The deployment just runs it; there are no per-namespace mounts or volumes to coordinate.
- **No local Nix required.** Namespace authors do `docker build`; Nix lives in the builder stage of the generated bundle Dockerfile only when at least one namespace ships `default.nix`.
- **Per-namespace venv = per-namespace deps.** Namespaces declare Python deps in their own pyproject; each gets its own `/nix/<short>/` so versions don't have to be unified across the bundle. uv keeps venv creation millisecond-scale during build. Any Nix-provided binaries from `default.nix` are symlinked into the same `bin/` and the worker's PATH is prepended with this dir — user code uses bare names (`git`, `claude`) transparently.
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

* `unary`  — plain `T` return
* `stream` — `-> AsyncIterator[T]` return, no `AsyncIterator` params
* `bidi`   — one `AsyncIterator[U]` param + `-> AsyncIterator[V]` return

Detection runs at `Dispatcher.bind` time and again on every `c.remote(...)` client-side; both branch on the resulting string. There is no plugin hook for new shapes — the assumption is the framework's three are exhaustive. If a fourth ever becomes necessary, edit `detect_shape` plus the two branch sites; the abstraction overhead of a swappable pattern hierarchy isn't paying for itself.

### 3. Branded identifiers from `agentix.idents`

There are four `str`s in the wire layer that are easy to confuse — a namespace's import path, a method name, the rollout correlation key, and the sandbox handle. They are `NewType`d in `agentix/idents.py` (`PackageName`, `MethodName`, `CallId`, `SandboxId`) and consumed everywhere the wire types appear:

- `NamespaceManifest.package: PackageName`
- `RemoteRequest.{package, method, call_id}`
- `TraceEvent.{call_id, source}` (source is also a `PackageName`)
- `Sandbox.sandbox_id` / `SandboxInfo.sandbox_id` / `DockerDeployment._ports`
- `Dispatcher._methods` keyed by `MethodName`; `NamespaceMultiplexer._entries` by `PackageName`
- `trace.set_call_context` / `trace.emit` / contextvars

When you write new wire-adjacent code, use the branded types — pyright treats them as distinct, so swapping `MethodName` for `PackageName` becomes a type error. Pydantic v2 understands `NewType`, so wire round-trip is unchanged.
