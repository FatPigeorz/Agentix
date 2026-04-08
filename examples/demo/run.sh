#!/usr/bin/env bash
set -euo pipefail

# ── harbor-nix End-to-End Demo ─────────────────────────────────────
#
# Builds runtime + claude-code agent closures, then tests both
# injection paths:
#   Path A (mount):  -v /nix/store:/nix/store:ro
#   Path B (upload): export tarball -> docker cp -> extract
#
# Usage:
#   ./demo/run.sh
# ───────────────────────────────────────────────────────────────────

# Source Nix if not already available
if ! command -v nix-build &>/dev/null; then
  if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
  else
    echo "ERROR: nix-build not found. Install Nix first." >&2
    exit 1
  fi
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

DOCKER_IMAGE="hnix-demo-task"
MOUNT_CONTAINER="hnix-demo-mount-$$"
UPLOAD_CONTAINER="hnix-demo-upload-$$"

# ── Helpers ────────────────────────────────────────────────────────

timer_start() { date +%s%N; }
timer_end() {
  local start=$1
  local end
  end=$(date +%s%N)
  local ms=$(( (end - start) / 1000000 ))
  echo "${ms}ms"
}

banner() {
  echo ""
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
  echo ""
}

cleanup() {
  echo ""
  echo "Cleaning up..."
  docker rm -f "$MOUNT_CONTAINER" "$UPLOAD_CONTAINER" 2>/dev/null || true
  rm -rf "$TMPDIR"
  echo "Done."
}
trap cleanup EXIT

# ── Step 1: Build closures ─────────────────────────────────────────

banner "Step 1: Building Nix closures"

echo "Building runtime closure..."
RUNTIME_PATH=$(nix-build "$REPO_ROOT/runtime/default.nix" --no-out-link)
echo "  Runtime: $RUNTIME_PATH"

echo "Building claude-code agent closure..."
AGENT_PATH=$(nix-build "$REPO_ROOT/agents/claude-code/default.nix" --no-out-link)
echo "  Agent:   $AGENT_PATH"

# ── Step 2: Build demo task container ──────────────────────────────

banner "Step 2: Building demo task container"

docker build -t "$DOCKER_IMAGE" "$DEMO_DIR/task" 2>&1 | tail -1
echo "  Image: $DOCKER_IMAGE"

# ── Step 3: Path A — Mount injection ──────────────────────────────

banner "Step 3: Path A — Mount injection (-v /nix/store)"

T_MOUNT=$(timer_start)

echo "Running: claude --version (via mount)"
docker run --rm \
  --name "$MOUNT_CONTAINER" \
  -v /nix/store:/nix/store:ro \
  -e "PATH=${AGENT_PATH}/bin:${RUNTIME_PATH}/bin:/usr/local/bin:/usr/bin:/bin" \
  "$DOCKER_IMAGE" \
  claude --version

MOUNT_ELAPSED=$(timer_end "$T_MOUNT")
echo ""
echo "  Mount path completed in: $MOUNT_ELAPSED"

# ── Step 4: Path B — Upload injection ─────────────────────────────

banner "Step 4: Path B — Upload injection (tarball)"

T_UPLOAD=$(timer_start)

# Export closures
echo "Exporting runtime closure..."
bash "$REPO_ROOT/scripts/export-closure.sh" "$RUNTIME_PATH" "$TMPDIR/runtime.tar.gz" 2>&1 | sed 's/^/  /'

echo "Exporting agent closure..."
bash "$REPO_ROOT/scripts/export-closure.sh" "$AGENT_PATH" "$TMPDIR/agent.tar.gz" 2>&1 | sed 's/^/  /'

# Start container
echo ""
echo "Starting container..."
docker run -d --name "$UPLOAD_CONTAINER" "$DOCKER_IMAGE" sleep infinity >/dev/null

# Upload and extract runtime
echo "Uploading runtime closure..."
docker cp "$TMPDIR/runtime.tar.gz" "$UPLOAD_CONTAINER":/tmp/runtime.tar.gz
docker exec "$UPLOAD_CONTAINER" tar xzf /tmp/runtime.tar.gz -C /
docker exec "$UPLOAD_CONTAINER" rm /tmp/runtime.tar.gz

# Upload and extract agent
echo "Uploading agent closure..."
docker cp "$TMPDIR/agent.tar.gz" "$UPLOAD_CONTAINER":/tmp/agent.tar.gz
docker exec "$UPLOAD_CONTAINER" tar xzf /tmp/agent.tar.gz -C /
docker exec "$UPLOAD_CONTAINER" rm /tmp/agent.tar.gz

# Run command
echo ""
echo "Running: claude --version (via upload)"
docker exec \
  -e "PATH=${AGENT_PATH}/bin:${RUNTIME_PATH}/bin:/usr/local/bin:/usr/bin:/bin" \
  "$UPLOAD_CONTAINER" \
  claude --version

UPLOAD_ELAPSED=$(timer_end "$T_UPLOAD")
echo ""
echo "  Upload path completed in: $UPLOAD_ELAPSED"

# ── Summary ────────────────────────────────────────────────────────

banner "Summary"

RUNTIME_SIZE=$(nix-store -qR "$RUNTIME_PATH" | xargs du -scb | tail -1 | cut -f1)
AGENT_SIZE=$(nix-store -qR "$AGENT_PATH" | xargs du -scb | tail -1 | cut -f1)

echo "  Runtime closure: $(numfmt --to=iec "$RUNTIME_SIZE")"
echo "  Agent closure:   $(numfmt --to=iec "$AGENT_SIZE")"
echo ""
echo "  Path A (mount):  $MOUNT_ELAPSED"
echo "  Path B (upload): $UPLOAD_ELAPSED"
echo ""
echo "Both injection paths verified successfully."
