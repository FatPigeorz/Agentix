"""Shared models for Agentix.

Closures are static per sandbox: deployment mounts each closure under /mnt,
runtime scans /mnt, imports each closure's declared Python package, and
binds its dispatcher in-process. No subprocesses, no UDS, no /load, no
caller-chosen namespaces — the Python package path is the unique identity.

Wire: `POST /_remote` body RemoteRequest -> RemoteResponse.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

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
    package: str = Field(
        description="Python import path of the closure package, e.g. 'agentix_closures.claude_code'."
    )
    description: str | None = None

    model_config = {"extra": "allow"}


# ── Runtime server wire types ─────────────────────────────────────


class ClosureInfo(BaseModel):
    """One entry in GET /closures. `manifest.package` is the closure identity;
    `path` is the on-disk mount, exposed for debugging only.
    """

    path: str
    manifest: ClosureManifest


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


# ── Remote-call wire ─────────────────────────────────────────────


class RemoteRequest(BaseModel):
    """POST /_remote body. `package` is the closure's Python import path
    (e.g. 'agentix_closures.claude_code'); `method` is a stub name bound
    by that closure's Dispatcher.
    """

    package: str
    method: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class RemoteError(BaseModel):
    type: str
    message: str
    traceback: str | None = None


class RemoteResponse(BaseModel):
    """POST /_remote response."""

    ok: bool
    value: Any = None
    error: RemoteError | None = None


class LogRecord(BaseModel):
    """One log line forwarded over the Socket.IO `log` event.

    Subscribers receive these whenever the runtime (or any closure logger
    under the `agentix.*` tree) emits a logging record.
    """

    level: str          # e.g. "INFO", "WARNING"
    name: str           # logger name
    message: str        # formatted message
    timestamp: float    # record.created — seconds since epoch


# ── Runtime I/O primitives (exec / upload / download) ────────────


class ExecRequest(BaseModel):
    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: float | None = None
    max_output: int | None = Field(
        default=None,
        description="Cap on stdout/stderr bytes for buffered exec. Default: 10 MiB.",
    )
    paths_from: list[str] | None = Field(
        default=None,
        description=(
            "Python package paths of loaded closures whose `bin/` should be prepended "
            "to PATH for this command. Default: PATH is the task image's default, "
            "closure bins do not shadow it. Use ['agentix_closures.<name>'] or ['*'] "
            "when you explicitly want a closure's tools on PATH."
        ),
    )


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class UploadResponse(BaseModel):
    path: str
    size: int


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

    @field_validator("closures", mode="before")
    @classmethod
    def _resolve_closure_specs(cls, v: Any) -> Any:
        """Accept ``list[str | <obj with __image__>]`` and normalise to list[str]."""
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
            raise ValueError(
                f"closure spec {item!r} must be a docker-image-ref string or "
                f"an object with a non-empty string `__image__` attribute "
                f"(e.g. a closure's Python package module)"
            )
        return out
    env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional env vars passed to the sandbox container (and therefore "
            "visible to the runtime + all closures)."
        ),
    )


class SandboxInfo(BaseModel):
    sandbox_id: str
    runtime_url: str
    status: str = "running"
