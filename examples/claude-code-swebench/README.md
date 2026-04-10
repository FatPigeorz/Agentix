# Example: Claude Code on SWE-bench

Run Claude Code on SWE-bench Verified instances using the Agentix closure protocol.

## Prerequisites

- Docker
- Python 3.11+
- `pip install datasets swebench`
- Anthropic API key (or proxy)

## Setup

```bash
# Clone all three repos
git clone https://github.com/Agentiix/Agentix.git
git clone https://github.com/Agentiix/Agentix-Agents-Hub.git
git clone https://github.com/Agentiix/Agentix-Datasets.git

# Install agentix
cd Agentix && pip install -e .
```

## Step 1: Preprocess dataset

Download SWE-bench Verified and convert to Agentix format:

```bash
cd ../Agentix-Datasets
python swebench/preprocess.py --output ../Agentix/examples/claude-code-swebench/data.jsonl
```

Each line is one instance:
```json
{"instance_id": "django__django-16139", "problem_statement": "...", "image": "sweb.eval.x86_64.django__django-16139:latest"}
```

## Step 2: Pull SWE-bench Docker image

```bash
# Get the image name from your instance
IMAGE=$(head -1 examples/claude-code-swebench/data.jsonl | python -c "import sys,json; print(json.load(sys.stdin)['image'])")

docker pull $IMAGE
```

## Step 3: Start runtime server inside a SWE-bench container

Pick an instance from `data.jsonl` and start a container with the runtime server:

```bash
# Extract one instance
INSTANCE=$(head -1 examples/claude-code-swebench/data.jsonl)
IMAGE=$(echo $INSTANCE | python -c "import sys,json; print(json.load(sys.stdin)['image'])")

# Save instance to file
echo $INSTANCE > /tmp/instance.json

# Start container with runtime server
docker run -d --name sandbox --network host \
  -v $(pwd):/opt/agentix:ro \
  -v $(realpath ../Agentix-Agents-Hub):/opt/agent:ro \
  -v $(realpath ../Agentix-Datasets):/opt/dataset:ro \
  -w /testbed \
  $IMAGE sleep infinity

# Install runtime inside container
docker exec sandbox pip install -e /opt/agentix
docker exec sandbox pip install fastapi uvicorn httpx

# Install Node.js + Claude CLI
docker exec sandbox bash -c "\
  apt-get update -qq && apt-get install -y -qq curl ca-certificates sudo > /dev/null 2>&1 && \
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - > /dev/null 2>&1 && \
  apt-get install -y -qq nodejs > /dev/null 2>&1 && \
  npm install -g @anthropic-ai/claude-code > /dev/null 2>&1"

# Create non-root user (claude requires it)
docker exec sandbox bash -c "\
  useradd -m -s /bin/bash agent 2>/dev/null; \
  chown -R agent:agent /testbed; \
  echo 'agent ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers"

# Start runtime server (background)
docker exec -d sandbox python -m agentix.runtime --port 8000
```

## Step 4: Run the example

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

cd Agentix
python examples/claude-code-swebench/run.py \
  --server http://localhost:8000 \
  --agent /opt/agent/claude-code \
  --dataset /opt/dataset/swebench \
  --instance-file /tmp/instance.json \
  --output result.json
```

The paths `/opt/agent/claude-code` and `/opt/dataset/swebench` are inside the container
(mounted from host via `-v`).

## Step 5: Check results

```bash
cat result.json | python -m json.tool
```

## Step 6: Cleanup

```bash
docker rm -f sandbox
```

## Using a proxy (no Anthropic API key)

If you have an OpenAI-compatible endpoint instead:

```bash
# Set these before running
export ANTHROPIC_BASE_URL="http://localhost:8082"
export ANTHROPIC_API_KEY="dummy"
```

## Pipeline overview

```
Orchestrator (run.py)                Runtime Server (sandbox)
    │                                      │
    │  load /opt/dataset/swebench          │
    │──────────────────────────────────►    │── spawn serve --socket swebench.sock
    │  load /opt/agent/claude-code         │
    │──────────────────────────────────►    │── spawn serve --socket claude.sock
    │                                      │
    │  call swebench/setup                 │
    │──────────────────────────────────►    │──► returns {instruction, workdir}
    │                                      │
    │  call claude/run                     │
    │──────────────────────────────────►    │──► calls claude CLI → returns {patch}
    │                                      │
    │  call swebench/verify                │
    │──────────────────────────────────►    │──► runs tests → returns {pass, reason}
```
