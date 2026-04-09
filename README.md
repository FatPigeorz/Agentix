<div align="center">

# Agentix

**Run Any Agent on Any Environment. Ready for Agentic Reinforcement.**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

</div>

## ✨ Core Features

- **Any Agent** — Claude Code, Codex, Aider, SWE-agent, OpenHands. Each agent is packaged as a self-contained Nix closure.
- **Any Environment** — SWE-bench, SWE-bench Pro, OpenSWE, OS-World, HumanEval. Inject agent closures into any environment.
- **Reproducible** — Same git commit = same binaries, forever. Nix guarantees bit-for-bit reproducibility.
- **Deployment Agnostic** — Docker, Kubernetes, Modal, E2B. The runtime server doesn't care where it runs.

## 📦 Installation

```bash
# From source
git clone https://github.com/Agentiix/Agentix.git
cd Agentix
nix develop  # or: pip install -e .

# Build closures
nix build .#runtime
nix build .#claude-code
```

## 🚀 Quick Start

```bash
RUNTIME=$(nix build .#runtime --no-link --print-out-paths)
AGENT=$(nix build .#claude-code --no-link --print-out-paths)

# Inject into any environment
docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -e PATH=$AGENT/bin:$RUNTIME/bin:/usr/bin:/bin \
  -p 8000:8000 \
  ubuntu:24.04 \
  $RUNTIME/bin/agentix-server

# Run agent
curl -X POST localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "claude -p \"Fix the bug in main.py\" --output-format text"}'

# Retrieve results
curl "localhost:8000/download?path=/workspace/main.py"
```

## 🏗️ Architecture

Agentix sits between the orchestrator and the sandbox. The **runtime server** provides a universal HTTP interface inside any container. **Agent closures** are mounted read-only via Nix store.

| Component | Role |
|-----------|------|
| **Runtime Server** | FastAPI server inside sandbox — `/exec`, `/upload`, `/download`, `/health` |
| **Agent Closure** | Nix package — agent binary + all deps + Python adapter |
| **Deployment** | Sandbox lifecycle management — create, get, update, delete |
| **Agent Adapter** | `runner.py` — calls CLI binary, returns `AgentOutput` with trajectory |

### Agent Adapter Protocol

```python
async def run(agent_input: AgentInput) -> AgentOutput:
    # AgentInput:  instruction, workdir, env
    # AgentOutput: exit_code, stdout, stderr, trajectory
```

## 📁 Repositories

| Repo | Purpose |
|------|---------|
| **[Agentix](https://github.com/Agentiix/Agentix)** | Core — runtime server, deployment, agent protocol |
| **[Agentix-Agents-Hub](https://github.com/Agentiix/Agentix-Agents-Hub)** | Agent adapters — claude-code, aider, ... |
| **[Agentix-Datasets](https://github.com/Agentiix/Agentix-Datasets)** | Benchmark runners — SWE-bench, ... |

## 🗺️ Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| **0** | Agent evaluation on datasets | In Progress |
| **1** | LLM Proxy — token-level trajectory tracing | Planned |
| **2** | Partial Rollout — search & RL over trajectories | Planned |

See [ROADMAP.md](ROADMAP.md) for details.

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

## 📄 License

[MIT License](LICENSE)
