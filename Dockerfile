# aelix-mattermost: hardened single-container image. The gateway and every Aelix child
# run here as uid/gid 10001; only the /var/lib/aelix-mattermost volume is writable.
#
#   docker build -t aelix-mattermost:0.3.0 .
#   docker build --build-arg OFFLINE=1 -t aelix-mattermost:0.3.0 .   # air-gapped
#
# OFFLINE=1 installs only from deploy/docker/wheelhouse (wheels and tini_*.deb); an
# optional wheelhouse/constraints.txt pins every dependency. See docs/docker.md.
# BuildKit (the default builder since Docker 23) is required for RUN --mount.
# No configuration, token or provider key is copied into any layer.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ARG OFFLINE=0
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN --mount=type=bind,source=deploy/docker/wheelhouse,target=/wheelhouse \
    set -eu; \
    if [ "$OFFLINE" = "1" ]; then index=--no-index; else index=; fi; \
    python -m pip wheel --no-cache-dir --disable-pip-version-check --no-deps $index \
        --find-links /wheelhouse --wheel-dir /dist .

FROM python:${PYTHON_VERSION}-slim-bookworm
ARG AELIX_VERSION=0.1.0b2
ARG OFFLINE=0
# Image labels only; VERSION follows pyproject.toml.
ARG VERSION=0.3.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="aelix-mattermost" \
      org.opencontainers.image.description="Mattermost bot gateway for Aelix in a hardened single container" \
      org.opencontainers.image.source="https://github.com/handochan/aelix-mattermost" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      io.github.handochan.aelix-mattermost.aelix-version="${AELIX_VERSION}"

# tini is PID 1: it forwards SIGTERM to the gateway and reaps orphaned tool processes.
RUN --mount=type=bind,source=deploy/docker/wheelhouse,target=/wheelhouse \
    set -eu; \
    if [ "$OFFLINE" = "1" ]; then \
        set -- /wheelhouse/tini_*_"$(dpkg --print-architecture)".deb; \
        [ -f "$1" ] || { echo "OFFLINE=1 needs tini_*.deb in deploy/docker/wheelhouse" >&2; exit 1; }; \
        dpkg -i "$@"; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends tini; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    groupadd --gid 10001 aelix-mattermost; \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin \
        --home-dir /var/lib/aelix-mattermost/home aelix-mattermost; \
    install -d -m 0755 /etc/aelix-mattermost; \
    install -d -m 0700 -o 10001 -g 10001 /var/lib/aelix-mattermost \
        /var/lib/aelix-mattermost/home /var/lib/aelix-mattermost/aelix-agent \
        /var/lib/aelix-mattermost/state /var/lib/aelix-mattermost/workspace

# Aelix first (rarely changes), then this project's wheel. Both pip runs only bind-mount
# the wheelhouse, so no wheel or index credential ends up in a layer.
RUN --mount=type=bind,source=deploy/docker/wheelhouse,target=/wheelhouse \
    set -eu; \
    if [ "$OFFLINE" = "1" ]; then index=--no-index; else index=; fi; \
    if [ -f /wheelhouse/constraints.txt ]; then pins="-c /wheelhouse/constraints.txt"; else pins=; fi; \
    python -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        $index $pins --find-links /wheelhouse "aelix==${AELIX_VERSION}"
RUN --mount=type=bind,source=deploy/docker/wheelhouse,target=/wheelhouse \
    --mount=type=bind,from=build,source=/dist,target=/dist \
    set -eu; \
    if [ "$OFFLINE" = "1" ]; then index=--no-index; else index=; fi; \
    if [ -f /wheelhouse/constraints.txt ]; then pins="-c /wheelhouse/constraints.txt"; else pins=; fi; \
    python -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        $index $pins --find-links /wheelhouse /dist/aelix_mattermost-*.whl; \
    python -m pip check; \
    aelix-mattermost --version
# Optional Aelix extension packages (space-separated pip requirements, e.g.
# --build-arg EXTENSION_PACKAGES="my-aelix-tools==1.2"), installed next to Aelix from the
# wheelhouse or the index. List their modules in aelix.extensions and their tools in
# allowed_tools; `aelix-mattermost tools` shows what is installed.
ARG EXTENSION_PACKAGES=""
RUN --mount=type=bind,source=deploy/docker/wheelhouse,target=/wheelhouse \
    set -eu; \
    if [ -n "$EXTENSION_PACKAGES" ]; then \
        if [ "$OFFLINE" = "1" ]; then index=--no-index; else index=; fi; \
        if [ -f /wheelhouse/constraints.txt ]; then pins="-c /wheelhouse/constraints.txt"; else pins=; fi; \
        python -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
            $index $pins --find-links /wheelhouse $EXTENSION_PACKAGES; \
        python -m pip check; \
    fi

# Children inherit this environment: a writable HOME and agent dir on the volume, no
# bytecode writes on the read-only root and no user site-packages (a writable HOME must
# not let one conversation plant code that later processes import).
ENV HOME=/var/lib/aelix-mattermost/home \
    AELIX_CODING_AGENT_DIR=/var/lib/aelix-mattermost/aelix-agent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1
VOLUME ["/var/lib/aelix-mattermost"]
WORKDIR /var/lib/aelix-mattermost
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["aelix-mattermost", "healthcheck", "--config", "/etc/aelix-mattermost/config.toml"]
ENTRYPOINT ["tini", "--", "aelix-mattermost"]
CMD ["run", "--config", "/etc/aelix-mattermost/config.toml"]
