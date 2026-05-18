# Project Conventions

## Two Concepts

Agentix has exactly two ideas:

1. **Remote calls** — `c.remote(fn, *args, **kwargs)` calls an
   importable Python function inside a sandbox worker. The target is
   `fn.__module__ + "::" + fn.__name__`; the call shape (unary /
   stream / bidi) is detected from the function signature; the return
   value is decoded into `fn`'s return type.
2. **Bundle** — `agentix build [path]` packages a Python project and
   its declared dependencies into a deploy-ready Docker image. The
   project's `[project].dependencies` defines what modules are
   installed into the runtime venv.

The primary user model is:

```python
from app import run

result = await client.remote(run, input="hello")
```

`import app; await client.remote(app.run, ...)` also works because it
passes the same function object.

## Composition Over Inheritance

Use inheritance only for genuine lifecycle interfaces such as a
deployment backend implementing the `Deployment` Protocol. Everywhere
else, prefer normal functions, Protocols, composition objects, or
callbacks.

A remote target is just a Python callable serialized by stdlib pickle.
There is no base class for user code to inherit from and no marker
Protocol for users to import.

## No Backward Compatibility Shims

This repo is in active design. Breaking changes are fine.

- Rename by deleting the old name, not by accepting both.
- Do not add deprecation warnings.
- Do not leave comments explaining removed behavior.
- Update tests to the current shape; do not preserve tests for removed
  behavior.

Sibling repos (`Agentix-Runtime-Basic`, `Agentix-Deployment-*`,
`agentix-cookbook`) are updated in lockstep with HEAD.

## Systems Map

```text
agentix/
├── runtime/
│   ├── shared/              — wire types, codec, framing, event names
│   ├── client/              — RuntimeClient
│   └── server/              — FastAPI + Socket.IO + worker package
├── deployment/          — Deployment Protocol + backend plugin loader
└── cli/                 — agentix build
```

One line per system:

- **runtime.shared** — msgpack codec, length-prefixed worker frames,
  Socket.IO event names, pydantic wire models, call-shape helpers, and
  branded wire ids.
- **runtime.client** — `RuntimeClient.remote(fn, ...)`; Socket.IO for unary,
  stream, and bidi; HTTP only for health.
- **runtime.server** — `agentix-server`; owns one runtime worker process,
  invokes pickle-resolved callables, forwards Socket.IO calls, and
  correlates events by `call_id`.
- **deployment** — host-side `Deployment` Protocol and backend lookup.
- **cli** — `agentix build [path]`.

## Remote Call Implementation

`c.remote(fn, ...)` serializes `fn` with stdlib pickle and sends that
callable payload to the runtime.

Example:

```python
# my_project/tasks.py
async def run(seed: int) -> dict:
    ...

# caller
from my_project.tasks import run

result = await client.remote(run, seed=42)
```

The Socket.IO payload carries:

```python
{
    "callable_payload": b"...pickle...",
    "display_name": "my_project.tasks::run",
    "shape": "unary",
    "args": [],
    "kwargs": {"seed": 42},
    "call_id": "optional-correlation-key",
}
```

The worker unpickles the callable, validates args with pydantic, calls
the callable, and serializes the result.

## Call Shapes

Three shapes are detected from `fn`'s signature:

- `async def f(...) -> T` -> **unary**
- `async def f(...) -> AsyncIterator[T]: yield ...` -> **stream**
- `async def f(..., inbox: Channel[I]) -> AsyncIterator[T]` -> **bidi**

`c.remote(...)` returns `Unary[T]`, `Stream[T]`, or `Bidi[I, T]`.
Await unary; `async for` over stream and bidi.

Sync functions work for unary too; the invoker awaits only when the
result is awaitable. Streams and bidi require async generators.

## Bundle Implementation

`agentix build [path]` packages one project root into a deploy-ready
image. The CLI does not enumerate runtime integrations; they arrive
through pip from `[project].dependencies`.

```toml
[project]
name = "my-agent"
version = "0.1.0"
dependencies = [
    "agentixx>=0.1.0",
    "agentix-runtime-basic>=0.1.0",
    "agentix-deployment-docker>=0.1.0",
]
```

Build stages:

1. Optional Nix stage if the project has `default.nix`; system binaries
   are copied into the final image and linked under `/nix/runtime/bin`.
2. Final image from `agentix/runtime:<version>`; copy the project and
   run one `pip install /src/project` into `/nix/runtime`.

The result is one shared `/nix/runtime` venv. User code, runtime
integrations, direct dependencies, and transitive dependencies are all
importable by the worker.

## Wire Protocol

Unary uses Socket.IO:

```text
unary        {call_id, callable_payload, display_name, shape, args, kwargs}
unary:result {call_id, value}
unary:error  {call_id, error}
```

Stream and bidi use Socket.IO events:

```text
stream       {call_id, callable_payload, display_name, shape, args, kwargs}
stream:item  {call_id, value}
stream:end   {call_id}
stream:error {call_id, error}

bidi:start   {call_id, callable_payload, display_name, shape, args, kwargs}
bidi:in      {call_id, item}
bidi:end_in  {call_id}
bidi:out     {call_id, value}
bidi:end     {call_id}
bidi:error   {call_id, error}
```

Errors stay in-band: Socket.IO emits an error event for unary, stream,
and bidi.
