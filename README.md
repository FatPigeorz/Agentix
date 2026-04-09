<div align="center">

# Agentix

### Run Any Agent on Any Benchmark

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)

Agentix packages coding agents as reproducible **Nix closures** and injects them into any benchmark's Docker image — SWE-bench, OpenSWE, OS-World, and more. One agent build, every benchmark.

</div>

---

## The Problem

You want to evaluate **N agents** on **M benchmarks**. Without Agentix, you build N x M custom setups. With Agentix:

- Package each agent **once** as a Nix closure
- Inject into **any** benchmark Docker image at runtime
- Collect results through a **universal HTTP interface**

## Core Ideas

**Any Agent** — Claude Code, Codex, Aider, SWE-agent, OpenHands. Each agent is a self-contained Nix closure (binary + all deps + Python adapter).

**Any Benchmark** — SWE-bench, SWE-bench Pro, OpenSWE, OS-World, HumanEval. The agent closure mounts into any benchmark's Docker image via `-v /nix/store:/nix/store:ro`.

**Deployment Agnostic** — Docker, Kubernetes, Modal, E2B. The runtime server is a pure sandbox interface that doesn't care where it runs.

**Reproducible** — Same git commit = same binaries, forever. Nix guarantees bit-for-bit reproducibility.

## Quick Start

```bash
# Build agent and runtime closures
RUNTIME=$(nix build .#runtime --no-link --print-out-paths)
AGENT=$(nix build .#claude-code --no-link --print-out-paths)

# Inject into any Docker image (ubuntu, swebench, os-world, ...)
docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -e PATH=$AGENT/bin:$RUNTIME/bin:/usr/bin:/bin \
  -p 8000:8000 \
  ubuntu:24.04 \
  $RUNTIME/bin/agentix-server

# Run the agent
curl -X POST localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "claude -p \"Fix the bug in main.py\" --output-format text"}'

# Retrieve results
curl "localhost:8000/download?path=/workspace/main.py"
```

## Agent Adapter

Each agent has a `runner.py` — a thin adapter that calls the CLI binary and returns structured output:

```python
async def run(agent_input: AgentInput) -> AgentOutput:
    # AgentInput:  instruction, workdir, env
    # AgentOutput: exit_code, stdout, stderr, trajectory
```

Agent-specific config (model, API keys, timeout) goes through environment variables, not function parameters.

## Repositories

| Repo | Purpose |
|------|---------|
| **[Agentix](https://github.com/Agentiix/Agentix)** | Core — runtime server, deployment, agent protocol |
| **[Agentix-Agents-Hub](https://github.com/Agentiix/Agentix-Agents-Hub)** | Agent adapters — claude-code, aider, ... |
| **[Agentix-Datasets](https://github.com/Agentiix/Agentix-Datasets)** | Benchmark runners — SWE-bench, ... |

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| **0** | Agent evaluation on benchmarks | In Progress |
| **1** | LLM Proxy — token-level trajectory tracing | Planned |
| **2** | Partial Rollout — search & RL over trajectories | Planned |

See [ROADMAP.md](ROADMAP.md) for details.

## Project Structure

```
agentix/
├── runtime/        # FastAPI server + async client
├── deployment/     # Sandbox lifecycle (Docker, K8s, ...)
├── agents/         # AgentInput / AgentOutput protocol
└── models.py       # Pydantic models
```

## Docs

- [Architecture](docs/architecture.md)
- [Agent Protocol](docs/agent-protocol.md)
- [Development](docs/DEVELOPMENT.md)
