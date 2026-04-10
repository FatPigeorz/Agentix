# Closure Interface Protocol

A closure is a self-contained package that exposes HTTP endpoints via a Unix socket.
Runtime server loads closures dynamically and reverse-proxies requests to them.

## ABI

A closure must:

1. Be a directory containing an executable (named `serve`, `main`, or in `bin/`)
2. Accept `--socket <path>` argument
3. Start an HTTP server on that Unix socket
4. Expose any endpoints it wants

That's it. Language, framework, endpoint count, request/response format — all up to the closure.

## Lifecycle

```
Orchestrator                          Runtime Server (sandbox)
    │                                      │
    │  POST /load                          │
    │  {"path": "/nix/store/xxx",          │
    │   "namespace": "swebench"}           │
    │ ────────────────────────────────►     │
    │                                      │  1. Find executable in closure dir
    │                                      │  2. Spawn: ./serve --socket /tmp/agentix/swebench.sock
    │                                      │  3. Wait for socket
    │                                      │  4. Register reverse proxy: /swebench/* → socket
    │  ◄──── {"status": "loaded"}          │
    │                                      │
    │  POST /swebench/setup                │
    │  {"instance_id": "..."}              │
    │ ────────────────────────────────►     │ ──proxy──► closure process
    │  ◄──── {"instruction": "..."}        │
    │                                      │
    │  POST /unload                        │
    │  {"namespace": "swebench"}           │
    │ ────────────────────────────────►     │  Kill process, remove socket
```

## Example Closure (Python)

```python
#!/usr/bin/env python3
"""SWE-bench closure: setup + eval endpoints."""

import argparse
import subprocess
import uvicorn
from fastapi import FastAPI

app = FastAPI()

@app.post("/setup")
def setup(data: dict):
    return {"instruction": data["problem_statement"], "workdir": "/testbed"}

@app.post("/eval")
def eval(data: dict):
    result = subprocess.run(["bash", "/tmp/eval.sh"], capture_output=True)
    return {"reward": 1.0 if result.returncode == 0 else 0.0}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    uvicorn.run(app, uds=args.socket)
```

## Example Closure (Bash + socat)

```bash
#!/bin/bash
# Minimal closure using socat
SOCKET="$2"  # --socket <path>

handle_request() {
    read -r line
    echo "HTTP/1.1 200 OK"
    echo "Content-Type: application/json"
    echo ""
    echo '{"status": "ok"}'
}

socat UNIX-LISTEN:$SOCKET,fork EXEC:handle_request
```

## Orchestrator Usage

```python
async with RuntimeClient(sandbox_url) as client:
    # Load closures
    await client.load("/nix/store/xxx-swebench", namespace="swebench")
    await client.load("/nix/store/xxx-claude-code", namespace="claude")

    # Call closure endpoints
    agent_input = await client.call("swebench", "setup", data=instance)
    agent_output = await client.call("claude", "run", data=agent_input)
    reward = await client.call("swebench", "eval", data=instance)

    # Core endpoints still work
    await client.exec("ls /testbed")
    await client.upload("local.py", "/tmp/local.py")
```

## Design Decisions

- **Unix socket, not stdin/stdout**: Avoids output pollution from logging/debug prints
- **HTTP, not gRPC/protobuf**: Universal, debuggable with curl, any language
- **Process per closure**: Isolation, independent crashes, independent dependencies
- **No manifest.json**: Convention over configuration — just be an executable
- **Reverse proxy**: Runtime server doesn't parse closure responses, just forwards bytes
