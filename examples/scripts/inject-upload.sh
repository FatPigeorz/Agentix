#!/usr/bin/env bash
set -euo pipefail

# ── Inject agent via tarball upload (Daytona / Modal / E2B) ─────
#
# Simulates the cloud path: start container → upload tarballs → run.
#
# Usage:
#   ./inject-upload.sh <agent.nix> <docker-image> [command...]
#
# Example:
#   ./inject-upload.sh agents/claude-code/default.nix ubuntu:24.04 claude --version
# ─────────────────────────────────────────────────────────────────

AGENT_NIX="${1:?Usage: $0 <agent.nix> <docker-image> [command...]}"
DOCKER_IMAGE="${2:?Usage: $0 <agent.nix> <docker-image> [command...]}"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

CONTAINER_NAME="hnix-upload-$$"

# Build and export closures
echo "Building runtime..."
RUNTIME_PATH=$(nix-build "$SCRIPT_DIR/../runtime/default.nix" --no-out-link)

echo "Building agent..."
AGENT_PATH=$(nix-build "$AGENT_NIX" --no-out-link)

echo "Exporting runtime closure..."
bash "$SCRIPT_DIR/export-closure.sh" "$RUNTIME_PATH" "$TMPDIR/runtime.tar.gz" 2>&1 | sed 's/^/  /'

echo "Exporting agent closure..."
bash "$SCRIPT_DIR/export-closure.sh" "$AGENT_PATH" "$TMPDIR/agent.tar.gz" 2>&1 | sed 's/^/  /'

echo ""
echo "Starting container: $DOCKER_IMAGE"
docker run -d --name "$CONTAINER_NAME" "$DOCKER_IMAGE" sleep infinity >/dev/null
trap "docker rm -f $CONTAINER_NAME >/dev/null 2>&1; rm -rf $TMPDIR" EXIT

# Upload and extract
echo "Uploading runtime closure..."
docker cp "$TMPDIR/runtime.tar.gz" "$CONTAINER_NAME":/tmp/runtime.tar.gz
docker exec "$CONTAINER_NAME" tar xzf /tmp/runtime.tar.gz -C /
docker exec "$CONTAINER_NAME" rm /tmp/runtime.tar.gz

echo "Uploading agent closure..."
docker cp "$TMPDIR/agent.tar.gz" "$CONTAINER_NAME":/tmp/agent.tar.gz
docker exec "$CONTAINER_NAME" tar xzf /tmp/agent.tar.gz -C /
docker exec "$CONTAINER_NAME" rm /tmp/agent.tar.gz

# Run command
echo ""
echo "Running: $*"
docker exec \
  -e "PATH=${AGENT_PATH}/bin:${RUNTIME_PATH}/bin:/usr/local/bin:/usr/bin:/bin" \
  "$CONTAINER_NAME" \
  "$@"
