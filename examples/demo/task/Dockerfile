# Minimal task container — NO agent installation needed.
# The agent runtime is volume-mounted from the Nix closure.
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY instruction.md /app/instruction.md

CMD ["sleep", "infinity"]
