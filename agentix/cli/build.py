"""`agentix build` — build a deploy-ready bundle image.

Usage:

    agentix build .                                              # current project, auto-tag
    agentix build . -o my:dev                                    # explicit tag
    agentix build . ../some-plugin -o my:0.1.0                   # your project + a plugin
    agentix build . --dry-run -o sandbox:dev                     # stage only, no docker invoke
    agentix build . ../clashing-deps --isolated -o my:dev        # per-namespace venvs

Each spec is one of:

  1. **Path:** a directory containing `pyproject.toml`. Works whether
     or not the project declares an `agentix.namespace` entry point —
     plugin projects declare one for `from agentix import <short>`
     uniformity; user projects don't need to.
  2. **Image ref:** pre-built — `:` AND `/` (not yet wired).
  3. **Short name:** assumed to be a PyPI dist `agentix-<name>`
     (currently stubbed — wheel-based builds aren't wired yet).

Build modes (the `--isolated` flag flips between them):

  * **Merged (default).** Every spec + its declared deps install into
    the framework's own venv at `/nix/runtime/`. One Python, one
    site-packages, one bin/ — `from agentix.bash import run` inside
    your worker just works, and any Nix-built binary (`claude`, `git`,
    …) is on PATH for every worker. Cost: if two specs need
    incompatible Python deps, pip's resolver fails the build with a
    clear error. The CLI suggests `--isolated` when that happens.

  * **Isolated (`--isolated`).** Each spec gets `/nix/<short>/` with
    its own uv-managed venv and its own bin/. Conflicting deps don't
    merge; namespaces stop being able to inline-import each other.
    The only cross-namespace path is host-side `c.remote(...)`. Use
    when merged mode fails or when you genuinely need per-namespace
    dep isolation.

  System deps **per namespace** are unchanged: a namespace that needs
  native binaries (claude CLI, git, …) ships a `default.nix` next to
  its `pyproject.toml`. A Nix builder stage runs first; the
  derivation's `bin/*` is symlinked into `/nix/runtime/bin/` (merged)
  or `/nix/<short>/bin/` (isolated). Bundles with no `default.nix`
  anywhere skip Nix entirely.

  The output image extends `agentix/runtime:<framework-version>`
  (override via `--runtime-image`). The runtime image must exist
  locally; build it from `Agentix-Runtime-Basic/runtime/Dockerfile`
  or pull it from your registry.
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


def _warn_merged_with_system_deps(
    resolved: list[tuple[NamespaceSpec, Path]],
    *,
    isolated: bool,
) -> None:
    """Build-time warning: in merged mode, code that inline-wraps a
    plugin requiring system deps depends on the merged-bin/ collapse.

    If the user later switches to `--isolated`, their inline wrapper
    will silently break — `subprocess.run(['claude', ...])` will fail
    to find the binary because per-namespace bin/ isn't on the user's
    worker PATH any more. Flagging this at build time so the user
    knows what merge buys them (and what they'd lose).
    """
    if isolated:
        return
    sys_dep_plugins = [spec.short for spec, p in resolved if _has_system_deps(p)]
    if not sys_dep_plugins:
        return
    names = ", ".join(sys_dep_plugins)
    print(
        f"\nnote: building in merged mode. Plugin(s) shipping system "
        f"binaries via `default.nix` ({names}) install their bins into "
        f"the shared /nix/runtime/bin/. Inline composition "
        f"(`from agentix.{sys_dep_plugins[0]} import …` from any worker) "
        f"works because those binaries are on every worker's PATH.\n"
        f"Trade-offs:\n"
        f"  * two plugins shipping a binary with the same name collide "
        f"(last-write wins).\n"
        f"  * code that inline-wraps these plugins will NOT work if you "
        f"later rebuild with `--isolated`; cross-namespace composition "
        f"must go through host-side `c.remote(...)` in that mode.\n",
        file=sys.stderr,
    )


def _image_exists_locally(image: str) -> bool:
    """`docker image inspect` returns 0 iff the image is locally present."""
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _ensure_runtime_image(runtime_image: str) -> None:
    """Verify the runtime base image exists locally.

    The base image used to be auto-built from
    `primitives/_template/Dockerfile`, but that template ships with
    `agentix-runtime-basic` now (under its `runtime/Dockerfile`).
    Users either pull the image from a registry or build it from
    that repo:

        docker build -t agentix/runtime:<version> \\
            -f /path/to/Agentix-Runtime-Basic/runtime/Dockerfile .
    """
    if _image_exists_locally(runtime_image):
        return
    raise SystemExit(
        f"runtime image {runtime_image!r} not found locally. Build it from "
        f"Agentix-Runtime-Basic (`runtime/Dockerfile`) or pull it from your "
        f"registry, then re-run `agentix build`."
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
    isolated: bool = False,
) -> str:
    """Generate the bundle's multi-stage Dockerfile.

    Two layout modes:

      * **Merged (default).** Every spec + its declared deps install
        into the framework's own venv at `/nix/runtime/`. One Python,
        one `site-packages`, one `bin/` — so `from agentix.bash import
        run` inside your worker just works, and `claude` is on PATH for
        any worker that has `agentix-claude-code` declared. The Python
        composition story is "regular Python — `import` whatever you
        installed."

      * **Isolated (`--isolated`).** Each spec gets its own
        `/nix/<short>/` venv with its own `bin/`. Conflicting Python
        deps across namespaces don't merge; the cost is that inline
        composition stops working — namespaces only see each other via
        host-side `c.remote(...)`. Reach for this when pip can't
        resolve a unified dep set.

    Stage 1 is optional and identical in both modes: a Nix builder
    that materialises `default.nix` derivations into `/nix/store/`.
    Stage 2 diverges by mode.
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

    if isolated:
        parts += _isolated_install_steps(specs, nix_shorts)
        mode = "isolated"
    else:
        parts += _merged_install_steps(specs, nix_shorts)
        mode = "merged"

    short_names = " ".join(s.short for s, _ in specs)
    parts.append(f'LABEL org.agentix.bundle.namespaces="{short_names}"')
    parts.append(f'LABEL org.agentix.bundle.mode="{mode}"')

    return "\n".join(parts) + "\n"


def _merged_install_steps(
    specs: list[tuple[NamespaceSpec, Path]],
    nix_shorts: set[str],
) -> list[str]:
    """All specs install into the framework's own venv at /nix/runtime/.

    The runtime base image already has the framework installed there,
    so a single `pip install /src/a /src/b ...` adds every namespace +
    its declared deps to one site-packages. pip's resolver runs once
    over the union — if it can't satisfy everyone simultaneously, the
    build fails with a clear error (and the CLI suggests `--isolated`).
    """
    steps: list[str] = []
    # Copy every spec's source tree first.
    for spec, _ in specs:
        steps.append(f"COPY {spec.short}/ /src/{spec.short}/")

    src_args = " ".join(f"/src/{spec.short}" for spec, _ in specs)
    steps.append(
        "RUN /nix/runtime/bin/pip install --no-cache-dir "
        + src_args
    )

    # Symlink every Nix-built binary into /nix/runtime/bin/. Two
    # namespaces shipping a binary with the same name will collide
    # (last-write wins); that's the price of merging.
    if nix_shorts:
        sl_steps = ["RUN set -eux; \\"]
        for short in sorted(nix_shorts):
            sl_steps.append(
                f"    SP=$(cat /nix/.sys-paths/{short}) && "
                f"for f in $SP/bin/*; do ln -sf \"$f\" /nix/runtime/bin/$(basename \"$f\"); done; \\"
            )
        sl_steps.append("    true")
        steps += sl_steps
    return steps


def _isolated_install_steps(
    specs: list[tuple[NamespaceSpec, Path]],
    nix_shorts: set[str],
) -> list[str]:
    """One venv per namespace under /nix/<short>/.

    Each `pip install` runs in its own venv so cross-namespace dep
    versions don't merge. The framework wheel is the only shared dep —
    installed into every venv from /nix/.wheels/ stashed by the
    runtime image. Namespaces can't inline-import each other; only
    host-side `c.remote(...)` crosses the boundary.
    """
    steps: list[str] = []
    for spec, _ in specs:
        block = [
            f"COPY {spec.short}/ /src/{spec.short}/",
            f"RUN uv venv /nix/{spec.short} && \\",
            f"    /nix/{spec.short}/bin/pip install --no-cache-dir "
            f"/nix/.wheels/agentix-*.whl /src/{spec.short}",
        ]
        if spec.short in nix_shorts:
            block[-1] += " && \\"
            block += [
                f"    SP=$(cat /nix/.sys-paths/{spec.short}) && \\",
                "    for f in $SP/bin/*; do \\",
                f"        ln -sf \"$f\" /nix/{spec.short}/bin/$(basename \"$f\"); \\",
                "    done",
            ]
        steps += block
    return steps


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
        help="base runtime image the bundle extends. Must exist locally; "
             "build it from Agentix-Runtime-Basic/runtime/Dockerfile or pull "
             f"it from your registry. (default: agentix/runtime:{FRAMEWORK_VERSION})",
    )
    parser.add_argument(
        "--isolated", action="store_true",
        help="give each namespace its own venv at /nix/<short>/ instead of "
             "merging into the framework venv. Inline imports across "
             "namespaces stop working; only `c.remote(...)` crosses the "
             "boundary. Use when pip can't resolve a unified dep set.",
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

    _warn_merged_with_system_deps(resolved, isolated=args.isolated)

    if args.dry_run:
        out = REPO_ROOT / "build" / tag.rsplit("/", 1)[-1].split(":", 1)[0]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        _stage(resolved, out)
        (out / "Dockerfile").write_text(
            _render_dockerfile(resolved, args.runtime_image, isolated=args.isolated),
        )
        print(f"staged build context → {out}")
        print(f"would build → {tag}")
        print(f"  extends → {args.runtime_image}")
        print(f"  mode → {'isolated' if args.isolated else 'merged'}")
        if len(specs) > 1:
            print(f"  namespaces: {', '.join(shorts)}")
        return 0

    # Ensure the runtime image exists locally before generating the bundle —
    # the bundle Dockerfile's `FROM agentix/runtime:<version>` would fail
    # otherwise.
    _ensure_runtime_image(args.runtime_image)

    with TemporaryDirectory(prefix="agentix-build-") as tmp:
        build_dir = Path(tmp)
        _stage(resolved, build_dir)
        (build_dir / "Dockerfile").write_text(
            _render_dockerfile(resolved, args.runtime_image, isolated=args.isolated),
        )
        ns_count = len(specs)
        mode_label = "isolated" if args.isolated else "merged"
        print(
            f"building {tag} ({ns_count} namespace{'s' if ns_count > 1 else ''} "
            f"extending {args.runtime_image}, {mode_label} mode)…",
            file=sys.stderr,
        )
        proc = subprocess.run(
            ["docker", "build", "-t", tag, str(build_dir)],
            check=False,
            stderr=subprocess.PIPE,
        )
        if proc.stderr:
            sys.stderr.buffer.write(proc.stderr)
        if proc.returncode != 0 and not args.isolated:
            # Heuristic: pip's resolution errors mention "conflict" /
            # "no matching" / "incompatible". Suggest --isolated.
            err = proc.stderr.decode(errors="replace") if proc.stderr else ""
            if any(
                token in err
                for token in ("conflict", "no matching distribution", "incompatible", "ResolutionImpossible")
            ):
                print(
                    "\nhint: docker build failed during the unified pip install — "
                    "two specs probably want incompatible Python deps. "
                    "Retry with `agentix build --isolated …` to give each spec "
                    "its own venv. Note: with --isolated, namespaces stop being "
                    "able to inline-import each other; cross-namespace composition "
                    "must go through `c.remote(...)` from the host.",
                    file=sys.stderr,
                )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
