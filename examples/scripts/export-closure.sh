#!/usr/bin/env bash
set -euo pipefail

# ── Export a Nix closure as a portable tarball ───────────────────
#
# Usage:
#   ./export-closure.sh <nix-file-or-store-path> <output.tar.gz>
#
# Examples:
#   ./export-closure.sh agents/claude-code/default.nix out/claude-code.tar.gz
#   ./export-closure.sh /nix/store/xxx-claude-code-runtime out/claude-code.tar.gz
# ─────────────────────────────────────────────────────────────────

INPUT="${1:?Usage: $0 <nix-file-or-store-path> <output.tar.gz>}"
OUTPUT="${2:?Usage: $0 <nix-file-or-store-path> <output.tar.gz>}"

# Resolve input: nix file → build, store path → use directly
if [[ "$INPUT" == /nix/store/* ]]; then
  STORE_PATH="$INPUT"
else
  echo "Building $INPUT..."
  STORE_PATH=$(nix-build "$INPUT" --no-out-link)
fi
echo "Store path: $STORE_PATH"

# Collect full closure
CLOSURE_PATHS=$(nix-store -qR "$STORE_PATH")
COUNT=$(echo "$CLOSURE_PATHS" | wc -l)
echo "Closure: $COUNT store paths"

# Create tarball
mkdir -p "$(dirname "$OUTPUT")"
tar czf "$OUTPUT" --hard-dereference -C / $(echo "$CLOSURE_PATHS" | sed 's|^/||')

SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo "Output: $OUTPUT ($SIZE)"

# Write metadata sidecar
META="${OUTPUT%.tar.gz}.meta"
cat > "$META" <<EOF
AGENT_STORE_PATH=$STORE_PATH
AGENT_BIN=$STORE_PATH/bin
EOF
echo "Meta: $META"
