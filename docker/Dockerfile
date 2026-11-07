# strata-complete image.
#
# The image holds ONLY the environment. The project folder is mounted at
# /workspace (docker-compose.yml): code and working folders (data/ in/ log/
# out/) are the same files inside and outside the container. No source file
# is copied in.
#
# Package index: if nexus.env exists in the project root, pip installs through
# Nexus; if not, it falls back to PyPI. nexus.env is read through a build-time
# bind mount, so it never ends up in an image layer (it may hold credentials).
# The hashes in requirements.txt are checked either way, so Nexus cannot serve
# different files than PyPI.
#
# Reproducibility:
# - every package is pinned and hash-checked (requirements.txt);
# - pip is not upgraded and the package is not installed: it is imported from
#   /workspace/src through PYTHONPATH;
# - the base image is pinned by digest: the tag is only for reading, the digest
#   decides. To update: pull the new image, read its digest, replace it in
#   BASE_IMAGE (RUNBOOK, Maintenance).
ARG BASE_IMAGE=python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
FROM ${BASE_IMAGE}

# The base image reference (with digest) is recorded in every output sidecar.
ARG BASE_IMAGE
ENV STRATA_BASE_IMAGE=${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/src \
    SC_ROOT=/workspace

RUN --mount=type=bind,target=/ctx \
    set -eu; \
    if [ -f /ctx/nexus.env ]; then \
        echo "package index: Nexus (nexus.env)"; \
        set -a; . /ctx/nexus.env; set +a; \
    else \
        echo "package index: PyPI (no nexus.env)"; \
    fi; \
    pip install --require-hashes --no-deps -r /ctx/requirements.txt

# git: used by VS Code Source Control inside the container. The repo is created
# on the host, so its owner differs from the container user: mark it safe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory /workspace

# Every shell says which container it is in: the projects share this image.
RUN echo 'echo "HOLA ${SC_CONTAINER_NAME:-$(hostname)}"' >> /etc/bash.bashrc

WORKDIR /workspace
CMD ["bash"]
