"""`agentix build` — build a namespace image (one spec) or bundle (many).

Usage:

    agentix build primitives/bash                              # one namespace, auto-tag
    agentix build bash                                         # short name → primitives/bash
    agentix build primitives/bash --tag my-bash:dev            # one namespace, explicit tag
    agentix build bash files claude-code -o my-agent:0.1.0     # bundle several namespaces
    agentix build bash files --dry-run                         # stage to ./build/<tag>/

Each spec is one of:

  1. **Path:** a directory containing `pyproject.toml` and an
     `src/agentix/<name>/` Python package — used as-is.
  2. **Image ref:** a string with a `:` AND a `/` — treated as a
     pre-built namespace image and pulled at bundle build time.
  3. **Short name:** searched against `primitives/<name>/` (relative
     to the repo root). If none match, falls back to PyPI as
     `agentix-<name>` (currently stubbed).

Image-ref and PyPI paths aren't fully wired yet — they raise
NotImplementedError with a clear message. The local-path / short-name
case works end-to-end today, which covers the in-repo dev flow.

With one spec and no `--output / --tag`, the output image tag is
auto-derived from the namespace's pyproject (`agentix-<short>:<version>`
→ `agentix/<short>:<version>`). With multiple specs `--output` is
required.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from agentix.cli._resolve import REPO_ROOT, NamespaceSpec, read_pyproject, resolve_spec

TEMPLATE_DIR = REPO_ROOT / "primitives" / "_template"

_SOURCE_SKIP = {
    "__pycache__", ".venv", "build", "dist", ".git",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
}


def _derive_tag_from_pyproject(pyproject: dict) -> str:
    """`agentix-bash` v `0.1.0` → `agentix/bash:0.1.0`."""
    project = pyproject.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SystemExit("pyproject.toml: [project] needs string name + version")
    if not name.startswith("agentix-"):
        raise SystemExit(
            f"pyproject.toml: name {name!r} must follow `agentix-<short>`"
        )
    return f"agentix/{name[len('agentix-'):]}:{version}"


def _resolve_path(spec: NamespaceSpec) -> Path:
    """Return the on-disk source dir for a spec, raising on unsupported kinds."""
    if spec.kind == "path":
        assert spec.path is not None
        return spec.path
    if spec.kind == "pypi":
        raise NotImplementedError(
            f"`agentix build {spec.short}`: PyPI sourcing not wired yet. "
            f"Needs a `pip download {spec.pypi_dist}` + wheel unpack step "
            f"before the source dir is ready. Use a local path instead."
        )
    raise SystemExit(
        f"`agentix build {spec.short}`: image refs aren't a valid input — "
        f"that image already exists. Use `agentix deploy local --image {spec.image_ref}`."
    )


def _stage(specs: list[tuple[NamespaceSpec, Path]], build_dir: Path) -> None:
    """Stage one or many namespaces into a docker build context.

    Layout:
      build_dir/
      ├── Dockerfile         # generated; same shape for N=1 and N>1
      ├── default.nix        # shared nix derivation
      └── <short>/           # one per spec — the namespace project as-is
          ├── pyproject.toml
          └── src/...
    """
    for spec, src in specs:
        dest = build_dir / spec.short
        dest.mkdir()
        for item in src.iterdir():
            if item.name in _SOURCE_SKIP or item.name.endswith(".egg-info"):
                continue
            d = dest / item.name
            if item.is_dir():
                shutil.copytree(item, d, ignore=shutil.ignore_patterns(*_SOURCE_SKIP))
            else:
                shutil.copy2(item, d)
    shutil.copy2(TEMPLATE_DIR / "default.nix", build_dir / "default.nix")
    (build_dir / "Dockerfile").write_text(_render_dockerfile([s for s, _ in specs]))


def _render_dockerfile(specs: list[NamespaceSpec]) -> str:
    """Multi-stage Dockerfile: build each namespace's nix derivation in a
    builder stage, copy each derivation's full store-path closure into
    /nix/store, ship a thin busybox image with VOLUME /nix.

    Works uniformly for N=1 and N>1 — there's no separate "bundle" shape.
    The runtime discovers every namespace via `importlib.metadata` at
    startup once the Nix store is symlink-merged into /nix/store.
    """
    builder_steps = "\n".join(
        f"WORKDIR /src/{spec.short}\n"
        f"COPY {spec.short}/ ./\n"
        f"COPY default.nix ./\n"
        f"RUN nix-build --no-out-link default.nix -o ./result && \\\n"
        f"    STORE_PATH=$(readlink -f ./result) && \\\n"
        f"    for p in $(nix-store -qR \"$STORE_PATH\"); do \\\n"
        f"        cp -a \"$p\" /export/nix/store/; \\\n"
        f"    done"
        for spec in specs
    )
    shorts = " ".join(spec.short for spec in specs)
    return f"""\
# Generated by `agentix build`. Do not hand-edit.
ARG NIX_IMAGE=nixos/nix:latest

FROM ${{NIX_IMAGE}} AS builder
RUN mkdir -p ~/.config/nix && \\
    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \\
    nix-channel --update 2>/dev/null || true
RUN mkdir -p /export/nix/store

{builder_steps}

FROM busybox:stable
COPY --from=builder /export /
VOLUME /nix
LABEL org.agentix.namespace=1
LABEL org.agentix.namespace.names="{shorts}"
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix build",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "specs", nargs="+",
        help="one or more namespace short names, paths, or image refs",
    )
    parser.add_argument(
        "-o", "--output", "--tag", dest="output", default=None,
        help="output docker image tag. Required when building >1 namespace; "
             "auto-derived from pyproject for a single namespace.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="stage to ./build/<tag>/ and print path; do NOT invoke docker",
    )
    args = parser.parse_args(argv)

    try:
        specs = [resolve_spec(s) for s in args.specs]
        # Disallow duplicate short names — would collide both in build_dir
        # and at runtime registration.
        shorts = [s.short for s in specs]
        dupes = {n for n in shorts if shorts.count(n) > 1}
        if dupes:
            raise SystemExit(f"duplicate namespace short names: {sorted(dupes)}")

        resolved: list[tuple[NamespaceSpec, Path]] = [
            (spec, _resolve_path(spec)) for spec in specs
        ]
    except NotImplementedError as exc:
        raise SystemExit(f"error: {exc}") from exc

    # Tag resolution. One spec → derive from pyproject if -o omitted.
    # Multi-spec → -o is required.
    if args.output:
        tag = args.output
    elif len(specs) == 1:
        tag = _derive_tag_from_pyproject(read_pyproject(resolved[0][1]))
    else:
        raise SystemExit("--output / --tag is required when building >1 namespace")

    if ":" not in tag:
        raise SystemExit(f"image tag must include `:<version>` (got {tag!r})")

    if args.dry_run:
        out = REPO_ROOT / "build" / tag.rsplit("/", 1)[-1].split(":", 1)[0]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        _stage(resolved, out)
        print(f"staged build context → {out}")
        print(f"would build → {tag}")
        if len(specs) > 1:
            print(f"  namespaces: {', '.join(shorts)}")
        return 0

    with TemporaryDirectory(prefix="agentix-build-") as tmp:
        build_dir = Path(tmp)
        _stage(resolved, build_dir)
        print(f"building {tag} ({len(specs)} namespace{'s' if len(specs) > 1 else ''})…", file=sys.stderr)
        proc = subprocess.run(
            ["docker", "build", "-t", tag, str(build_dir)],
            check=False,
        )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
