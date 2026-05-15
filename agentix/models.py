"""Closure metadata + sandbox / deployment models.

Cross-cutting types: closure metadata (surfaced via `/closures` for
introspection — no longer shipped as an on-disk file, since the runtime
discovers closures via `importlib.metadata` entry points) and the
top-level sandbox/deployment config that orchestrators hand to a
`Deployment`. Runtime transport / wire types live in
`agentix.runtime.models` instead.
"""

from __future__ import annotations

import importlib.metadata
import types
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agentix.idents import PackageName, SandboxId


class ClosureManifest(BaseModel):
    """Lightweight metadata for one registered closure.

    Populated by the runtime from `importlib.metadata` at discovery time
    and returned by `GET /closures` for introspection. `package` is the
    closure's Python import path (e.g. `agentix.bash`) and the runtime's
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
    image: str = Field(description="Base Docker/OCI image the sandbox runs on (the task environment)")
    runtime: str = Field(description="Runtime closure image ref")
    closures: list[str] = Field(
        default_factory=list,
        description=(
            "Closures to mount. Accepts docker image refs (strings) or any object "
            "exposing a string `__image__` attribute — typically the closure's "
            "imported Python package, e.g. `closures=[claude_code, mock_agent]`. "
            "Modules are resolved to their `__image__` at validation; the stored "
            "list is always strings. Each closure's runtime identity still comes "
            "from its manifest's `package` field — there are no caller-chosen "
            "namespaces."
        ),
    )
    env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional env vars passed to the sandbox container (and therefore "
            "visible to the runtime + all closures)."
        ),
    )

    @field_validator("closures", mode="before")
    @classmethod
    def _resolve_closure_specs(cls, v: Any) -> Any:
        """Normalize each closure spec to a docker image-ref string.

        Three acceptable inputs:

          * A raw string (passed through).
          * An object exposing a string `__image__` attribute (typically a
            closure's imported Python package — the override path).
          * A `types.ModuleType` (the closure's imported Python package).
            We derive the image from `importlib.metadata` by mapping the
            module name to a distribution name (underscore → dash) and
            looking up its version. This is the common case — closure
            authors don't have to redeclare metadata that already lives
            in `pyproject.toml`.
        """
        if not isinstance(v, list):
            return v  # pydantic will reject below
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
                continue
            img = getattr(item, "__image__", None)
            if isinstance(img, str) and img:
                out.append(img)
                continue
            derived = _derive_image_from_module(item)
            if derived is not None:
                out.append(derived)
                continue
            raise ValueError(
                f"closure spec {item!r}: cannot resolve image. Pass a "
                f"docker-image-ref string, set `__image__` on the module, "
                f"or install the closure's wheel so importlib.metadata "
                f"can derive the image from the distribution version."
            )
        return out


def _derive_image_from_module(item: Any) -> str | None:
    """Best-effort: a closure's Python module → its docker image ref.

    Convention: the distribution name is the module's import path with
    `.` → `-` and `_` → `-`. E.g. `agentix.primitive.bash` →
    `agentix-primitive-bash`. For `agentix-` dists the docker tag mirrors
    the dist name in the `agentix/<rest>:<version>` form (e.g.
    `agentix/primitive-bash:0.1.0`). Non-`agentix-` dists fall through
    to a plain `<dist>:<version>` tag.

    Returns None when no installed distribution matches — the caller
    surfaces a clear error in that case.
    """
    if not isinstance(item, types.ModuleType):
        return None
    mod_name = getattr(item, "__name__", "")
    if not mod_name:
        return None
    dist = mod_name.replace(".", "-").replace("_", "-")
    try:
        version = importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None
    if dist.startswith("agentix-"):
        return f"agentix/{dist[len('agentix-'):]}:{version}"
    return f"{dist}:{version}"


class SandboxInfo(BaseModel):
    sandbox_id: SandboxId
    runtime_url: str
    status: str = "running"
