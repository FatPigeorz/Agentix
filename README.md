# Agentix

**Run Any Agent on Any Benchmark.**

Agentix is the middleware layer between coding agents and benchmark environments. It packages agents as reproducible Nix closures and injects them into any benchmark's Docker image — SWE-bench, SWE-bench Pro, OpenSWE, OS-World, and more.

## Why

- **Any Agent** — Claude Code, Codex, Aider, SWE-agent, OpenHands... each agent is a Nix closure with a thin Python adapter.
- **Any Benchmark** — SWE-bench, SWE-bench Pro, OpenSWE, OS-World, HumanEval... inject the same agent closure into any benchmark's Docker image.
- **Deployment Agnostic** — Docker, Kubernetes, Daytona, Modal, E2B. The runtime server doesn't care where it runs.
- **Reproducible** — Same git commit = same binaries, forever. Nix guarantees bit-for-bit reproducibility.

## Quick Start

```bash
# Build
RUNTIME=$(nix build .#runtime --no-link --print-out-paths)
AGENT=$(nix build .#claude-code --no-link --print-out-paths)

# Launch sandbox
docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -e PATH=$AGENT/bin:$RUNTIME/bin:/usr/bin:/bin \
  -p 8000:8000 \
  ubuntu:24.04 \
  $RUNTIME/bin/agentix-server

# Execute
curl -X POST localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "claude -p \"Fix the bug in main.py\" --output-format text"}'

# Retrieve files
curl "localhost:8000/download?path=/workspace/main.py"
```

## Agent Adapter

Each agent has a `runner.py` — a thin adapter that calls the CLI binary and returns structured output:

```python
async def run(agent_input: AgentInput) -> AgentOutput:
    # AgentInput:  instruction, workdir, env
    # AgentOutput: exit_code, stdout, stderr, trajectory
```

Agent-specific config (model, max_turns, timeout) goes through environment variables, not function parameters.

## Repositories

| Repo | Purpose |
|------|---------|
| [Agentix](https://github.com/Agentiix/Agentix) | Core: runtime server, client, deployment |
| [Agentix-Agents-Hub](https://github.com/Agentiix/Agentix-Agents-Hub) | Agent adapters: claude-code, aider, ... |
| [Agentix-Datasets](https://github.com/Agentiix/Agentix-Datasets) | Benchmark runners: SWE-bench, ... |

## Project Structure

```
agentix/
├── runtime/       # FastAPI server + async client
│   ├── server.py  # /exec, /upload, /download, /health
│   ├── client.py  # RuntimeClient with retries
│   └── executor.py
├── deployment/    # Sandbox lifecycle management
│   ├── base.py    # Abstract Deployment interface
│   └── docker.py  # Docker implementation
├── agents/        # Agent protocol
│   └── protocol.py  # AgentInput, AgentOutput, Step
└── models.py      # Pydantic models
```

## Docs

- [Architecture](docs/architecture.md)
- [Agent Protocol](docs/agent-protocol.md)
- [Development](docs/DEVELOPMENT.md)
