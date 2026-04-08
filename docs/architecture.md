# harbor-nix 架构

## 做什么

Coding Agent SDK：用 Nix 打包 AI agent 运行环境，注入到评估沙箱中，提供统一接口运行 agent。

- **打包**: Nix closure，binary + 全部依赖，hash-pinned 可复现
- **Runtime server**: 沙箱内 HTTP 服务，加载 agent runner，统一接口屏蔽 deployment 差异
- **评估代码写一次，到处跑**

## 三层架构

```
┌─ Host ─────────────────────────────────────────────────────────┐
│                                                                 │
│  nix build .#runtime → runtime closure                          │
│  nix build .#claude-code → agent closure                        │
│                                                                 │
│  Orchestrator / 评估代码                                        │
│  ├── 调用 Deployment CRUD 管理沙箱                               │
│  └── 调用 Sandbox HTTP API 运行 agent                           │
│       POST /run   ← 核心: 运行 agent                            │
│       POST /exec  ← 底层: 执行任意命令                           │
│       POST /upload, GET /download ← 文件传输                     │
│                                                                 │
└────────────┬───────────────────────────────────┬───────────────┘
             │ Deployment 接口                    │ Sandbox 接口
             │ (CRUD)                             │ (HTTP, 统一)
             ▼                                    ▼
┌─ Deployment ──────────────┐    ┌─ Sandbox ──────────────────────┐
│                            │    │                                │
│  沙箱的 CRUD:              │    │  hnix-server (port 8000)       │
│  Create  创建+注入+启动    │    │  ├── POST /run                 │
│  Read    查询状态          │    │  ├── POST /exec                │
│  Update  更新配置          │    │  ├── POST /upload              │
│  Delete  销毁释放          │    │  ├── GET  /download            │
│                            │    │  └── GET  /health              │
│  实现:                     │    │                                │
│  - DockerDeployment        │    │  启动时加载:                    │
│  - K8sDeployment           │    │  /opt/hnix/agent/runner.py     │
│  - DaytonaDeployment       │    │                                │
│  - ModalDeployment         │    │                                │
└────────────────────────────┘    └────────────────────────────────┘
```

## 协议

### Sandbox HTTP API (Runtime Server)

Runtime server 运行在沙箱内，启动时从 `/opt/hnix/agent/runner.py` 加载 agent runner。

#### POST /run — 运行 agent

核心接口。Orchestrator 传入 `agent_input`，server 调用 `runner.run(agent_input)`。

```
请求:
POST /run
{
    "agent_input": {
        "instruction": "Fix the bug in main.py",
        "api_key": "sk-ant-...",
        "model": "claude-sonnet-4-20250514",
        "timeout": 300
    }
}

响应:
{
    "result": {
        "exit_code": 0,
        "stdout": "Fixed the bug...",
        "stderr": ""
    }
}
```

`agent_input` 的内容由每个 agent 的 `runner.py` 定义。Orchestrator 和 runtime server 不解析它，透传给 `runner.run()`。

#### POST /exec — 执行任意命令

底层接口，用于调试或 agent 不适合用 `/run` 的场景。

```
请求:
POST /exec
{
    "command": "git diff",
    "timeout": 30,
    "cwd": "/app",
    "env": {"KEY": "value"}
}

响应:
{
    "exit_code": 0,
    "stdout": "...",
    "stderr": ""
}
```

#### POST /upload — 上传文件到沙箱

```
请求:
POST /upload
Content-Type: multipart/form-data
- file: (binary)
- path: "/app/test.py"

响应:
{
    "path": "/app/test.py",
    "size": 1024
}
```

#### GET /download — 从沙箱下载文件

```
请求:
GET /download?path=/app/result.txt

响应:
Content-Type: application/octet-stream
(file content)
```

#### GET /health — 存活检查

```
请求:
GET /health

响应:
{
    "status": "ok",
    "version": "0.1.0"
}
```

### Agent Runner 协议

每个 agent 提供一个 `runner.py`，放在 agent closure 的根目录。Runtime server 启动时从 `/opt/hnix/agent/runner.py` 加载。

#### 接口

```python
async def run(agent_input: dict) -> dict
```

- **输入**: `agent_input` — agent 自定义的参数字典，由 orchestrator 透传
- **输出**: `dict` — agent 执行结果，格式由 agent 自定义
- **运行位置**: 沙箱内，可以直接访问文件系统和执行命令

#### 示例: Claude Code

```python
# agents/claude-code/runner.py
async def run(agent_input: dict) -> dict:
    """
    agent_input: {
        "instruction": str,      # required
        "api_key": str,          # required
        "model": str,            # default "claude-sonnet-4-20250514"
        "timeout": float | None, # optional
    }
    """
    # 直接在沙箱内执行 claude CLI
    proc = await asyncio.create_subprocess_shell(
        f"claude -p {shlex.quote(instruction)} -m {model}",
        env={"ANTHROPIC_API_KEY": api_key, **os.environ},
        ...
    )
    return {"exit_code": ..., "stdout": ..., "stderr": ...}
```

#### 示例: 自研 Agent

```python
# agents/my-agent/runner.py
async def run(agent_input: dict) -> dict:
    """
    agent_input: {
        "instruction": str,
        "api_key": str,
        "max_iterations": int,
    }
    """
    # 自研 agent 可以做任何事
    from my_agent import Agent
    agent = Agent(api_key=agent_input["api_key"])
    result = await agent.solve(agent_input["instruction"])
    return {"solution": result.code, "iterations": result.n_iterations}
```

### Deployment 接口 (Host → Deployment)

```python
class SandboxConfig(BaseModel):
    task_image: str          # benchmark 提供的 Docker image
    runtime_closure: str     # Nix store path
    agent_closure: str       # Nix store path


class SandboxInfo(BaseModel):
    sandbox_id: str          # 唯一标识
    runtime_url: str         # e.g. "http://localhost:18000"
    status: str              # "running" | "stopped" | "error"


class Deployment(ABC):

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """创建沙箱。一步完成:
        1. 基于 task_image 创建容器/沙箱
        2. 注入 runtime closure
        3. 注入 agent closure (包含 runner.py)
        4. 设置 PATH
        5. 启动 hnix-server
        返回 sandbox_id + runtime_url
        """

    async def get(self, sandbox_id: str) -> SandboxInfo:
        """查询沙箱状态。"""

    async def update(self, sandbox_id: str, config: SandboxConfig) -> SandboxInfo:
        """更新沙箱 (如换 agent closure)。"""

    async def delete(self, sandbox_id: str) -> None:
        """销毁沙箱，释放资源。"""
```

| 步骤 | Docker | K8s | Daytona | Modal |
|------|--------|-----|---------|-------|
| 创建 | `docker run -d` | create pod | `create_sandbox()` | `Sandbox.create()` |
| 注入 | `-v /nix/store:ro` | PV mount | upload tarball | Volume / upload |
| 启动 runtime | 容器 CMD | container command | `exec(hnix-server)` | sandbox exec |
| 返回 URL | `localhost:{port}` | `pod-ip:8000` | `sandbox.url` | `sandbox.url` |

### RuntimeClient (Host 侧)

Orchestrator 用 `RuntimeClient` 与沙箱通信：

```python
from hnix.runtime import RuntimeClient

async with RuntimeClient("http://localhost:18000") as client:
    await client.wait_until_alive()

    # 运行 agent
    result = await client.run({
        "instruction": "Fix the bug in main.py",
        "api_key": "sk-ant-...",
        "model": "claude-sonnet-4-20250514",
    })
    print(result)  # {"exit_code": 0, "stdout": "...", "stderr": ""}

    # 底层命令 (调试用)
    resp = await client.exec("git diff")

    # 文件传输
    await client.upload("local/file.py", "/app/file.py")
    await client.download("/app/result.txt", "local/result.txt")
```

## 沙箱内结构

```
/opt/hnix/
├── runtime/            → hnix-server + Python deps
│   └── bin/hnix-server
└── agent/              → agent binary + deps + runner.py
    ├── bin/claude
    ├── bin/node
    ├── lib/node_modules/...
    └── runner.py       → async def run(agent_input) -> dict

PATH=/opt/hnix/agent/bin:/opt/hnix/runtime/bin:$PATH
```

## Agent 打包

每个 agent 是一个目录，包含两个文件：

```
agents/{name}/
├── default.nix    # Nix 打包: binary + deps → closure
└── runner.py      # 调用协议: run(agent_input) -> dict
```

- `default.nix` → `nix build` 产出 closure，包含 agent binary + 全部依赖 + runner.py
- `runner.py` → 定义如何调用这个 agent

Blackbox/whitebox 不区分流程，都是同一个结构。

## 典型流程

```
Host                          Deployment              Sandbox
 │                                │                       │
 │  nix build (runtime + agent)   │                       │
 │                                │                       │
 │  deployment.create(config) ───►│                       │
 │                                │──► 创建容器            │
 │                                │──► 注入 closures ────►│
 │                                │──► 启动 hnix-server ──►│ :8000
 │  ◄── SandboxInfo               │                       │ (加载 runner.py)
 │      { runtime_url }           │  (退到后台)            │
 │                                │                       │
 │  POST /run ──────────────────────────────────────────►│
 │  {"agent_input": {                                    │
 │    "instruction": "fix the bug",                      │
 │    "api_key": "sk-ant-..."                            │
 │  }}                                                   │
 │                                │        runner.run()   │
 │  ◄── {"result": {"exit_code": 0, "stdout": "..."}}   │
 │                                │                       │
 │  GET /download (results) ────────────────────────────►│
 │                                │                       │
 │  deployment.delete(id) ───────►│──► 销毁 ─────────────►│ ✗
 │                                │                       │
```

## 关注点分离

| 层 | 关注 | 不关注 |
|---|------|--------|
| **Host** | 构建 closure、编排流程、调 `/run`、收集结果 | 基础设施类型、agent CLI 细节 |
| **Deployment** | 沙箱 CRUD、注入 closure、启动 runtime | agent_input 内容、runner 逻辑 |
| **Sandbox** | 加载 runner.py、执行 `run(agent_input)`、文件 I/O | 自己在哪、closure 从哪来 |

## 版本管理

Nix 原生能力，不造轮子:

| 需求 | 方案 |
|------|------|
| 有哪些 agent？ | `nix flake show` (flake.nix = 注册表) |
| 版本锁定 | `version` + `outputHash` + `flake.lock` |
| 可复现 | 同一 git commit = bit-for-bit 相同产物 |
| 更新 | 改 version → build → 填新 hash → commit |
| 回滚 | `git revert` → 从 cache 秒恢复 |
| 分发 (有 Nix) | Nix binary cache (S3-backed), 增量传输 |
| 分发 (无 Nix) | tarball export → S3/OCI |

## 不做什么

- 不是评估框架（Harbor 等做这个）
- 不管 task image（benchmark adapter 提供）
- 不区分 blackbox/whitebox 的打包流程
- 只管：**打包 agent、提供沙箱运行接口**
