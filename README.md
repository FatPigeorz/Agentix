# harbor-nix

Nix-based toolchain for packaging agent runtimes into closures and injecting them into sandbox containers. This is a standalone toolchain -- not a Harbor integration.

## Architecture

```
                        HOST (Nix)                                  SANDBOX (no Nix)
 ┌──────────────────────────────────────┐          ┌──────────────────────────────────────┐
 │                                      │          │                                      │
 │  flake.nix                           │          │  /nix/store/...-hnix-runtime/bin/     │
 │    ├── runtime/default.nix ──────────┼──build──>│    ├── hnix-load                     │
 │    │     hnix-load, hnix-run,        │          │    ├── hnix-run                      │
 │    │     hnix-info, hnix-extract     │          │    ├── hnix-info                     │
 │    │     + bash + coreutils          │          │    ├── hnix-extract                  │
 │    │                                 │          │    ├── bash, coreutils ...            │
 │    └── agents/xxx/default.nix ───────┼──build──>│                                      │
 │          agent binary + all deps     │          │  /nix/store/...-agent-runtime/bin/    │
 │                                      │          │    └── claude, node, npm ...          │
 └──────────────────────────────────────┘          └──────────────────────────────────────┘

 Two-layer closure model:
   Layer 1: Runtime closure   -- loader tools (hnix-*), always present
   Layer 2: Agent closure     -- agent binary + dependencies, swappable
```

### Injection paths

```
 Path A: Mount (local Docker / K8s)        Path B: Upload (Daytona / Modal / E2B)
 ────────────────────────────────           ─────────────────────────────────────────
 nix-build runtime + agent                 nix-build runtime + agent
        │                                         │
        v                                         v
 docker run \                              export-closure.sh  -->  *.tar.gz
   -v /nix/store:/nix/store:ro \                  │
   container cmd                           docker cp  -->  container:/tmp/
                                                  │
                                           tar xzf -C /  -->  /nix/store/...
                                                  │
                                           docker exec container cmd
```

**Path A** is fast -- zero-copy, read-only bind mount of the host Nix store. Ideal for local development and Kubernetes pods with shared volumes.

**Path B** is portable -- the closure tarball is self-contained and can be shipped to any Linux container, even on hosts without Nix. This is the path for cloud sandboxes (Daytona, Modal, E2B).

## Quick start

Prerequisites: [Nix](https://nixos.org/download) and Docker.

```bash
# Source Nix (if not already in PATH)
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

# Build closures
nix-build runtime/default.nix --no-out-link
nix-build agents/claude-code/default.nix --no-out-link

# Run the end-to-end demo (tests both injection paths)
bash demo/run.sh
```

## Project structure

```
harbor-nix/
├── flake.nix                       # Top-level Nix flake
├── runtime/default.nix             # Runtime closure (hnix-load/run/info/extract + bash + coreutils)
├── agents/
│   └── claude-code/default.nix     # Claude Code agent closure
├── scripts/
│   ├── export-closure.sh           # Export a closure as a portable tarball
│   ├── inject-mount.sh             # Path A: mount injection helper
│   └── inject-upload.sh            # Path B: upload injection helper
├── demo/
│   ├── run.sh                      # End-to-end demo (builds, tests both paths, reports timing)
│   └── task/
│       ├── Dockerfile              # Minimal sandbox container (ubuntu:24.04)
│       └── instruction.md          # Sample task for the agent
├── harbor/                         # Harbor submodule (reference only)
└── reports/                        # Analysis reports
```

## How to add a new agent

Each agent is a standalone Nix derivation under `agents/<name>/default.nix`. It must produce a store path with a `bin/` directory containing the agent's executable.

### Template

```nix
{ pkgs ? import <nixpkgs> {} }:

pkgs.stdenv.mkDerivation {
  pname = "my-agent-runtime";
  version = "1.0.0";

  dontUnpack = true;

  installPhase = ''
    mkdir -p $out/bin

    # Install your agent binary/script into $out/bin/
    cat > $out/bin/my-agent <<'WRAPPER'
    #!/bin/sh
    exec /path/to/interpreter "$@"
    WRAPPER
    chmod +x $out/bin/my-agent
  '';

  meta.description = "My agent runtime for Harbor";
}
```

### Steps

1. Create `agents/<name>/default.nix` following the template above.
2. Register it in `flake.nix`:
   ```nix
   packages.${system} = {
     runtime     = import ./runtime/default.nix { inherit pkgs; };
     claude-code = import ./agents/claude-code/default.nix { inherit pkgs; };
     my-agent    = import ./agents/my-agent/default.nix { inherit pkgs; };
   };
   ```
3. Build and test:
   ```bash
   nix-build agents/my-agent/default.nix --no-out-link
   bash scripts/inject-mount.sh agents/my-agent/default.nix ubuntu:24.04 my-agent --version
   ```

### Requirements

- The derivation must produce `$out/bin/<agent-executable>`.
- All dependencies must be captured in the Nix closure (no runtime `apt-get` or `pip install`).
- For network-fetching builds (e.g., npm/pip), use a fixed-output derivation with `outputHash` to ensure reproducibility. See `agents/claude-code/default.nix` for an example.
