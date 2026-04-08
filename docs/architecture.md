# harbor-nix 架构

## 做什么

用 Nix 打包 AI agent 运行环境，注入到评估沙箱中，提供统一接口操作沙箱。

- **打包**: Nix closure，binary + 全部依赖，hash-pinned 可复现
- **Runtime server**: 沙箱内 HTTP 服务，统一接口屏蔽 deployment 差异
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
│  └── 调用 Sandbox HTTP API 操作沙箱内部                          │
│       POST /exec, POST /upload, GET /download                   │
│       不关心底下是 Docker 还是 Modal                              │
│                                                                 │
└────────────┬───────────────────────────────────┬───────────────┘
             │ Deployment 接口                    │ Sandbox 接口
             │ (CRUD)                             │ (HTTP, 统一)
             ▼                                    ▼
┌─ Deployment ──────────────┐    ┌─ Sandbox ──────────────────────┐
│                            │    │                                │
│  沙箱的 CRUD:              │    │  hnix-server (port 8000)       │
│  Create  创建+注入+启动    │    │  ├── POST /exec                │
│  Read    查询状态          │    │  ├── POST /upload              │
│  Update  更新配置          │    │  ├── GET  /download            │
│  Delete  销毁释放          │    │  └── GET  /health              │
│                            │    │                                │
│  实现:                     │    │  不知道自己在哪里运行           │
│  - DockerDeployment        │    │  不知道 closure 怎么来的        │
│  - K8sDeployment           │    │  只管响应 HTTP 请求             │
│  - DaytonaDeployment       │    │                                │
│  - ModalDeployment         │    │                                │
└────────────────────────────┘    └────────────────────────────────┘
```

## Host

运行 orchestrator 和评估代码的机器。

**职责**:
- `nix build` 构建 runtime 和 agent closures
- 调 Deployment CRUD 管理沙箱生命周期
- 通过统一的 HTTP API 操作沙箱（不关心底层是什么基础设施）
- 编排评估流程、收集结果

## Deployment

沙箱的 CRUD。不同基础设施各自实现，对 Host 暴露统一接口。

**核心价值**: 把基础设施差异封死在 `create` 里。Create 之后，所有 Deployment 对外都是同一个 HTTP 接口。

### 接口

```python
class SandboxConfig(BaseModel):
    task_image: str          # benchmark 提供的 Docker image
    runtime_closure: str     # Nix store path (runtime)
    agent_closure: str       # Nix store path (agent)


class SandboxInfo(BaseModel):
    sandbox_id: str          # 唯一标识
    hnix_server_url: str     # e.g. "http://localhost:18000"
    status: str              # "running" | "stopped" | "error"


class Deployment(ABC):

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """创建沙箱。一步完成:
        1. 基于 task_image 创建容器/沙箱
        2. 注入 runtime closure
        3. 注入 agent closure
        4. 设置 PATH
        5. 启动 hnix-server
        返回 sandbox_id + hnix_server_url
        """

    async def get(self, sandbox_id: str) -> SandboxInfo:
        """查询沙箱状态。"""

    async def update(self, sandbox_id: str, config: SandboxConfig) -> SandboxInfo:
        """更新沙箱 (如换 agent closure)。"""

    async def delete(self, sandbox_id: str) -> None:
        """销毁沙箱，释放资源。"""
```

### 各实现的 Create 差异

| 步骤 | Docker | K8s | Daytona | Modal |
|------|--------|-----|---------|-------|
| 创建 | `docker run -d` | create pod | `create_sandbox()` | `Sandbox.create()` |
| 注入 | `-v /nix/store:ro` | PV mount | upload tarball | Volume / upload |
| 启动 runtime | 容器 CMD | container command | `exec(hnix-server)` | sandbox exec |
| 返回 URL | `localhost:{port}` | `pod-ip:8000` | `sandbox.url` | `sandbox.url` |

Create 返回后，Deployment 退到后台。Host 直接跟 Sandbox HTTP API 对话。

## Sandbox

运行 task + agent 的隔离环境。内部有 hnix-server。

### Runtime server 的价值

Runtime server 不是技术必要性，而是**统一接口**。

没有 runtime server:
```
评估代码要适配每种 deployment:
  if docker:   docker exec container cmd
  elif k8s:    kubectl exec pod cmd
  elif daytona: daytona.exec(sandbox, cmd)
  elif modal:   sandbox.exec(cmd)
```

有 runtime server:
```
评估代码只写一次:
  POST /exec {"command": "claude --version"}
  POST /upload (file)
  GET  /download?path=/app/result.txt

不管底下是 Docker/K8s/Daytona/Modal
```

### 沙箱内结构

```
/opt/hnix/
├── runtime/    → hnix-server + Python deps
└── agent/      → agent binary + all deps
    ├── bin/claude
    ├── bin/node
    └── lib/node_modules/...

PATH=/opt/hnix/agent/bin:/opt/hnix/runtime/bin:$PATH
```

### HTTP API

```
GET  /health
  → { "status": "ok", "version": "0.1.0" }

POST /exec
  ← { "command": "...", "timeout": 60, "cwd": "/app", "env": {...} }
  → { "exit_code": 0, "stdout": "...", "stderr": "" }

POST /upload
  ← multipart: file + path
  → { "path": "/app/test.py", "size": 1024 }

GET  /download?path=/app/result.txt
  → file content (application/octet-stream)
```

## Agent 打包

所有 agent 都是 `agents/xxx/flake.nix`，产出都是 Nix closure。Blackbox/whitebox 不区分流程。

| | Blackbox | Whitebox |
|---|---|---|
| 例子 | claude-code, codex, aider | terminus, 自研 agent |
| 代码来源 | npm/pip registry | 本地 repo |
| Closure 内容 | binary + all deps | deps + source (或 source mount) |
| Dev 模式 | 无 | deps 固定, source mount, debugpy |
| 可观测性 | 事后解析日志 | LangSmith/OTEL (打在 deps 里) |

打包方式:
```bash
nix build .#claude-code   # → /nix/store/xxx-claude-code-runtime-2.1.96
nix build .#openhands     # → /nix/store/xxx-openhands-runtime-0.x
nix build .#my-agent      # → /nix/store/xxx-my-agent-0.1.0
```

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
 │  ◄── SandboxInfo               │                       │
 │      { hnix_server_url }       │  (退到后台)            │
 │                                │                       │
 │  POST /exec ─────────────────────────────────────────►│
 │  {"command": "claude -p 'fix the bug'"}               │
 │  ◄── {"exit_code": 0, "stdout": "..."}               │
 │                                │                       │
 │  POST /upload (test files) ──────────────────────────►│
 │  GET /download (results) ────────────────────────────►│
 │                                │                       │
 │  deployment.delete(id) ───────►│──► 销毁 ─────────────►│ ✗
 │                                │                       │
```

## 关注点分离

| 层 | 关注 | 不关注 |
|---|------|--------|
| **Host** | 构建 closure、编排流程、收集结果 | 基础设施类型、closure 注入方式 |
| **Deployment** | 沙箱 CRUD、注入 closure、启动 runtime | closure 内容、agent 怎么运行 |
| **Sandbox** | 执行命令、传文件 | 自己在哪、closure 从哪来 |

## 不做什么

- 不是评估框架（Harbor 等做这个）
- 不管 task image（benchmark adapter 提供）
- 不管 agent 运行逻辑（agent 自己的事）
- 不区分 blackbox/whitebox 的打包流程
- 只管：**打包、注入、提供统一运行接口**
