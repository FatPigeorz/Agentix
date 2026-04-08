# Development Guide

## Prerequisites

- [Nix](https://nixos.org/download/)
- [Docker](https://docs.docker.com/get-docker/)
- [direnv](https://direnv.net/) (recommended)

### One-time Nix setup

```bash
# Enable flakes
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf

# (Optional) Add GitHub token to avoid rate limiting
echo "access-tokens = github.com=ghp_your_token" >> ~/.config/nix/nix.conf
```

## Setup

### Option A: direnv (recommended)

```bash
direnv allow    # auto-loads nix dev shell when you cd into the project
```

### Option B: manual

```bash
nix develop     # enter dev shell
```

Both give you Python 3.12, ruff, pytest, all runtime deps, docker, node.

## Editor Setup

### VSCode

Install two extensions:

1. **Nix IDE** (`jnoortheen.nix-ide`) — `.nix` syntax support
2. **direnv** (`mkhl.direnv`) — auto-loads Nix dev shell environment

That's it. With direnv, VSCode automatically picks up the correct Python interpreter, ruff, and all tools. No manual interpreter path config needed.

### Other editors

Any editor with direnv support works the same way:

- **Neovim**: [direnv.vim](https://github.com/direnv/direnv.vim)
- **Emacs**: [envrc](https://github.com/purcell/envrc)
- **JetBrains**: [Direnv integration plugin](https://plugins.jetbrains.com/plugin/15285-direnv-integration)

Without direnv, run `nix develop` in your terminal and launch the editor from there.

## Dev Shell

`nix develop` / direnv gives you:

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

hnix-server 内置 debugpy 支持，与部署方式无关（Docker、K8s、Daytona、Modal 都一样）。

### 原理

```
IDE (VSCode/PyCharm/...)
  │
  │  DAP protocol over TCP
  │
  ▼
sandbox:5678  ← debugpy (hnix-server 内置)
sandbox:8000  ← hnix-server HTTP API
```

hnix-server 启动时加 `--debug` 就会启动 debugpy 监听。Deployment 层只需要多暴露 5678 端口。

### 启动 debug 模式

```bash
# 沙箱内 (不管是什么 deployment)
hnix-server --debug                    # debugpy 在 5678 监听
hnix-server --debug --debug-wait       # 等 IDE attach 后才启动
hnix-server --debug --debug-port 9229  # 自定义端口
```

### 端口暴露 (Deployment 层的事)

| Deployment | 暴露 debug 端口的方式 |
|------------|----------------------|
| Docker | `-p 5678:5678` |
| K8s | `kubectl port-forward pod/xxx 5678:5678` |
| Daytona | sandbox port mapping 配置 |
| Modal | sandbox network 配置 |

### IDE 配置

**VSCode** `.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "hnix-server (local)",
      "type": "debugpy",
      "request": "launch",
      "module": "hnix",
      "cwd": "${workspaceFolder}/runtime",
      "args": ["--port", "8000"]
    },
    {
      "name": "Attach to sandbox",
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

**PyCharm**: Run → Edit Configurations → Python Debug Server → host `localhost`, port `5678`

**pdb** (terminal):
```bash
python -m pdb -m hnix
```

### Whitebox agent debugging

同样的模式 — deps 从 Nix closure 来 (不变), source volume mount (实时编辑):

```
IDE attach → sandbox:5678
              │
              ├── agent source: volume mount (编辑即生效)
              ├── agent deps: Nix closure (稳定, 不用重建)
              └── debugpy: 断点、单步、inspect 变量
```

Deployment 层在创建沙箱时：
1. 挂载 agent source 到沙箱内 (如 `/app/src`)
2. 启动 agent 时加 debugpy: `python -m debugpy --listen 0.0.0.0:5678 /app/src/main.py`
3. 暴露 5678 端口

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
