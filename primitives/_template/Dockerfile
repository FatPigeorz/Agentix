# Canonical agentix Dockerfile.
#
# Used by the runtime image build (`docker build -f primitives/_template/Dockerfile .`
# from the repo root). `agentix build` generates its own equivalent Dockerfile
# programmatically for namespace bundles, but the runtime image — which provides
# `/mnt/runtime/entry/bin/start` to every sandbox — is built directly from this
# file.
#
# Build context must contain `pyproject.toml` + `default.nix` (or a sibling that
# `default.nix` reads) so the nix derivation can pick up name + version.
ARG NIX_IMAGE=nixos/nix:latest

FROM ${NIX_IMAGE} AS builder
RUN mkdir -p ~/.config/nix && \
    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \
    nix-channel --update 2>/dev/null || true
WORKDIR /src
COPY . .
RUN nix-build --no-out-link default.nix -o ./result && \
    STORE_PATH=$(readlink -f ./result) && \
    mkdir -p /export/nix/store && \
    for p in $(nix-store -qR "$STORE_PATH"); do cp -a "$p" /export/nix/store/; done && \
    ln -s "$STORE_PATH" /export/nix/entry

FROM busybox:stable
COPY --from=builder /export /
VOLUME /nix
ARG NAMESPACE_NAME=unknown
LABEL org.agentix.namespace=1
LABEL org.agentix.namespace.name=${NAMESPACE_NAME}
