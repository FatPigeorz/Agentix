# Roadmap

Agentix keeps two user-facing concepts:

- **Remote calls.** `c.remote(fn, ...)` calls a callable target inside a
  sandbox. The callable is serialized with stdlib pickle, and the call
  shape is detected from its signature.
- **Bundle.** `agentix build [path]` packages one project root and its
  declared dependencies into a deploy-ready runtime image.

Everything below should preserve that surface. Internal worker topology,
transport choice, and deployment backend details should remain opaque to
downstream users of the library.

## v0.1.0 — RPC + Bundle

Current architecture:

- [x] `RuntimeClient.remote(fn, ...)` for unary, stream, and bidi calls.
- [x] One runtime server per sandbox image.
- [x] One worker subprocess per runtime server.
- [x] Pickle callable payloads for Python-native callable references.
- [x] Callable invocation inside `agentix.runtime.server`; targets are not
      required to be pure functions. If Python can resolve the callable
      from the requested target, Agentix should be able to invoke it.
- [x] Single-spec `agentix build`; integrations arrive through normal
      Python dependencies.
- [x] One merged `/nix/runtime` venv containing the framework, user
      project, integrations, and transitive dependencies.
- [x] Deployment backend plugin axis via `agentix.deployment`.

The single-worker model is intentional for now. It keeps runtime state
and debugging simple while the public API is still being shaped.

## Architectural Direction

### Worker Model

Keep one worker process as the default near-term runtime model.

Future improvements may add:

- worker pools
- per-call worker isolation
- concurrency limits
- CPU-bound call offloading
- restart and health policies

These changes must be opaque to downstream users. Code written as:

```python
result = await client.remote(run, input="hello")
```

should not change if the runtime later moves from one worker to many
workers.

### Callable Targets

Agentix should not require targets to be pure functions.

The runtime may call any resolved callable target, including callables
that close over module state, mutate sandbox-local state, call CLIs,
read/write files, or interact with benchmark harnesses. Purity is a user
or integration concern, not a framework constraint.

The framework's responsibility is narrower:

- serialize the callable with stdlib pickle
- unpickle and invoke it inside the sandbox
- validate/coerce inputs and outputs through annotations when present
- surface errors in-band through the runtime protocol

### Transport Strategy

Remote calls use one Socket.IO connection for unary, stream, and bidi.
HTTP is kept only for `/health`.

This gives the runtime one correlation, cancellation, and error path for
all call shapes. Future trace/log event fan-out should share the same
connection rather than adding another transport.

Remaining transport work:

- collapse the separate unary / stream / bidi event names into a single
  `call:*` event family if the current naming becomes noisy
- let the worker classify actual return values at runtime instead of
  relying only on pre-call shape detection

## Sibling Repos

Sibling repos are updated in lockstep with Agentix HEAD while the design
is still moving quickly.

- [`Agentix-Runtime-Basic`](https://github.com/Agentiix/Agentix-Runtime-Basic)
  — `bash` and `files` modules. Published as `agentix-runtime-basic`.
- [`Agentix-Deployment-Docker`](https://github.com/Agentiix/Agentix-Deployment-Docker)
  — local Docker backend. Published as `agentix-deployment-docker`.
- [`Agentix-Deployment-Daytona`](https://github.com/Agentiix/Agentix-Deployment-Daytona)
  and [`Agentix-Deployment-E2B`](https://github.com/Agentiix/Agentix-Deployment-E2B)
  — hosted deployment backends.
- [`abridge`](https://github.com/Agentiix/abridge) — host-side
  rollout-to-RL-buffer bridge.

## Later

Future directions, listed so the framework can avoid architectural
dead-ends without expanding the current API prematurely.

- **Trace pub/sub** — remote functions emit structured rollout events;
  subscribers receive rollout-scoped fan-out.
- **RolloutPool** — warm sandbox pool for batched RL rollouts.
- **LLM proxy** — transparent proxy for API calls from remote functions,
  enabling token-level trajectory capture, cost tracking, and replay.
- **Checkpoint / partial rollout** — snapshot a sandbox filesystem and
  loaded runtime state, then fork to explore alternative continuations.
- **K8s deployment backend** — `Deployment` implementation using the
  same bundle-image contract, likely shipping as `agentix-deployment-k8s`.
