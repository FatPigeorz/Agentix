"""`agentix build` — build a deploy-ready bundle image.

Usage:

    agentix build primitives/bash                              # one namespace, auto-tag
    agentix build bash                                         # short name → primitives/bash
    agentix build primitives/bash -o my-bash:dev               # explicit tag
    agentix build bash files claude-code -o my-agent:0.1.0     # bundle several namespaces
    agentix build bash files --dry-run -o sandbox:dev          # stage to ./build/<tag>/

Each spec is one of:

  1. **Path:** a directory containing `pyproject.toml` and the namespace's
     Python package under `src/agentix/<name>/`.
  2. **Image ref:** pre-built — `:` AND `/` (not yet wired).
  3. **Short name:** searched against `primitives/<name>/` in the repo,
     falling back to PyPI `agentix-<name>` (currently stubbed).

Build shape — every namespace gets its own venv under `/nix/<short>/`:

  Python deps **per namespace**: each namespace is pip-installed into its
  own `/nix/<short>/` (uv-managed) so two namespaces can pull
  incompatible dep versions without conflict. The multiplexer spawns a
  worker subprocess using each namespace's venv interpreter and prepends
  `/nix/<short>/bin` to PATH — `subprocess.run("git", ...)` in user code
  resolves transparently.

  System deps **per namespace**: a namespace that needs native binaries
  (claude CLI, git, …) ships a `default.nix` next to its `pyproject.toml`.
  If any spec ships one, a Nix builder stage runs first; the derivation's
  `bin/*` is then symlinked into the namespace's `/nix/<short>/bin/`.
  Bundles with no `default.nix` anywhere skip Nix entirely.

  The output image extends `agentix/runtime:<framework-version>` (override
  via `--runtime-image`). If that image isn't present locally, `agentix
  build` auto-builds it from `primitives/_template/Dockerfile` — users
  don't run `docker build` directly.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from agentix import __version__ as FRAMEWORK_VERSION
from agentix.cli._resolve import REPO_ROOT, NamespaceSpec, read_pyproject, resolve_spec

_RUNTIME_DOCKERFILE = REPO_ROOT / "primitives" / "_template" / "Dockerfile"

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
    if spec.kind == "path":
        assert spec.path is not None
        return spec.path
    if spec.kind == "pypi":
        raise NotImplementedError(
            f"`agentix build {spec.short}`: PyPI sourcing not wired yet. "
            f"Use a local path instead."
        )
    raise SystemExit(
        f"`agentix build {spec.short}`: image refs aren't a valid input — "
        f"that image already exists. Use `agentix deploy local --image {spec.image_ref}`."
    )


def _has_system_deps(src: Path) -> bool:
    """True if the namespace ships a `default.nix` next to its pyproject."""
    return (src / "default.nix").is_file()


def _image_exists_locally(image: str) -> bool:
    """`docker image inspect` returns 0 iff the image is locally present."""
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _ensure_runtime_image(runtime_image: str) -> None:
    """Build the runtime image from primitives/_template/Dockerfile if missing.

    Auto-build keeps `agentix build` a single user-facing command — no
    side-trip to `docker build -f primitives/_template/Dockerfile .`. The
    runtime image rarely changes (only when the framework version bumps);
    subsequent calls skip the build entirely.
    """
    if _image_exists_locally(runtime_image):
        return
    if not _RUNTIME_DOCKERFILE.is_file():
        raise SystemExit(
            f"runtime image {runtime_image} not found locally and template "
            f"Dockerfile is missing at {_RUNTIME_DOCKERFILE}"
        )
    print(
        f"runtime image {runtime_image!r} not found locally; building from "
        f"{_RUNTIME_DOCKERFILE.relative_to(REPO_ROOT)} (one-time)…",
        file=sys.stderr,
    )
    proc = subprocess.run(
        [
            "docker", "build",
            "-t", runtime_image,
            "-f", str(_RUNTIME_DOCKERFILE),
            str(REPO_ROOT),
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to build runtime image {runtime_image!r} "
            f"(docker build returned {proc.returncode})"
        )


def _stage(specs: list[tuple[NamespaceSpec, Path]], build_dir: Path) -> None:
    """Copy each namespace's project tree into `build_dir/<short>/`."""
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


def _render_dockerfile(
    specs: list[tuple[NamespaceSpec, Path]],
    runtime_image: str,
) -> str:
    """Multi-stage Dockerfile.

    Stage 1 (optional): Nix builder for any namespace that ships
    `default.nix`. Skipped entirely if no spec does.

    Stage 2: extends the runtime image. For each namespace, create
    `/nix/<short>/` via uv and `pip install` the namespace + the
    bundled framework wheel into it. If the namespace shipped a
    `default.nix`, symlink the derivation's `bin/*` into
    `/nix/<short>/bin/` so the namespace's worker subprocess (with
    PATH=/nix/<short>/bin:…) can invoke them transparently.
    """
    nix_specs = [(s, p) for s, p in specs if _has_system_deps(p)]
    nix_shorts = {s.short for s, _ in nix_specs}

    parts: list[str] = ["# Generated by `agentix build`. Do not hand-edit."]

    if nix_specs:
        parts += [
            "ARG NIX_IMAGE=nixos/nix:latest",
            "",
            "FROM ${NIX_IMAGE} AS sys-builder",
            "RUN mkdir -p ~/.config/nix && \\",
            "    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \\",
            "    nix-channel --update 2>/dev/null || true",
            "RUN mkdir -p /export/nix/store /export/nix/.sys-paths",
        ]
        for spec, _ in nix_specs:
            # Each namespace's derivation closure goes into /export/nix/store;
            # the derivation's own store path is stashed at
            # /export/nix/.sys-paths/<short> so the final stage knows where
            # to read the binaries from for the symlink step.
            parts += [
                "",
                f"WORKDIR /src/{spec.short}",
                f"COPY {spec.short}/ ./",
                "RUN nix-build --no-out-link default.nix -o ./result && \\",
                "    STORE_PATH=$(readlink -f ./result) && \\",
                "    for p in $(nix-store -qR \"$STORE_PATH\"); do \\",
                "        cp -a \"$p\" /export/nix/store/ || true; \\",
                "    done && \\",
                f"    echo \"$STORE_PATH\" > /export/nix/.sys-paths/{spec.short}",
            ]

    parts += ["", f"FROM {runtime_image}"]
    if nix_specs:
        parts.append("COPY --from=sys-builder /export/nix/store /nix/store")
        parts.append("COPY --from=sys-builder /export/nix/.sys-paths /nix/.sys-paths")

    # One venv per namespace under /nix/<short>/. uv venv is millisecond-scale;
    # each `pip install` runs in its own venv so cross-namespace dep versions
    # don't merge. The framework wheel is the only shared dep — installed
    # into every venv from /nix/.wheels/ stashed by the runtime image.
    for spec, _ in specs:
        steps = [
            f"COPY {spec.short}/ /src/{spec.short}/",
            f"RUN uv venv /nix/{spec.short} && \\",
            f"    /nix/{spec.short}/bin/pip install --no-cache-dir "
            f"/nix/.wheels/agentix-*.whl /src/{spec.short}",
        ]
        # If this namespace shipped default.nix, symlink the derivation's
        # bin/* into the namespace's bin/. Worker PATH points at this dir
        # so user code can call `subprocess.run("git", ...)` directly.
        if spec.short in nix_shorts:
            steps[-1] += " && \\"
            steps += [
                f"    SP=$(cat /nix/.sys-paths/{spec.short}) && \\",
                "    for f in $SP/bin/*; do \\",
                f"        ln -sf \"$f\" /nix/{spec.short}/bin/$(basename \"$f\"); \\",
                "    done",
            ]
        parts += steps

    short_names = " ".join(s.short for s, _ in specs)
    parts.append(f'LABEL org.agentix.bundle.namespaces="{short_names}"')

    return "\n".join(parts) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix build",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("specs", nargs="+",
                        help="one or more namespace short names, paths, or image refs")
    parser.add_argument(
        "-o", "--output", "--tag", dest="output", default=None,
        help="output docker image tag. Required when building >1 namespace; "
             "auto-derived from pyproject for a single namespace.",
    )
    parser.add_argument(
        "--runtime-image", default=f"agentix/runtime:{FRAMEWORK_VERSION}",
        help="base runtime image the bundle extends. Auto-built from "
             "primitives/_template/Dockerfile if not present locally. "
             f"(default: agentix/runtime:{FRAMEWORK_VERSION})",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="stage to ./build/<tag>/ and print path; do NOT invoke docker")
    args = parser.parse_args(argv)

    try:
        specs = [resolve_spec(s) for s in args.specs]
        shorts = [s.short for s in specs]
        dupes = {n for n in shorts if shorts.count(n) > 1}
        if dupes:
            raise SystemExit(f"duplicate namespace short names: {sorted(dupes)}")
        resolved = [(spec, _resolve_path(spec)) for spec in specs]
    except NotImplementedError as exc:
        raise SystemExit(f"error: {exc}") from exc

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
        (out / "Dockerfile").write_text(_render_dockerfile(resolved, args.runtime_image))
        print(f"staged build context → {out}")
        print(f"would build → {tag}")
        print(f"  extends → {args.runtime_image}")
        if len(specs) > 1:
            print(f"  namespaces: {', '.join(shorts)}")
        return 0

    # Ensure the runtime image exists locally before generating the bundle —
    # the bundle Dockerfile's `FROM agentix/runtime:<version>` would fail
    # otherwise. Auto-build hides the docker-build call from users.
    _ensure_runtime_image(args.runtime_image)

    with TemporaryDirectory(prefix="agentix-build-") as tmp:
        build_dir = Path(tmp)
        _stage(resolved, build_dir)
        (build_dir / "Dockerfile").write_text(_render_dockerfile(resolved, args.runtime_image))
        ns_count = len(specs)
        print(
            f"building {tag} ({ns_count} namespace{'s' if ns_count > 1 else ''} "
            f"extending {args.runtime_image})…",
            file=sys.stderr,
        )
        proc = subprocess.run(
            ["docker", "build", "-t", tag, str(build_dir)],
            check=False,
        )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
