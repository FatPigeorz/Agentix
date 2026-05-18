"""Deployment Protocol + plugin registry.

A deployment backend is anything that creates / deletes / inspects a
sandbox. The framework treats them as plugins: each backend is a class
registered under the `agentix.deployment` entry-point group. Builtin
`local` / `daytona` / `e2b` are registered in the framework's own
`pyproject.toml`; third parties (`agentix-deployment-fly`, …) just
declare their own entry point and `pip install` makes them available
to `agentix deploy <name>`.

```toml
# downstream pyproject.toml
[project.entry-points."agentix.deployment"]
fly = "agentix_deployment_fly:FlyDeployment"
```

```python
# downstream module
from agentix.deployment import Deployment   # Protocol

class FlyDeployment:                          # no inheritance, structural type
    async def create(self, cfg): ...
    async def delete(self, sid): ...
    async def get(self, sid): ...
```

`agentix deploy fly --image my-agent:0.1.0` works after the install
with zero framework changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import NewType, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agentix.deployment._plugin import Registry

SandboxId = NewType("SandboxId", str)
"""Deployment-side handle for a running sandbox container. Returned by
`Deployment.create(...)` and threaded back through `delete(...)` /
`get(...)`."""


class SandboxConfig(BaseModel):
    """Configuration a deployment uses to provision a sandbox.

    The image is a deploy-ready bundle produced by `agentix build` —
    it carries the runtime + every pip-installed plugin in one venv,
    plus any system deps under `/nix`. The deployment just runs it.
    """

    image: str = Field(
        description="Deploy-ready bundle image ref, e.g. `my-agent:0.1.0`.",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description="Optional env vars passed to the sandbox container.",
    )


class SandboxInfo(BaseModel):
    sandbox_id: SandboxId
    runtime_url: str
    status: str = "running"


@dataclass
class Sandbox:
    """Live sandbox handle — `runtime_url` is what `RuntimeClient` connects to."""

    sandbox_id: SandboxId
    runtime_url: str
    status: str


@runtime_checkable
class Deployment(Protocol):
    """Sandbox lifecycle management. Structural type — backends don't
    inherit, they just implement the three methods.

    Backends are typically classes registered as entry points; the
    framework instantiates them with no arguments via `load_deployment`,
    so any backend-specific configuration (API keys, regions, …) is
    read from environment variables in the backend's `__init__`.
    """

    async def create(self, config: SandboxConfig) -> Sandbox: ...
    async def delete(self, sandbox_id: SandboxId) -> None: ...
    async def get(self, sandbox_id: SandboxId) -> SandboxInfo: ...


# The plugin registry — one `agentix.deployment` group. Builtin
# backends are registered in agentix's own pyproject.toml; downstream
# dists add their own. Tests can also `register_deployment("fake", ...)`
# imperatively via the public helper below.
_deployments: Registry[type[Deployment]] = Registry("agentix.deployment")


def register_deployment(name: str, cls: type[Deployment]) -> None:
    """In-process deployment registration. Test / dynamic use only —
    production deployments are declared in their dist's `pyproject.toml`
    `[project.entry-points."agentix.deployment"]`."""
    _deployments.register(name, lambda: cls)


def load_deployment(name: str) -> type[Deployment]:
    """Return the deployment class registered under `name`.

    Raises `KeyError` (with available names) if no backend claims that
    name, or re-raises the loader's exception if the backend's import
    fails.
    """
    return _deployments.get(name)


def deployments() -> Registry[type[Deployment]]:
    """The underlying registry — for tests and introspection."""
    return _deployments


@asynccontextmanager
async def session(
    deployment: Deployment, config: SandboxConfig,
) -> AsyncIterator[Sandbox]:
    """Scoped sandbox: created on entry, deleted on exit.

    Free function instead of a Deployment method so the Protocol stays
    minimal (three methods); structural backends don't have to inherit
    a helper class.
    """
    sandbox = await deployment.create(config)
    try:
        yield sandbox
    finally:
        await deployment.delete(sandbox.sandbox_id)
