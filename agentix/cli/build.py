"""`agentix build` — build a single namespace image.

Usage:

    agentix build primitives/bash                      # explicit path
    agentix build bash                                 # short name → primitives/bash
    agentix build claude-code                          # short name → agents/claude-code
    agentix build agentix-bash               # PyPI dist (stubbed)
    agentix build primitives/bash --tag my-bash:dev
    agentix build primitives/bash --dry-run

The argument accepts the same spec format as `agentix install`: an
explicit path, a short name resolved against `primitives/agents/datasets/`
in the repo, or a PyPI distribution (`agentix install` and PyPI fetching
both stub the PyPI path with a clear NotImplementedError today).

A namespace's minimum-viable source layout:

    <namespace_dir>/
    ├── pyproject.toml            # all metadata (name, version, description)
    └── agentix_namespaces/<name>/
        ├── __init__.py           # stub class
        └── _impl.py              # impl class

Everything else — Dockerfile, default.nix, manifest.json — is shared
infrastructure pulled in from `primitives/_template/` and `tools/`.
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


def _derive_tag(pyproject: dict) -> str:
    """`agentix-bash` v `0.1.0` → `agentix/bash:0.1.0`.

    The framework's convention is `agentix-<kind>-<name>` for the
    distribution and `agentix/<kind>-<name>:<version>` for the image.
    """
    project = pyproject.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SystemExit("pyproject.toml: [project] needs string name + version")
    if not name.startswith("agentix-"):
        raise SystemExit(
            f"pyproject.toml: name {name!r} must follow `agentix-<kind>-<name>`"
        )
    short = name[len("agentix-"):]  # e.g. primitive-bash
    return f"agentix/{short}:{version}"


def _materialize_path(spec: NamespaceSpec) -> Path:
    """Return the on-disk namespace source dir for `spec`.

    `path` kinds are already on disk and used as-is. `pypi` would do
    `pip download` + wheel unpack into a temp dir — not yet wired.
    `image` doesn't make sense for `build`: the artifact is already
    built; the user wanted `agentix deploy` or `agentix install --no-rebuild`.
    """
    if spec.kind == "path":
        assert spec.path is not None
        return spec.path
    if spec.kind == "pypi":
        raise NotImplementedError(
            f"`agentix build {spec.short}`: PyPI sourcing not wired yet. "
            f"The build pipeline needs a `pip download {spec.pypi_dist}` + "
            f"wheel unpack step before the namespace dir is ready. Use a local "
            f"path or check that the namespace lives under primitives/, "
            f"agents/, or datasets/ in this repo."
        )
    raise SystemExit(
        f"`agentix build {spec.short}`: image refs aren't a valid input — "
        f"that image already exists. Try `agentix deploy local --image {spec.image_ref}` "
        f"or include it in `agentix install`."
    )


_SOURCE_SKIP = {
    "__pycache__", ".venv", "build", "dist", ".git",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "*.egg-info",
}


def _stage(namespace_dir: Path, build_dir: Path) -> None:
    """Copy namespace source + shared build infra into a self-contained context.

    The namespace source is treated as a normal Python project (uv init form):
    we copy everything except common dev artifacts, then drop in the shared
    Dockerfile + default.nix + gen_manifest.py alongside it.
    """
    for item in namespace_dir.iterdir():
        if item.name in _SOURCE_SKIP or item.name.endswith(".egg-info"):
            continue
        dest = build_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=shutil.ignore_patterns(*_SOURCE_SKIP))
        else:
            shutil.copy2(item, dest)
    shutil.copy2(TEMPLATE_DIR / "Dockerfile", build_dir / "Dockerfile")
    shutil.copy2(TEMPLATE_DIR / "default.nix", build_dir / "default.nix")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix build",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("spec", help="namespace short name or path (e.g. bash, primitives/bash)")
    parser.add_argument("--tag", type=str, default=None,
                        help="override the derived docker image tag")
    parser.add_argument("--dry-run", action="store_true",
                        help="stage to ./build/<name>/ and print the path; do NOT invoke docker")
    args = parser.parse_args(argv)

    spec = resolve_spec(args.spec)
    try:
        namespace_dir = _materialize_path(spec)
    except NotImplementedError as exc:
        # Convert to SystemExit so argparse-style stderr message + exit-1
        # behaviour matches the rest of the CLI.
        raise SystemExit(f"error: {exc}") from exc
    pyproject = read_pyproject(namespace_dir)
    tag = args.tag or _derive_tag(pyproject)
    short_name = pyproject["project"]["name"].rsplit("-", 1)[-1]

    if args.dry_run:
        out = REPO_ROOT / "build" / short_name
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        _stage(namespace_dir, out)
        print(f"staged build context → {out}")
        print(f"would build → {tag}")
        return 0

    with TemporaryDirectory(prefix=f"agentix-build-{short_name}-") as tmp:
        build_dir = Path(tmp)
        _stage(namespace_dir, build_dir)
        print(f"building {tag} from {namespace_dir}…", file=sys.stderr)
        proc = subprocess.run(
            [
                "docker", "build",
                "--build-arg", f"CLOSURE_NAME={short_name}",
                "-t", tag,
                str(build_dir),
            ],
            check=False,
        )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
