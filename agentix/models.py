"""Closure ABI + sandbox / deployment models.

These are the cross-cutting types: the closure-image contract (everyone
who builds or runs a closure depends on `ClosureManifest`) and the
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

# ── Closure manifest (shipped inside the closure image) ───────────

AGENTIX_CLOSURE_ABI = 1
"""Protocol version of the closure convention. Runtime ignores closures whose
manifest declares a different value. Bump on hard breaks (path layout,
manifest schema, dispatch ABI)."""


class ClosureManifest(BaseModel):
    """Static metadata shipped at `/nix/entry/manifest.json` inside a closure
    image. Presence of this file is what marks a `/mnt/<ns>` mount as an
    Agentix closure — runtime ignores anything without one.

    `package` is the Python import path the runtime imports at startup to
    obtain the closure's Dispatcher (via `<package>._register.register()`).
    """

    abi: int
    name: str
    version: str
    package: PackageName = Field(
        description=(
            "Python import path of the closure package, whatever its "
            "pyproject.toml ships (e.g. 'agentix_primitive_bash')."
        ),
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

    Convention: the distribution name is the module's import name with
    underscores replaced by dashes (the standard PEP 503 normalisation
    direction). For `agentix-…` dists the docker tag mirrors the dist
    name in the `agentix/<rest>:<version>` form. Non-`agentix-` dists
    fall through to a plain `<dist>:<version>` tag.

    Returns None when no installed distribution matches the module
    name — the caller surfaces a clear error in that case.
    """
    if not isinstance(item, types.ModuleType):
        return None
    mod_name = getattr(item, "__name__", "")
    if not mod_name:
        return None
    dist = mod_name.replace("_", "-")
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
