"""Cross-cutting metadata + sandbox / deployment models.

Namespace metadata is surfaced via `/namespaces` for introspection — no
longer shipped as an on-disk file since the runtime discovers namespaces
via `importlib.metadata` entry points. The top-level sandbox/deployment
config that orchestrators hand to a `Deployment` lives here too;
runtime transport / wire types live in `agentix.runtime.models` instead.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agentix.idents import PackageName, SandboxId


class NamespaceManifest(BaseModel):
    """Lightweight metadata for one registered namespace.

    Populated by the runtime from `importlib.metadata` at discovery time
    and returned by `GET /namespaces` for introspection. `package` is the
    namespace's Python import path (e.g. `agentix.bash`) and the runtime's
    routing key — there are no caller-chosen namespaces.
    """

    name: str
    version: str
    package: PackageName = Field(
        description="Python import path (e.g. 'agentix.bash').",
    )
    description: str | None = None

    model_config = {"extra": "allow"}


# ── Deployment ────────────────────────────────────────────────────


class SandboxConfig(BaseModel):
    """Configuration a deployment uses to provision a sandbox.

    The image is a deploy-ready bundle produced by `agentix build` —
    it carries the runtime + every namespace's Python package in one
    venv, plus any system deps under `/nix`. The deployment just runs
    it; there's no per-namespace mount-and-merge.
    """

    image: str = Field(
        description="Deploy-ready bundle image ref, e.g. `my-agent:0.1.0`.",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional env vars passed to the sandbox container (and "
            "therefore visible to the runtime + all namespaces)."
        ),
    )


class SandboxInfo(BaseModel):
    sandbox_id: SandboxId
    runtime_url: str
    status: str = "running"
