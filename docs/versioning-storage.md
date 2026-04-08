# harbor-nix 版本管理与存储方案

## 概述

大团队 CI 驱动，需要：
- 版本锁定与可复现构建
- 多人多机共享构建产物（不重复 build）
- 版本回滚
- 构建状态可查询（哪些 agent 的哪些版本已构建）

## 架构

```
Source of truth          Build            Storage              Consume
─────────────────       ─────            ───────              ───────

flake.nix               CI               Nix Binary Cache     开发者机器
flake.lock         →    nix build   →    (S3-backed)      →   评估集群
agents/*/flake.nix      per agent        + OCI Registry       云沙箱
                                         (tarball artifacts)
```

## 1. 版本管理

### 三层版本锁定

```
flake.lock                  # 锁 nixpkgs 版本 (glibc, node, python 等基础依赖)
agents/claude-code/
  └── flake.nix             # 锁 agent 版本 (version = "2.1.96")
                            # 锁 agent 内容 (outputHash = "sha256-xxx")
git commit                  # 锁定一切：flake.lock + 所有 agent 版本 = 完全可复现
```

**一个 git commit = 一组确定的 agent closures。** 任何人在任何时间 checkout 同一 commit，`nix build` 产出完全一样（bit-for-bit identical）。

### Agent 版本矩阵

```nix
# agents/claude-code/flake.nix
{
  version = "2.1.96";                    # agent 上游版本
  outputHash = "sha256-WgJ45G8F...";     # 内容 hash = 版本锁
}
```

| 信息 | 来源 | 锁定方式 |
|------|------|---------|
| Agent 上游版本 | `version` 字段 | 显式声明 |
| Agent 依赖树 | `outputHash` | 内容寻址 (hash 变 = 内容变) |
| 系统依赖 (node, python, glibc) | `flake.lock` → nixpkgs commit | flake lock |
| 完整快照 | git commit | git |

### 更新流程

```bash
# 更新一个 agent
vi agents/claude-code/flake.nix          # 改 version, 清空 outputHash
nix build .#claude-code                  # 构建失败, 打印正确 hash
vi agents/claude-code/flake.nix          # 填入新 hash
nix build .#claude-code                  # 构建成功
git commit -m "bump claude-code to 2.2.0"

# 更新系统依赖 (node, python 等)
nix flake update                         # 更新 flake.lock
nix build .#claude-code                  # 重新构建 (hash 可能变)
git commit -m "update nixpkgs"

# 回滚
git revert HEAD                          # 回到上一个版本
nix build .#claude-code                  # 从 cache 秒取 (已构建过)
```

## 2. 存储

### 双层存储

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Nix Binary Cache (S3-backed)              │
│                                                     │
│  s3://harbor-nix-cache/                             │
│  ├── nar/sha256-xxx.nar.xz      # 每个 store path  │
│  └── narinfo/xxx.narinfo         # 元数据           │
│                                                     │
│  用途: nix build 时自动查 cache                      │
│        命中 → 直接下载, 不重新 build                  │
│  工具: nix copy --to s3://...                        │
│  适用: 本地 Docker / K8s (有 Nix 的环境)             │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Layer 2: OCI Registry / Object Storage             │
│                                                     │
│  s3://harbor-nix-artifacts/                         │
│  ├── runtime/0.1.0/closure.tar.gz                   │
│  ├── agents/claude-code/2.1.96/closure.tar.gz       │
│  └── agents/claude-code/2.1.96/metadata.json        │
│                                                     │
│  或 OCI:                                            │
│  registry.example.com/harbor-nix/claude-code:2.1.96 │
│                                                     │
│  用途: 云沙箱 (Daytona/Modal) 下载 tarball           │
│        没有 Nix 的环境直接 tar xzf                   │
│  工具: scripts/export-closure.sh + upload            │
│  适用: 任何环境                                      │
└─────────────────────────────────────────────────────┘
```

### 为什么两层？

| | Nix Binary Cache | Tarball / OCI |
|---|---|---|
| 速度 | 最快 (增量, 只传缺的 store paths) | 整包传输 |
| 去重 | 自动 (store path 级别) | 无 |
| 依赖 | 需要 Nix | 不需要任何工具 |
| 适用 | 开发者机器, 有 Nix 的 CI | 云沙箱, 没有 Nix 的环境 |

有 Nix 的地方用 cache（快、省空间），没 Nix 的地方用 tarball（通用）。

## 3. CI/CD Pipeline

```yaml
# .github/workflows/build-agents.yml (示意)
on:
  push:
    paths: ['agents/**', 'runtime/**', 'flake.nix', 'flake.lock']

jobs:
  build:
    strategy:
      matrix:
        agent: [claude-code, codex, openhands, aider, ...]

    steps:
      # 1. 构建
      - run: nix build .#${{ matrix.agent }}

      # 2. 推送到 Nix binary cache
      - run: nix copy --to s3://harbor-nix-cache .#${{ matrix.agent }}

      # 3. 导出 tarball + 推送到 S3
      - run: |
          scripts/export-closure.sh .#${{ matrix.agent }} \
            out/${{ matrix.agent }}.tar.gz
          aws s3 cp out/${{ matrix.agent }}.tar.gz \
            s3://harbor-nix-artifacts/agents/${{ matrix.agent }}/$(nix eval .#${{ matrix.agent }}.version)/

      # 4. (可选) 推送到 OCI registry
      - run: |
          nix build .#${{ matrix.agent }}-image
          docker push registry.example.com/harbor-nix/${{ matrix.agent }}:latest

  # 构建 runtime (只在 runtime/ 变更时)
  build-runtime:
    steps:
      - run: nix build .#runtime
      - run: nix copy --to s3://harbor-nix-cache .#runtime
      - run: scripts/export-closure.sh .#runtime out/runtime.tar.gz
      - run: aws s3 cp out/runtime.tar.gz s3://harbor-nix-artifacts/runtime/$(nix eval .#runtime.version)/
```

### CI 触发策略

| 触发条件 | 构建范围 |
|---------|---------|
| `agents/claude-code/` 变更 | 只构建 claude-code |
| `flake.lock` 变更 | 构建全部 agent (系统依赖可能变了) |
| `runtime/` 变更 | 只构建 runtime |
| 定时 (每天/每周) | 全量构建, 检查上游新版本 |

## 4. 消费方式

### 开发者本地 (有 Nix)

```bash
# 配置 cache (一次性)
nix.conf:
  substituters = https://cache.nixos.org s3://harbor-nix-cache
  trusted-public-keys = harbor-nix:xxx

# 使用: 自动从 cache 拉取, 不用本地 build
nix build .#claude-code    # 秒完成 (cache hit)
```

### 评估集群 (本地 Docker / K8s)

```bash
# Node 上有 Nix → 从 cache 拉取
nix build .#claude-code --no-out-link
# 然后 volume mount 到容器
docker run -v /nix/store:/nix/store:ro ...
```

### 云沙箱 (Daytona / Modal, 没有 Nix)

```bash
# 从 S3 下载 tarball
aws s3 cp s3://harbor-nix-artifacts/agents/claude-code/2.1.96/closure.tar.gz .
# 通过 hnix-server API 上传到沙箱
curl -X POST http://sandbox:8000/upload-and-load \
  -F file=@closure.tar.gz \
  -F closure_store_path=/nix/store/xxx-claude-code-runtime-2.1.96
```

## 5. 查询接口

```bash
# 列出所有可用 agent
nix flake show

# 查看某个 agent 版本
nix eval .#claude-code.version

# 查看 S3 上已构建的版本
aws s3 ls s3://harbor-nix-artifacts/agents/claude-code/

# 查看 cache 中是否有某个 closure
nix path-info --store s3://harbor-nix-cache .#claude-code
```

## 6. 目录结构

```
harbor-nix/
├── flake.nix                  # 顶层: 注册所有 agent + runtime
├── flake.lock                 # 锁定 nixpkgs
├── runtime/
│   ├── hnix/                  # Python runtime server
│   ├── pyproject.toml
│   └── default.nix            # runtime Nix 打包
├── agents/
│   ├── claude-code/flake.nix  # outputHash 锁定
│   ├── codex/flake.nix
│   ├── openhands/flake.nix
│   └── my-agent/flake.nix
├── scripts/
│   ├── export-closure.sh      # 导出 tarball
│   ├── inject-mount.sh
│   └── inject-upload.sh
└── ci/
    └── build-agents.yml       # CI pipeline
```

## 总结

| 问题 | 方案 |
|------|------|
| 有哪些 agent 可用？ | `nix flake show` / flake.nix 即注册表 |
| 版本锁定 | `version` + `outputHash` + `flake.lock` + git commit |
| 可复现 | 同一 commit = bit-for-bit 相同产物 |
| 构建 | CI 自动, 按 agent 并行 |
| 存储 | Nix cache (有 Nix) + S3 tarball (通用) |
| 分发 | 有 Nix → `nix copy` / cache; 无 Nix → tarball download |
| 回滚 | `git revert` → 从 cache 秒恢复 |
| 查询 | `nix eval` / `nix path-info` / `aws s3 ls` |
