# Development Guide

## Prerequisites

- [Nix](https://nixos.org/download/)
- [Docker](https://docs.docker.com/get-docker/)

Enable flakes (one-time):
```bash
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
```

## Setup

```bash
nix develop
```

That's it. Dev shell provides Python 3.12, ruff, pytest, all runtime deps, docker, node. No global installs needed.

## Dev Shell

`nix develop` gives you:

| Tool | Purpose |
|------|---------|
| python 3.12 | Runtime + all deps (fastapi, uvicorn, pydantic, httpx) |
| ruff | Linter + formatter |
| pytest | Testing |
| docker | Container runtime |
| node 22 | For npm-based agent builds |

## Daily Workflow

### Run runtime server locally

```bash
python -m hnix
# Server at http://localhost:8000
```

### Run in Docker (with agent)

```bash
RUNTIME=$(nix-build runtime/default.nix --no-out-link)
AGENT=$(nix-build agents/claude-code/default.nix --no-out-link)

docker run -d --name hnix-dev \
  -v /nix/store:/nix/store:ro \
  -e "PATH=${AGENT}/bin:${RUNTIME}/bin:/usr/bin:/bin" \
  -p 8000:8000 \
  ubuntu:24.04 \
  $RUNTIME/bin/hnix-server

# Test
curl http://localhost:8000/health
curl -X POST http://localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "claude --version"}'

# Cleanup
docker rm -f hnix-dev
```

### Lint & Format

```bash
ruff check runtime/          # lint
ruff check runtime/ --fix    # lint + auto-fix
ruff format runtime/         # format
```

### Test

```bash
pytest                        # all tests
pytest -x                     # stop on first failure
pytest -k "test_exec"         # run specific test
```

### Build Nix packages

```bash
nix build .#runtime           # runtime closure
nix build .#claude-code       # agent closure
```

## Debugging

### Local (no Docker)

Standard Python debugging — works with any IDE.

**VSCode** `.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "hnix-server",
      "type": "debugpy",
      "request": "launch",
      "module": "hnix",
      "cwd": "${workspaceFolder}/runtime",
      "args": ["--port", "8000"]
    }
  ]
}
```

**pdb:**
```bash
python -m pdb -m hnix
```

### In Docker (remote debug)

For debugging the runtime server running inside a container.

**1. Start server with debugpy:**

```bash
RUNTIME=$(nix-build runtime/default.nix --no-out-link)
AGENT=$(nix-build agents/claude-code/default.nix --no-out-link)

# Mount source for live editing, expose debug port
docker run -d --name hnix-debug \
  -v /nix/store:/nix/store:ro \
  -v $(pwd)/runtime/hnix:/debug-src/hnix:ro \
  -e "PATH=${AGENT}/bin:/usr/bin:/bin" \
  -e "PYTHONPATH=/debug-src" \
  -p 8000:8000 \
  -p 5678:5678 \
  ubuntu:24.04 \
  ${RUNTIME}/bin/python -m debugpy --listen 0.0.0.0:5678 --wait-for-client -m hnix
```

Server waits for debugger to attach before starting.

**2. VSCode attach** `.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Attach to Docker",
      "type": "debugpy",
      "request": "attach",
      "connect": { "host": "localhost", "port": 5678 },
      "pathMappings": [
        { "localRoot": "${workspaceFolder}/runtime/hnix", "remoteRoot": "/debug-src/hnix" }
      ]
    }
  ]
}
```

Source is volume-mounted — edit locally, changes reflect in container immediately. No rebuild needed.

### Whitebox agent debugging

Same pattern — deps from Nix closure, source from volume mount:

```bash
docker run -d --name agent-debug \
  -v /nix/store:/nix/store:ro \
  -v $(pwd)/my-agent/src:/app/src:ro \
  -p 5678:5678 \
  ubuntu:24.04 \
  python -m debugpy --listen 0.0.0.0:5678 --wait-for-client /app/src/main.py
```

- **Deps**: Nix closure (stable, no rebuild)
- **Source**: volume mount (edit → immediate effect)
- **Debug**: IDE attach via port 5678

## Adding a New Agent

1. Create `agents/{name}/default.nix`
2. Register in `flake.nix`:
   ```nix
   packages.${system}.my-agent = import ./agents/my-agent/default.nix { inherit pkgs; };
   ```
3. Build: `nix build .#my-agent`
4. Test:
   ```bash
   AGENT=$(nix-build agents/my-agent/default.nix --no-out-link)
   docker run --rm -v /nix/store:/nix/store:ro $AGENT/bin/my-agent --version
   ```
