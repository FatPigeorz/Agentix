# harbor-nix

Coding Agent SDK: Nix-based agent packaging and sandboxed execution.

## Architecture

```
Host                              Sandbox
 │                                 │
 │  nix build .#claude-code        │  hnix-server (:8000)
 │  nix build .#runtime            │  ├── POST /run    ← run agent
 │                                 │  ├── POST /exec   ← run command
 │  deployment.create(config)      │  ├── POST /upload
 │  client.run(agent_input)  ────► │  └── GET  /download
 │                                 │
 │  deployment.delete(id)          │  runner.py: run(agent_input) -> dict
```

## Quick Start

```bash
nix develop    # dev environment: python, ruff, pytest, docker

nix build .#runtime
nix build .#claude-code

RUNTIME=$(nix-build runtime/default.nix --no-out-link)
AGENT=$(nix-build agents/claude-code/default.nix --no-out-link)

docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -e "PATH=${AGENT}/bin:${RUNTIME}/bin:/usr/bin:/bin" \
  -p 8000:8000 ubuntu:24.04 $RUNTIME/bin/hnix-server

curl -X POST localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"agent_input": {"instruction": "hello", "api_key": "sk-..."}}'
```

## Structure

```
src/hnix/
├── runtime/           server (sandbox) + client (host)
├── deployment/        sandbox CRUD (docker, k8s, ...)
└── models.py

agents/{name}/
├── default.nix        nix packaging
└── runner.py          run(agent_input: dict) -> dict
```

## Docs

- [Architecture](docs/architecture.md) — protocols, API spec
- [Development](docs/DEVELOPMENT.md) — setup, linting, debugging
