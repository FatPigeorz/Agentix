"""Spec resolution — internal helper for `agentix build`.

A *spec* is whatever the user types on the command line: a short name
(`bash`), a relative path (`./primitives/bash`), or an image reference
(`docker.io/me/agent:0.1.0`). `resolve_spec` walks a hardcoded list of
built-in resolvers in priority order and returns the first non-`None`
answer.

This is **not** a plugin axis. The four resolvers below cover every
spec shape the CLI accepts; a new shape means editing this file, not
shipping a wheel.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class NamespaceSpec:
    """One resolved input to a build / install. Exactly one source field is set."""

    short: str
    kind: Literal["path", "pypi", "image"]
    path: Path | None = None
    pypi_dist: str | None = None
    image_ref: str | None = None


def read_pyproject(namespace_dir: Path) -> dict:
    pp = namespace_dir / "pyproject.toml"
    if not pp.is_file():
        raise SystemExit(f"{namespace_dir}: missing pyproject.toml")
    with pp.open("rb") as f:
        return tomllib.load(f)


# ── Built-in resolvers ──────────────────────────────────────────────


def _short_from_pyproject(pyproject: dict) -> str:
    """`agentix-bash` → `bash`."""
    name = pyproject.get("project", {}).get("name", "")
    if not isinstance(name, str) or not name.startswith("agentix-"):
        raise SystemExit(
            f"pyproject.toml: name {name!r} must start with `agentix-`"
        )
    return name[len("agentix-"):]


def _short_from_image(ref: str) -> str:
    """`docker.io/me/agentix/bash:0.1.0` → `bash`."""
    last = ref.rsplit("/", 1)[-1].rsplit(":", 1)[0]
    return last[len("agentix-"):] if last.startswith("agentix-") else last


def _resolve_path(spec: str) -> NamespaceSpec | None:
    """Treat explicit-path strings and existing source dirs as namespace sources."""
    if spec.startswith((".", "/")):
        p = Path(spec).resolve()
        if not (p / "pyproject.toml").is_file():
            raise SystemExit(f"{spec}: no pyproject.toml — not a namespace source dir")
        return NamespaceSpec(short=_short_from_pyproject(read_pyproject(p)), kind="path", path=p)
    p = Path(spec)
    if p.is_dir() and (p / "pyproject.toml").is_file():
        return NamespaceSpec(
            short=_short_from_pyproject(read_pyproject(p.resolve())),
            kind="path", path=p.resolve(),
        )
    return None


def _resolve_image(spec: str) -> NamespaceSpec | None:
    """`host/path:tag` strings — pre-built image references."""
    if "/" in spec and ":" in spec and not spec.startswith((".", "/")):
        return NamespaceSpec(short=_short_from_image(spec), kind="image", image_ref=spec)
    return None


def _resolve_local_repo(spec: str) -> NamespaceSpec | None:
    """Short names looked up under the repo's `primitives/<name>/` tree."""
    candidate = REPO_ROOT / "primitives" / spec
    if candidate.is_dir() and (candidate / "pyproject.toml").is_file():
        return NamespaceSpec(short=spec, kind="path", path=candidate)
    return None


def _resolve_pypi_fallback(spec: str) -> NamespaceSpec:
    """Last-chance: assume the bare name is a published PyPI dist."""
    return NamespaceSpec(short=spec, kind="pypi", pypi_dist=f"agentix-{spec}")


# Ordered most-specific to least-specific. `resolve_spec` returns the
# first non-None answer; the PyPI fallback always claims, so the chain
# is total.
_RESOLVERS = (_resolve_path, _resolve_image, _resolve_local_repo, _resolve_pypi_fallback)


def resolve_spec(spec: str) -> NamespaceSpec:
    """Walk every built-in resolver in priority order; first match wins."""
    for resolver in _RESOLVERS:
        result = resolver(spec)
        if result is not None:
            return result
    raise SystemExit(f"no resolver claimed {spec!r}")  # unreachable; pypi fallback claims


__all__ = ["REPO_ROOT", "NamespaceSpec", "read_pyproject", "resolve_spec"]
