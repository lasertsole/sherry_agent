# syntax=docker/dockerfile:1

# EMA AI Agent backend — hermetic/cloud profile.
#
# Heavy local-inference packages (llama-cpp-python, funasr, torch) are NOT
# installed: server startup with the default cloud-LLM profile was verified
# to work without them (Robyn boots, HTTP reachable, all services up). This
# avoids a multi-GB CUDA/torch download and a llama-cpp-python source build.
#
#   * With this image set EMBEDDING_MODEL_LOCAL=false (cloud embedding):
#     local GGUF models (embedding / reranker / ITTT / VTTT / auxiliary LLM)
#     require llama-cpp-python, which is not installed here.
#   * To enable local GGUF models: remove the three --no-install-package
#     flags in the builder stage AND add `gcc g++ cmake` to that stage.
#
# podman on WSL2: WSL2 kernels ship without nftables support, so netavark
# cannot set up bridge networks or port mappings — RUN steps fail with
# "nftables error". Build and run with `--network host` there (real Linux
# or Docker Desktop environments need no such flag):
#   podman build --network host -t sherry-agent:dev .
#   podman run -d --network host --env-file .env <image>
#
# Run:  docker run -p 8080:8080 --env-file .env <image>
# The API binds API_HOST=0.0.0.0 inside the container (env-overridable, see
# config/__init__.py); port via API_PORT (default 8080).

FROM python:3.13-slim AS builder

# uv pinned to the version that generated uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first for layer caching. uv.lock references the local wheel
# below via [tool.uv.sources], so it must be present before `uv sync`.
#
# --no-install-package: the hermetic/cloud profile verified to boot the
# server (Robyn up, HTTP reachable). torch/torchvision alone would still
# drag in the ~2.3GB Linux CUDA closure (nvidia-*, triton) — none of it is
# imported at server startup. NOTE: the nvidia-cu13 names follow the torch
# major in uv.lock; revisit if torch is ever bumped.
#
# UV_DEFAULT_INDEX: override at build time for a regional PyPI mirror, e.g.
#   podman build --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# UV_CONCURRENT_DOWNLOADS=4: bounded download concurrency keeps the build
# reliable on flaky links (uv's default 50 can saturate proxies/NAT).
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
COPY pyproject.toml uv.lock ./
COPY models/STT_model/editdistance-0.8.1-py3-none-any.whl models/STT_model/
RUN UV_CONCURRENT_DOWNLOADS=4 uv sync --frozen --no-dev \
    --default-index "$UV_DEFAULT_INDEX" \
    --no-install-package llama-cpp-python \
    --no-install-package funasr \
    --no-install-package torch \
    --no-install-package torchvision \
    --no-install-package triton \
    --no-install-package nvidia-cublas \
    --no-install-package nvidia-cuda-cupti \
    --no-install-package nvidia-cuda-nvrtc \
    --no-install-package nvidia-cuda-runtime \
    --no-install-package nvidia-cudnn-cu13 \
    --no-install-package nvidia-cufft \
    --no-install-package nvidia-cufile \
    --no-install-package nvidia-curand \
    --no-install-package nvidia-cusolver \
    --no-install-package nvidia-cusparse \
    --no-install-package nvidia-cusparselt-cu13 \
    --no-install-package nvidia-nccl-cu13 \
    --no-install-package nvidia-nvjitlink \
    --no-install-package nvidia-nvshmem-cu13


FROM python:3.13-slim

ENV PATH="/app/.venv/bin:$PATH" \
    API_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# libglib2.0-0: runtime .so dependency of opencv-python-headless on Debian
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY . .

# Pre-create runtime-writable dirs (static/ silences a Robyn serve_directory
# error; the rest is app data — mount volumes here in production).
RUN mkdir -p static temp logs/output src/images src/audio src/video src/checkpoints \
    && useradd --create-home appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# Robyn backend (same entry as start.sh): HTTP + WebSocket (/sessions/ws)
CMD ["python", "-m", "server", "--fast", "--disable-openapi"]

# TCP-level healthcheck: an accepted connection means the server is up,
# regardless of which HTTP status a given route returns.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), 3)"]
