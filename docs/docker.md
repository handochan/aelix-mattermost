# Docker sandbox (single container)

The Docker option runs the gateway **and every Aelix child it starts** in one hardened
container: uid/gid 10001, read-only root filesystem, no capabilities, `no-new-privileges`,
memory/PID/CPU limits, `tini` as PID 1 and the bot token from a file secret. Only the
`/var/lib/aelix-mattermost` volume and a 64 MiB `noexec` `/tmp` are writable. This is
Tier 1: it protects the host from the bot, not one conversation from another (see
[Security model](#security-model)).

Files: [Dockerfile](../Dockerfile), [compose.yaml](../deploy/docker/compose.yaml),
[config.toml.example](../deploy/docker/config.toml.example),
[models.json.example](../deploy/docker/models.json.example),
[provider.env.example](../deploy/docker/provider.env.example),
[smoke_test.py](../deploy/docker/smoke_test.py).

## Docker or systemd?

| | Docker (`deploy/docker/`) | systemd ([unit](../deploy/aelix-mattermost.service)) |
| --- | --- | --- |
| Runtime | One image with a pinned Aelix; no Python on the host | venv and Aelix in a service account |
| Writable paths | The state volume and `/tmp` only | `/var/lib/aelix-mattermost` (`ReadWritePaths=`) and `PrivateTmp` |
| Stop cleanup | The kernel kills everything left in the container's PID namespace | `KillMode=mixed` kills what is left in the service cgroup |
| Limits | `mem_limit`, `pids_limit`, `cpus` | `MemoryMax=`, `TasksMax=` you add |
| Upgrade/rollback | Image tags | Wheels in a venv |
| Extra trust | The Docker daemon (root-equivalent) | None |

Both run every conversation under one account. Prefer Docker for a packaged read-only
runtime with resource ceilings; prefer systemd when the host must not run a container engine.

## Quick start

Requires Docker Engine 23+ with Compose v2 (BuildKit is the default builder), or Docker
Desktop/OrbStack, a Member-role bot token and a model endpoint Aelix supports.

```bash
cd deploy/docker
cp config.toml.example config.toml          # url, allowed_users, model
cp models.json.example models.json          # provider endpoint and model
cp provider.env.example provider.env && chmod 600 provider.env
mkdir -p extensions secrets
printf 'Mattermost bot token: '; read -rs token; echo
(umask 077; printf '%s' "$token" > secrets/mattermost_token); unset token
sudo chown 10001:10001 secrets/mattermost_token   # Linux hosts only, see below
docker compose build
docker compose run --rm aelix-mattermost check-config --config /etc/aelix-mattermost/config.toml
docker compose run --rm aelix-mattermost doctor --config /etc/aelix-mattermost/config.toml --check-aelix
docker compose up -d
docker compose ps        # "healthy" once the Mattermost WebSocket is connected
docker compose logs -f
```

Compose bind-mounts file secrets and ignores `uid`/`gid`/`mode`, so on Linux the token file
must be readable by uid 10001 (`chown` as above, mode 0600). Docker Desktop and OrbStack map
ownership for you. Rootless Docker and Podman map uid 10001 to a subordinate uid: change
ownership inside the user namespace, e.g. `podman unshare chown 10001:10001 secrets/mattermost_token`.
`config.toml`, `models.json` and `extensions/` must be readable by uid 10001 too; mode 0644 is
fine while keys stay in `provider.env`, which Compose reads on the host. The repository's
`.gitignore` excludes these local files (`config.toml`, `models.json`, `provider.env`,
`secrets/`, `extensions/`) and `*.tgz` archives, so `git add -A` does not pick them up.

| Host (`deploy/docker/`) | Container | Purpose |
| --- | --- | --- |
| `config.toml` | `/etc/aelix-mattermost/config.toml` (read-only) | Gateway configuration |
| `secrets/mattermost_token` | `/run/secrets/mattermost_token` | Bot token, read through `token_file` |
| `models.json` | `/var/lib/aelix-mattermost/aelix-agent/models.json` (read-only) | Aelix providers and models |
| `provider.env` | Environment | Provider API keys named by `models.json` |
| `extensions/` | `/etc/aelix-mattermost/extensions` (read-only) | Optional domain-tool extension files |
| Volume `aelix-mattermost_state` | `/var/lib/aelix-mattermost` | `state/` (database, sessions, transcripts, `health.json`), `workspace/`, `home/`, `aelix-agent/` |

Use absolute container paths in `config.toml`: relative paths resolve against the read-only
`/etc/aelix-mattermost`. Configuration is read at start; run `docker compose restart` after edits.

## Provider configuration

Aelix reads `models.json` from its agent directory (`AELIX_CODING_AGENT_DIR`, set by the
image). The example defines provider `internal` with model `my-model`, which `config.toml`
selects with `model = "internal/my-model"`; `doctor --check-aelix` must report a resolved
model. `apiKey` names a variable from `provider.env` (`INTERNAL_LLM_API_KEY`); Aelix uses the
variable's value when it is set and the literal string otherwise.

`ca_file` covers only the Mattermost connection. For a model endpoint behind an internal CA,
either set `SSL_CERT_FILE` in `provider.env` to a mounted bundle that contains the public roots
plus your CA, or build a derived image:

```dockerfile
FROM aelix-mattermost:0.2.0
USER 0
COPY internal-ca.crt /usr/local/share/ca-certificates/internal-ca.crt
RUN update-ca-certificates
USER 10001:10001
```

MCP servers stay off unless `aelix.mcp_config` names a file you mount read-only. Domain-tool
extensions under `extensions/` can import `aelix_mattermost.context`, which is installed in the
same environment as Aelix.

## Closed networks

**Build outside, move the image (recommended).**

```bash
docker build -t aelix-mattermost:0.2.0 .                  # repository root
docker save aelix-mattermost:0.2.0 | gzip > aelix-mattermost-0.2.0.tar.gz
sha256sum aelix-mattermost-0.2.0.tar.gz > aelix-mattermost-0.2.0.tar.gz.sha256
# inside the closed network, next to deploy/docker:
sha256sum -c aelix-mattermost-0.2.0.tar.gz.sha256
docker load -i aelix-mattermost-0.2.0.tar.gz
docker compose up -d --no-build
```

Build for the target CPU (`docker build --platform linux/amd64 ...` on an Arm workstation).

**Build inside from a wheelhouse.** On a connected machine with the target CPU architecture,
from the repository root, collect every wheel (the list mirrors Aelix plus the dependencies in
`pyproject.toml`) and the `tini` package into the wheelhouse and save the base image:

```bash
docker run --rm -v "$PWD/deploy/docker/wheelhouse:/wheelhouse" python:3.12-slim-bookworm sh -ec '
  pip download --dest /wheelhouse "aelix==0.1.0b2" "aiohttp>=3.10,<4" "setuptools>=68" wheel
  apt-get update && cd /wheelhouse && apt-get download tini'
docker save python:3.12-slim-bookworm | gzip > python-3.12-slim-bookworm.tar.gz
```

Move the repository (with the wheelhouse) and the base image, then build with no index:

```bash
docker load -i python-3.12-slim-bookworm.tar.gz
docker build --build-arg OFFLINE=1 -t aelix-mattermost:0.2.0 .
```

`OFFLINE=1` adds `--no-index` to every pip call and installs `tini` from the `.deb`. The
wheelhouse is bind-mounted during the build and never stored in the image. To pin every
transitive version (online builds too), first write `constraints.txt` from a tested image and
pass it to the download: the build applies `wheelhouse/constraints.txt` automatically.

```bash
docker run --rm --entrypoint pip aelix-mattermost:0.2.0 freeze --exclude aelix-mattermost \
  > deploy/docker/wheelhouse/constraints.txt
# then add: -c /wheelhouse/constraints.txt to the pip download above
```

At run time `offline = true` disables Aelix's own update/catalog traffic; the container only
needs Mattermost and the model endpoint.

## Upgrades and rollback

Tag every build (`aelix-mattermost:0.2.0`, `0.2.1`, ...) and keep the previous image;
`AELIX_MATTERMOST_IMAGE` selects the tag in `compose.yaml`. [CHANGELOG.md](../CHANGELOG.md)
lists what each release changes, state migrations included. Rebuild with
`--build-arg AELIX_VERSION=...` for a new Aelix, then run `doctor --check-aelix` and the smoke
test before switching. Back up the volume while the service is stopped. The archive holds
every transcript, `gateway.db` and Aelix's `auth.json`, so it is written with `umask 077`
(mode 0600) into a root-only directory outside the repository:

```bash
docker compose stop
sudo install -d -m 0700 /var/backups/aelix-mattermost
docker run --rm --user 0:0 --entrypoint sh -v aelix-mattermost_state:/data:ro \
  -v /var/backups/aelix-mattermost:/backup aelix-mattermost:0.2.0 \
  -c 'umask 077 && tar -czf /backup/state-$(date +%Y%m%d).tgz -C /data .'
AELIX_MATTERMOST_IMAGE=aelix-mattermost:0.2.1 docker compose up -d --no-build
```

On Docker Desktop, use a mode 0700 directory under your home instead of `/var/backups`.
To roll back, stop, restore the archive if the newer release changed state, then start the
previous tag. Run as root, `tar` restores the original owners and modes:

```bash
docker run --rm --user 0:0 --entrypoint sh -v aelix-mattermost_state:/data \
  -v /var/backups/aelix-mattermost:/backup:ro aelix-mattermost:0.2.0 \
  -c 'umask 077 && tar -xzf /backup/state-YYYYMMDD.tgz -C /data'
```

One volume serves one gateway; its state lock refuses a second.

## Health, logs and stop

`aelix-mattermost healthcheck` (the image `HEALTHCHECK`) reads `state/health.json`, which the
gateway rewrites every 10 seconds, and reports healthy only while the file is fresh
(`--max-age`, default 60 seconds) and the WebSocket is connected; it makes no network request.
`docker compose logs` shows the gateway's log (rotated, 5 x 10 MB). `docker compose stop`
sends SIGTERM through tini; the gateway aborts running requests, edits their placeholders to a
shutdown notice and stops its children within `stop_grace_period` (30 s), then Docker kills
what is left.

## Security model

**What the container protects**

- **The host.** The gateway, Aelix and any allowed tool run as uid 10001 with no capabilities
  and `no-new-privileges` on a read-only root filesystem. They see only the state volume, the
  read-only mounts, `/tmp` and the network.
- **Child cleanup.** tini forwards SIGTERM and reaps orphaned processes. The gateway first asks
  Aelix to abort a run, which stops its tool process trees, then kills the child's process
  group. A tool process that escapes both survives only until the container stops: when the
  gateway exits or the container stops, the kernel kills every process left in the container's
  PID namespace.
- **Resource ceilings.** `mem_limit`, `pids_limit` and `cpus` bound the whole bot, runaway
  tools included. Each live Aelix child needs about 85 MiB.
- **Consistent Aelix state.** `HOME` and `AELIX_CODING_AGENT_DIR` point into the volume, so
  Aelix can always create `auth.json` and transcripts. `PYTHONNOUSERSITE=1` stops a writable
  `HOME` from injecting Python code into later processes.
- **Secrets outside images.** The build context is an allowlist, nothing secret is copied
  into a layer and the token arrives as a file secret.

**What it does NOT protect**

- **Conversations from each other.** All conversations share one container, one uid and one
  volume. An allowed shell, read or write tool in one conversation can read other
  conversations' work directories and transcripts (`state/sessions/`) and change files that
  later children read (Aelix's agent directory and `HOME`).
- **Credentials from tools.** Tools run as the gateway's uid: they can read
  `/run/secrets/mattermost_token`, the provider keys in the environment and the gateway's
  `/proc` entries. Enabling a shell or file tool hands it the bot token and model keys.
- **The network.** Egress is unrestricted by default; restrict it as shown below.
- **The host from Docker itself.** Access to the Docker daemon is root on the host. Prefer
  rootless Docker or Podman, limit who may run `docker compose`, and never mount
  `/var/run/docker.sock` into this or any agent container.
- **Kernel exploits.** Containers share the host kernel. For a stronger boundary consider a
  sandboxed runtime such as gVisor (`runtime: runsc`) or a virtual machine.

### Restricting egress

Put the bot on an internal network and let an allowlisting forward proxy reach the model
endpoint, e.g. in `compose.override.yaml`:

```yaml
services:
  aelix-mattermost:
    networks: [sandbox]
  egress-proxy:
    image: registry.example.internal/squid:6   # any allowlisting forward proxy
    volumes:
      - ./squid.conf:/etc/squid/squid.conf:ro  # allow only the model endpoint
    networks: [sandbox, outside]
networks:
  sandbox:
    internal: true                             # no route off the host
  outside: {}
```

Set `HTTPS_PROXY=http://egress-proxy:3128` (and `HTTP_PROXY` for a plain-HTTP endpoint) in
`provider.env`: Aelix's model client honours these and `NO_PROXY`. The gateway's Mattermost
connection does not use a proxy, so Mattermost must be reachable on the internal network:
run it in the same project, or add a TCP pass-through (nginx `stream`, HAProxy) on both
networks and pin the Mattermost hostname to it with `extra_hosts`, which keeps TLS
verification of the real certificate. On Linux hosts the `DOCKER-USER` iptables chain is an
alternative.

## Tier 2: per-session sandboxes (future work)

Not implemented. The next step is to run each conversation's tools, or its whole Aelix child,
in a short-lived container with its own volume, uid, network policy and limits, created
through a narrowly scoped broker (for example rootless Podman, or Kubernetes Jobs with gVisor
or Kata) and never by giving the gateway the Docker socket. Until then keep shell, read and
write tools disabled, or treat all conversations as one trust domain.

## Smoke test

```bash
python deploy/docker/smoke_test.py --build --python .venv/bin/python
```

The script starts `tests/mm_fixture.py` (needs aiohttp, hence `--python`) and a mock model on
the host, runs `--version`, `check-config` and `doctor --check-aelix`, then the gateway, all
with the `compose.yaml` hardening. It waits for a DM reply and a healthy status, checks uid
10001, the read-only root, that the token never reaches the logs and a clean stop, and
removes everything it created. On Linux the fixture binds to the `docker0` gateway address
that `host.docker.internal` resolves to; override it with `--bind`.
