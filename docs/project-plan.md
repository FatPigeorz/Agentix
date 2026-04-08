# harbor-nix 项目规划

## 核心定位

用 Nix 统一打包 agent runtimes，提供注入沙箱的工具链。不集成 Harbor，不修改 Harbor 代码。

## 架构

**所有 agent 都在沙箱内运行。** Blackbox/whitebox 区别只在打包方式。

```
┌─ 沙箱 ──────────────────────────────────────────────┐
│                                                      │
│  Task env              (benchmark adapter 提供)       │
│  ├── OS + 代码仓库 + 数据集                           │
│  └── tests/test.sh                                   │
│                                                      │
│  Runtime               (harbor-nix 提供)              │
│  └── hnix-server (Python/FastAPI, port 8000)          │
│      ├── GET  /health                                │
│      ├── POST /load         加载 agent closure        │
│      ├── POST /exec         执行命令                  │
│      ├── POST /upload       上传文件                  │
│      └── POST /upload-and-load  云端注入              │
│                                                      │
│  Agent process         (沙箱内运行)                   │
│  ├── Blackbox: closure 整包 (binary + all deps)       │
│  └── Whitebox: deps closure + source mount            │
│                                                      │
│  Observability         (agent 自带, 沙箱内发出)        │
│  └── LangSmith / LangFuse / OTEL (打在 deps 里)      │
│                                                      │
│  Debug (dev 模式)                                     │
│  └── debugpy 端口暴露, VSCode remote attach           │
│                                                      │
└──────────────────────────────────────────────────────┘

外部 orchestrator 通过 HTTP 与 hnix-server 通信
```

### 三个独立环境

| 环境 | 谁提供 | 内容 | 变化频率 |
|------|--------|------|---------|
| **Task** | Benchmark adapter | OS + 代码 + 测试 | 每个 benchmark 不同 |
| **Runtime** | harbor-nix | hnix-server + Python deps | 很少变 |
| **Agent** | harbor-nix | agent binary + deps (+ source for whitebox) | 跟 agent 版本走 |

三者独立打包：N tasks × 1 runtime × M agents，不产生组合爆炸。

### Agent 打包：blackbox vs whitebox

统一都是 `agents/xxx/flake.nix`，区别只在内部：

| | Blackbox | Whitebox |
|---|---|---|
| 代码来源 | npm/pip registry | 本地 repo |
| Closure 内容 | binary + all deps | deps closure + source (mount) |
| Dev 模式 | 无 | deps 固定, source volume mount, debugpy |
| 可观测 | 事后解析日志 (ATIF) | 实时 traces (LangSmith 等, 打在 deps 里) |
| 例子 | claude-code, codex, aider | terminus, 自研 agent |

### 注入方式

| 沙箱类型 | 方式 |
|---------|------|
| 本地 Docker / K8s | `-v /nix/store:/nix/store:ro` |
| Daytona / Modal / E2B | tarball upload via hnix-server API |

### Whitebox Dev 模式

```
开发者本地:
  VSCode ──debugpy attach (port 5678)──► 沙箱内 agent process
                                         ├── source: volume mount (实时同步)
                                         ├── deps: Nix closure (不变)
                                         └── observability: LangSmith SDK (deps 里)
```

改代码 → 立即生效（source mount），不用重建 closure。

## 工作项

### P0: Runtime server

- [x] hnix-server (FastAPI): /health, /load, /exec, /upload, /upload-and-load
- [x] Nix 打包 runtime
- [x] Client library (hnix.client.HnixClient)
- [x] End-to-end 验证 (mount + upload 路径)

### P1: Agent 打包

**第三方 agent (blackbox)**

| 类型 | Agents | 打包方式 |
|------|--------|---------|
| npm | claude-code, codex, gemini-cli, opencode, qwen-code, cline | FOD + `npm install --prefix` |
| pip | openhands, openhands-sdk, swe-agent, mini-swe-agent, kimi-cli, trae-agent | FOD + `pip install` |
| curl/other | aider, goose, cursor-cli, hermes, rovodev-cli | 解析安装脚本 → Nix 化 |

状态：claude-code PoC 已完成

**自研 agent (whitebox)**
- deps 和 source 分层打包
- Dev 模式：deps closure 固定, source volume mount
- Debug: debugpy 端口暴露
- Observability: LangSmith/OTEL SDK 作为 deps 打包

### P2: 工具链

- CI/CD: agent 版本更新自动 nix build + push to cache
- 脚手架: `harbor-nix init <agent-name>` 生成 flake.nix 模板
- Export 工具: closure → tarball 导出脚本

## 不做的事

- 不集成 Harbor（不改 Harbor 代码）
- 不替换 task image
- 不生成 N×M 组合 image
- 不把 agent 放到沙箱外运行
