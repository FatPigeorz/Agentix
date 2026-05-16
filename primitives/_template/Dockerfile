# Runtime image — Python slim + framework wheel.
#
# Built from the repo root:
#   docker build -t agentix/runtime:<version> -f primitives/_template/Dockerfile .
#
# Namespace bundles extend this with their own Python packages
# (pip-installed alongside `agentix`) and any system deps from Nix.
# At deploy time the bundle image runs directly — no mount-and-merge,
# no volume populate.

FROM python:3.11-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY agentix ./agentix
RUN python -m build --wheel --outdir /dist

FROM python:3.11-slim
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/agentix-*.whl && rm /tmp/agentix-*.whl
EXPOSE 8000
ENV AGENTIX_BIND_PORT=8000
ENTRYPOINT ["agentix-server"]
