#!/usr/bin/env bash
set -euo pipefail

# ── Inject agent via volume mount (local Docker / K8s) ──────────
#
# Usage:
#   ./inject-mount.sh <agent.nix> <docker-image> [command...]
#
# Example:
#   ./inject-mount.sh agents/claude-code/default.nix ubuntu:24.04 claude --version
# ─────────────────────────────────────────────────────────────────

AGENT_NIX="${1:?Usage: $0 <agent.nix> <docker-image> [command...]}"
DOCKER_IMAGE="${2:?Usage: $0 <agent.nix> <docker-image> [command...]}"
shift 2

# Build runtime and agent
RUNTIME_PATH=$(nix-build "$(dirname "$0")/../runtime/default.nix" --no-out-link)
AGENT_PATH=$(nix-build "$AGENT_NIX" --no-out-link)

echo "Runtime: $RUNTIME_PATH"
echo "Agent:   $AGENT_PATH"
echo "Image:   $DOCKER_IMAGE"
echo ""

# Run with /nix/store mounted read-only
exec docker run --rm \
  -v /nix/store:/nix/store:ro \
  -e "PATH=${AGENT_PATH}/bin:${RUNTIME_PATH}/bin:/usr/local/bin:/usr/bin:/bin" \
  "$DOCKER_IMAGE" \
  "$@"
