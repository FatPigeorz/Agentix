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

The substrate is a single Python runtime process inside a sandbox container, into which **namespace dists install their Python packages**. The runtime walks `importlib.metadata.entry_points(group="agentix.namespace")` at start-up to discover them and dispatches `c.remote(Bash.run, ...)` calls in-process. No subprocess per namespace, no UDS, no reverse-proxy, no manifest files.

The word *namespace* here means the framework-recognized unit of extension on the dispatch axis: a Python class whose `@staticmethod` methods are the remote-callable surface. (Other extension axes — deployments, trace sinks, etc. — are documented in `docs/plugin-authors.mdx`.)

### The extension contract

A namespace is a normal Python distribution that declares one `agentix.namespace` entry point:

```toml
# pyproject.toml — the entire framework-facing surface
[project.entry-points."agentix.namespace"]
bash = "agentix.bash:Bash"
```

That's it. Key (`bash`) is the short name for display; value (`agentix.bash:Bash`) names the module and the `Namespace` subclass to load. The framework imports and binds the class on first dispatch.

### Namespace source layout

A namespace is a **normal Python project** (the shape `uv init --lib` produces) that contributes to the `agentix.*` import namespace:

```
primitives/bash/
├── pyproject.toml                  # name = "agentix-bash", [project.entry-points."agentix.namespace"]
└── src/agentix/bash/               # `agentix/` has no __init__.py (PEP 420 namespace package)
    └── __init__.py                 # `class Bash(Namespace)` with @staticmethod bodies
```

The framework's `agentix/__init__.py` extends its `__path__` via `pkgutil.extend_path`, so once a namespace dist installs files at `<site-packages>/agentix/bash/`, `from agentix.bash import Bash` resolves. Multiple namespace dists can install peer entries under `agentix/` without colliding.

Reserved by the framework — namespace dists may not shadow: `agentix.cli`, `agentix.deployment`, `agentix.dispatch`, `agentix.idents`, `agentix.models`, `agentix.namespace`, `agentix.rollout`, `agentix.runtime`, `agentix.trace`, `agentix.wire`. Everything else under `agentix.*` is fair game.

### The class IS the namespace

```python
# src/agentix/bash/__init__.py
from agentix.namespace import Namespace

class Bash(Namespace):
    @staticmethod
    async def run(command: str) -> BashResult:
        proc = await asyncio.create_subprocess_shell(command, ...)
        ...
```

* `class Bash(Namespace)` declares the namespace. The methods are `@staticmethod` — the class is a pure namespace, no `self`, no instance state.
* Method bodies are the **real implementation**. There's no stub vs impl split. Namespaces with heavy dependencies use *lazy imports inside methods* if they want to avoid paying import cost on caller-side.
* No `_register.py`, no `_impl.py`, no `<Name>Impl` convention, no `manifest.json`. The framework reads `pyproject.toml` for metadata via `importlib.metadata` and loads the class via entry points.

`pip install ./primitives/bash` works as-is. `pytest`, `pyright`, `ruff`, `uv build` — every standard Python tool works against the namespace's source dir without further configuration.

Build infrastructure is shared, not per-namespace:

- `primitives/_template/Dockerfile` — same for every namespace
- `primitives/_template/default.nix` — same for every namespace; pulls metadata from the namespace's `pyproject.toml`

The runtime loads each namespace lazily — the entry-point object is captured at startup but `ep.load()` only runs on first dispatch for that namespace. A broken namespace surfaces on call, not at boot.

### Extension axes beyond namespaces

The framework has **two** plugin axes — only the things that cross the host↔sandbox boundary are entry-point discovered:

| Axis | Group | What it ships |
|---|---|---|
| Namespaces | `agentix.namespace` | Python class whose `@staticmethod` methods run **inside the sandbox** |
| Deployments | `agentix.deployment` | host-side backend that **provisions** the sandbox (`local`, `daytona`, `e2b`, …) |

Everything else (trace sinks, wire patterns, spec resolvers, CLI verbs) is pure host-side Python. The hooks are plain functions/classes you import — no entry points, no `Registry[T]`. See [feedback memory](../../.claude/projects/-apdcephfs-gy4-share-302774114-davejhwang-Agentix/memory/feedback_plugins_only_cross_sandbox.md) for the principle.

- `agentix.trace.register_sink(fn)` to add a trace consumer (OTel, Sentry, custom bus).
- Wire patterns are a frozen tuple of three built-ins (`unary` / `stream` / `bidi`) in `agentix/wire.py`. No extension hook — add a fourth `WirePattern` subclass directly if a new call shape ever becomes necessary.
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

### Sandbox layout at runtime

```
/nix/                            — tmpfs (writable by entrypoint only)
  store/                         — symlink forest: each /mnt/*/store/<hash> linked here
/mnt/
  runtime/                       — runtime image's /nix slice
    store/<hash>-*/
    entry/bin/start              — agentix-server
  c<digest>/                     — namespace image's /nix slice (dir name is internal)
    store/<hash>-*/              — content-addressed Nix store; includes the namespace's
                                   site-packages dir, made visible to the runtime's Python
                                   via the symlink farm in /nix/store
```

Sandbox entrypoint (inlined into the `docker run` command):
```sh
mkdir -p /nix/store
for d in /mnt/*/store; do ln -sfn "$d"/* /nix/store/; done
exec /mnt/runtime/entry/bin/start
```

### Runtime startup (lazy)

On lifespan startup, the runtime:

1. Walks `importlib.metadata.entry_points(group="agentix.namespace")`. Because the namespaces' wheels installed into the same Nix-managed Python environment, every namespace's `dist-info/entry_points.txt` is visible to `importlib.metadata`.
2. For each entry point, **registers a pending entry** in the global `Registry`. **No imports run.**
3. The namespace's class is `ep.load()`ed on **first `/_remote` request** for that package (`Registry.get_or_load`), under a per-package async lock so concurrent first-calls share one import.
4. Import failures are cached on the entry; every subsequent call returns the same error without retrying.

There is no on-disk `manifest.json` or `/mnt/*` scan — discovery is purely the entry-point walk.

Two images shipping the same `package` collide — second is skipped with a warning. There are **no caller-chosen namespaces**; the Python import path is the identity.

This means: a broken namespace does not block sandbox boot; an unused namespace costs nothing to mount; first-call latency for a namespace includes its one-time import cost (typically tens of ms).

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

User subprocess default `PATH=/usr/local/bin:/usr/bin:/bin` (task image's). Nix env vars (`LD_LIBRARY_PATH`, `NIX_*`, `PYTHONPATH`, etc.) scrubbed to avoid ABI clash. `paths_from=["<package>"]` prepends that namespace's `entry/bin` to PATH.

### What Nix buys us

- Content-addressed `/nix/store` paths → multiple namespaces' deps never collide, so the symlink forest is trivially safe.
- Hermetic native binaries per namespace (claude, git, …) referenced via Nix-absolute shebangs + RPATH.

### Deliberate non-choices

- **No subprocess-per-namespace.** All namespace impls run in the runtime's Python event loop.
- **No reverse proxy.** `POST /_remote` is direct dispatch; namespaces expose Python functions, not arbitrary HTTP routes.
- **No caller-chosen namespaces.** `manifest.package` is the identity. Two images shipping the same package collide.
- **Streaming returns** via `AsyncIterator[T]` annotation on the stub: `async for x in c.remote(stream_fn, ...)`. Wire is Socket.IO `stream`/`stream:item`/`stream:end` events. Bidi (stub takes one `AsyncIterator[T]` parameter and returns `AsyncIterator[U]`) is supported via the `bidi:*` event family.
- **No monolithic single-image runtime.** Each namespace is its own image; the runtime image only ships `agentix` + `pydantic` + `fastapi` + `uvicorn`.

## Implementation notes

- **Hash paths are internal.** Users pass docker image refs in `SandboxConfig.namespaces` — either as strings or as the namespace's imported Python package (which exposes `__image__` for resolution). Mount-dir names are deployment-internal (`/mnt/c<digest>`); the runtime indexes by `manifest.package`.
- **No local Nix required.** Namespace authors do `docker build`; Nix lives in the builder stage of their Dockerfile.
- **Namespace Python deps stay thin.** Namespaces share the runtime's Python interpreter — Python wrappers should depend on stdlib + the `agentix` package itself (which already brings pydantic). Heavy deps belong in Nix-bundled native binaries, not in `pyproject.toml`.
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

### 2. Wire patterns (R2)

Call shapes (unary / server-stream / bidi) live in `agentix.wire` as `WirePattern` subclasses. Each pattern owns:

* `matches(sig) -> bool` — does this signature use this pattern?
* `bind(sig)` — per-method state precompute at `Dispatcher.bind` time.

The three built-ins (`UnaryPattern`, `StreamPattern`, `BidiPattern`) are exhaustive for the call shapes the framework supports and are checked in specific-to-general order via `select_pattern`. They are **not** user-extensible — there is no `register_pattern` hook. If a future call shape becomes necessary, add a fourth `WirePattern` subclass to `agentix/wire.py` directly. The Dispatcher picks the pattern at bind time and caches it on the bound method.

### 3. Branded identifiers from `agentix.idents`

There are four `str`s in the wire layer that are easy to confuse — a namespace's import path, a method name, the rollout correlation key, and the sandbox handle. They are `NewType`d in `agentix/idents.py` (`PackageName`, `MethodName`, `CallId`, `SandboxId`) and consumed everywhere the wire types appear:

- `NamespaceManifest.package: PackageName`
- `RemoteRequest.{package, method, call_id}`
- `TraceEvent.{call_id, source}` (source is also a `PackageName`)
- `Sandbox.sandbox_id` / `SandboxInfo.sandbox_id` / `DockerDeployment._ports`
- `Dispatcher._methods` keyed by `MethodName`, `Registry._entries` by `PackageName`
- `trace.set_call_context` / `trace.emit` / contextvars

When you write new wire-adjacent code, use the branded types — pyright treats them as distinct, so swapping `MethodName` for `PackageName` becomes a type error. Pydantic v2 understands `NewType`, so JSON round-trip is unchanged.

### 4. Stub ↔ impl signature drift is a CI failure

`tools/check_stub_impl.py` loads each namespace's `_register.register()` and compares the stub's signature against the impl's for every bound method — parameter names, kinds, defaults, annotations, return type. Run it locally:

```
python tools/check_stub_impl.py            # defaults to primitives/
python tools/check_stub_impl.py path/to/namespace
```

Drift causes a non-zero exit. This is the one class of bug the runtime itself cannot catch until the first call lands, so it gets caught in CI instead.

The checker is shape-agnostic: it works for both legacy module-function stubs and for the upcoming class-based `Namespace` shape, because both bottom out at `Dispatcher.bind()`.
