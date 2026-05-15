# Namespace Protocol (v0.1.0)

A **namespace** is a Docker image that ships a Python package the Agentix runtime imports in-process. Calls are typed Python (`await c.remote(my_namespace.fn, x=1)`), not arbitrary HTTP — the runtime exposes a single dispatch endpoint and routes by Python module path.

## Image convention

A namespace image MUST:

1. Declare `VOLUME /nix` (so Docker's volume-init-from-image rule populates a named volume on first attach).
2. Contain `/nix/store/<hash>-*/` — the content-addressed Nix dependencies (native binaries + the namespace's Python wheel contents).
3. Contain `/nix/entry/python/<package-tree>/` — the namespace's Python package, drop-importable when the runtime adds `entry/python` to `sys.path`.
4. Contain `/nix/entry/manifest.json` — a `NamespaceManifest` JSON file with `abi == AGENTIX_CLOSURE_ABI` and `package = "agentix_namespaces.<name>"`.
5. Optionally contain `/nix/entry/bin/...` — native binaries the impl shells out to (claude, git, …). Exposed to `/exec` via `paths_from=["agentix_namespaces.<name>"]`.

There is no `bin/start`. The runtime is a single process; namespaces contribute Python modules to it, not separate binaries.

## Python package layout

The namespace's Python package must declare three things:

```
agentix_namespaces/
└── <name>/
    ├── __init__.py        # caller-facing stubs
    ├── _impl.py           # real implementation
    └── _register.py       # def register() -> Dispatcher
```

### `__init__.py` — typed stubs

```python
from dataclasses import dataclass

@dataclass
class RunResult:
    exit_code: int
    patch: str

def run(instruction: str, workdir: str = "/testbed") -> RunResult:
    """Run against an instruction; returns a fake patch echoing the input."""
    raise NotImplementedError("call via RuntimeClient.remote(my_namespace.run, ...)")
```

Signature is the contract. Body raises so a caller who accidentally invokes `run(...)` locally fails fast.

### `_impl.py` — real bodies

```python
from . import RunResult

def run(instruction: str, workdir: str = "/testbed") -> RunResult:
    # actually do work
    return RunResult(exit_code=0, patch="...")
```

Plain functions; no decorators, no FastAPI, no socket binding. May be `async def`.

### `_register.py` — bind stubs to impls

```python
from agentix.dispatch import Dispatcher
from . import run
from ._impl import run as _run

def register() -> Dispatcher:
    d = Dispatcher()
    d.bind(run, _run)
    return d
```

Runtime calls `register()` once on startup. Pure function, no globals.

## Manifest

`/nix/entry/manifest.json` is the marker that identifies a `/mnt/<dir>` mount as an Agentix namespace. The runtime ignores any mount whose manifest is missing, malformed, or carries an incompatible abi.

```json
{
  "abi": 1,
  "name": "my-namespace",
  "version": "1.0.0",
  "package": "agentix_namespaces.my_namespace",
  "kind": "agent",
  "description": "Short blurb"
}
```

| Field | Required | Purpose |
|---|---|---|
| `abi` | yes | Must equal `AGENTIX_CLOSURE_ABI` (currently `1`). Runtime skips mismatches with a warning. |
| `name` | yes | Human-readable name (for logs). |
| `version` | yes | Semantic version. |
| `package` | yes | Python import path (`agentix_namespaces.<name>`). **Routing key.** |
| `description` | no | Short description. |
| `kind` | no | Free-form tag for tooling; runtime ignores. |

Extra fields are allowed and preserved.

## Sandbox-side placement

After the deployment puts each namespace's content into a per-image named volume and mounts each at `/mnt/c<digest>:ro`, a sandbox sees:

```
/mnt/c<digest>/
├── store/<hash>-*/                         ← Nix deps (used by the symlink forest)
└── entry/
    ├── python/
    │   └── agentix_namespaces/<name>/        ← runtime imports this
    │       ├── __init__.py
    │       ├── _impl.py
    │       └── _register.py
    ├── bin/<cli>                           ← optional native binaries
    └── manifest.json                       ← NamespaceManifest
```

and

```
/nix/
└── store/<hash>-*/                         ← tmpfs, symlinked from /mnt/*/store/*
```

Every Nix binary's absolute `/nix/store/<hash>` reference resolves through the symlink forest. Mount dir names (`/mnt/c<digest>`) are deployment-internal; the runtime indexes namespaces by `manifest.package`, not by directory.

## Runtime lifecycle

```
Sandbox boot
    │
    ├─ tmpfs /nix
    ├─ mkdir /nix/store
    ├─ ln -sfn /mnt/*/store/*  /nix/store/
    └─ exec /mnt/runtime/entry/bin/start
           │
           └─ lifespan: scan /mnt/* (skip 'runtime')
                for each /mnt/<dir>/entry/manifest.json (valid + matching abi):
                    sys.path.insert(0, /mnt/<dir>/entry/python)
                    registry.register(pending=manifest)        # no import yet

First POST /_remote for <pkg>:
    registry.get_or_load(<pkg>):
        async with per-pkg lock:
            importlib.import_module(<pkg>)
            dispatcher = importlib.import_module("<pkg>._register").register()
            entry.dispatcher = dispatcher
    dispatcher.dispatch(request)
```

Namespaces are **fixed at sandbox create time** (the set is discovered at boot); import is **lazy** (deferred until first call per namespace). Change the set by recreating the sandbox.

## Wire

A single endpoint serves all remote calls.

```
POST /_remote
  { "package": "agentix_namespaces.my_namespace",
    "method":  "run",
    "args":    [],
    "kwargs":  { "instruction": "fix the bug" } }
```

Success:

```json
{ "ok": true, "value": { "exit_code": 0, "patch": "..." } }
```

Failure (validation, impl raises, serialization):

```json
{ "ok": false, "error": { "type": "ValueError", "message": "...", "traceback": "..." } }
```

The wire stays 200 even for impl failures — `error.type` carries the exception class name. Only "package not loaded" returns 404.

W3C `traceparent` / `tracestate` headers pass through.

## Caller side

```python
from agentix import RuntimeClient
from agentix_namespaces import my_namespace

async with RuntimeClient(sandbox.runtime_url) as c:
    result = await c.remote(my_namespace.run, instruction="...")
    # result is my_namespace.RunResult — IDE / mypy fully informed
```

`RuntimeClient.remote(fn, *args, **kwargs)`:
- Reads `fn.__module__` → routing key
- Reads `fn.__name__` → method
- Serialises args/kwargs via pydantic `TypeAdapter` from `inspect.signature(fn)`
- Decodes the response value into `fn`'s return type

No magic registration, no decorators on the stubs. The function reference and its signature carry everything the wire needs.

## Streaming returns

A stub annotated `-> AsyncIterator[T]` (or `AsyncGenerator[T, ...]`) opts into streaming. The impl is an `async def` generator:

```python
# __init__.py
from typing import AsyncIterator

def chat(prompt: str) -> AsyncIterator[Token]:
    raise NotImplementedError("call via RuntimeClient.remote(chat, ...)")

# _impl.py
async def chat(prompt: str) -> AsyncIterator[Token]:
    proc = await asyncio.create_subprocess_exec("claude", "-p", prompt, ...)
    async for line in proc.stdout:
        yield Token(text=line.decode())
```

Caller iterates directly — **no `await`**:

```python
async for token in c.remote(my_namespace.chat, prompt="..."):
    print(token.text)
```

`RuntimeClient.remote` picks the streaming or unary code path by inspecting the stub's return annotation. The wire stays at `POST /_remote`, but the response becomes `application/x-ndjson` — one JSON event per line:

```jsonl
{"item": {"text": "Hello", ...}}
{"item": {"text": " world", ...}}
{"end": true}
```

If the impl raises mid-stream, the next line is `{"error": {...}}` and the client raises `RemoteCallError`. Already-yielded items are observed before the raise.

**Not supported in v1**: streaming inputs (chunked request body), bidirectional streaming, automatic back-pressure tuning. Items are consumed by the HTTP/1.1 chunked-transfer stream; slow consumers will TCP-block the impl's `yield`.

## Runtime built-ins

Independent of any namespace, the runtime exposes:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /exec` | Run a shell command in the sandbox. Body `{command, cwd?, env?, timeout?, paths_from?}`. SSE when `Accept: text/event-stream`; else JSON `{exit_code, stdout, stderr}`. |
| `POST /upload` | Multipart upload into `AGENTIX_UPLOAD_ROOT` (default `/workspace`). |
| `GET /download?path=…` | Stream a file back. |
| `GET /namespaces` | List loaded namespaces and their manifests. |

`RuntimeClient.run / upload / download / namespaces` are typed Python helpers. Directory listing and other file inspection go through `/exec` (`ls -la`, `find`, `stat`).

### `/exec` env and PATH

Subprocesses run with a scrubbed env:

- Stripped: `LD_LIBRARY_PATH`, `LD_PRELOAD`, `PYTHONPATH`, `PYTHONHOME`, `LOCALE_ARCHIVE`, `FONTCONFIG_*`, `SSL_CERT_FILE`, anything prefixed `NIX_`.
- Default PATH: the task image's (`/usr/local/bin:/usr/bin:/bin`). Task-image tools take precedence over namespace-bundled tools of the same name.
- Opt-in to a namespace's bins with `paths_from=["agentix_namespaces.<name>"]` — prepends that namespace's `entry/bin`. `["*"]` includes every loaded namespace.

## Writing a namespace (minimal recipe)

```
my-namespace/
├── pyproject.toml          # [project] name = "agentix-namespace-my-namespace"; packages = ["agentix_namespaces/my_namespace"]
├── agentix_namespaces/
│   └── my_namespace/
│       ├── __init__.py     # stub: typed signatures, body raises NotImplementedError
│       ├── _impl.py        # real bodies
│       └── _register.py    # def register() -> Dispatcher: ...
├── manifest.json           # { "abi": 1, "package": "agentix_namespaces.my_namespace", ... }
├── default.nix             # buildPythonPackage + symlinkJoin into /entry/python + /entry/manifest.json
└── Dockerfile              # nix-build → copy /export into final image
```

Build the image with a Dockerfile that runs `nix-build` in a builder stage and copies `/export` into a `VOLUME /nix` final layer. `tests/namespace-docker/Dockerfile` in this repo is a working reference, as are `tests/namespaces/mock-agent/` and `tests/namespaces/mock-dataset/`.

Use it:

```python
SandboxConfig(
    image="ubuntu:24.04",
    runtime="agentix/runtime:0.1.0",
    namespaces=["my-namespace:1.0"],
)
```
