# Deployment

Two layouts are supported: a service account with the
[systemd unit](../deploy/aelix-mattermost.service) (below) or the hardened single-container
[Docker image](docker.md). Both run every conversation under one account; read
[SECURITY.md](../SECURITY.md) before enabling tools.

## Linux with systemd

1. Create a dedicated `aelix-mattermost` service account and the `/opt/aelix-mattermost`
   installation directory.
2. Create `/opt/aelix-mattermost/.venv` and install a verified Aelix release and this wheel into
   it, so domain tools can import `aelix_mattermost.context`; or set an absolute executable in
   `aelix.command`.
3. Configure `/etc/aelix-mattermost/config.toml`: server URL, users/channels, model, CA bundle,
   `state_dir = "/var/lib/aelix-mattermost/state"` and
   `work_dir = "/var/lib/aelix-mattermost/workspace"`.
4. Put `MATTERMOST_TOKEN=...` and any provider keys in `/etc/aelix-mattermost/secrets.env`
   (root-owned, mode 0600; systemd reads it before dropping privileges), or set `token_file`
   to a file only the service account can read. The unit requires `secrets.env` either way;
   with `token_file` it may hold only provider keys.
5. Add the provider configuration to the agent directory (below).
6. Run `check-config` and `doctor --check-aelix` with the unit's environment (below); neither
   submits a model prompt.
7. Start the unit and work through [Verify your deployment](#verify-your-deployment).
8. Enable domain tools individually after checking backend authorization and side effects.

| Path | Purpose |
| --- | --- |
| `/opt/aelix-mattermost/.venv` | The gateway, Aelix and domain-tool dependencies |
| `/etc/aelix-mattermost/config.toml` | Gateway configuration |
| `/etc/aelix-mattermost/secrets.env` | Bot token and provider keys (`EnvironmentFile`) |
| `/var/lib/aelix-mattermost/aelix-agent` | Aelix agent directory (`AELIX_CODING_AGENT_DIR`): `models.json`, optional `settings.json` and `auth.json` |
| `/var/lib/aelix-mattermost/home` | `HOME` of the gateway and Aelix |
| `/var/lib/aelix-mattermost/state` | `state_dir`: database, sessions and transcripts, `health.json` |
| `/var/lib/aelix-mattermost/workspace` | `work_dir`: one work directory per conversation |

The unit sets `HOME` and `AELIX_CODING_AGENT_DIR` under `/var/lib/aelix-mattermost`, which
`StateDirectory=` creates (owner `aelix-mattermost`, mode 0700) and `ReadWritePaths=` keeps
writable. Aelix opens `auth.json` in its agent directory read-write on every start, so an agent
directory under `/home` (the default `~/.aelix/agent`) makes every child fail under
`ProtectHome=read-only`, even when `doctor` passes in an admin shell. Create the directories and
the provider file before the first start:

```bash
sudo install -d -o aelix-mattermost -g aelix-mattermost -m 0700 /var/lib/aelix-mattermost \
  /var/lib/aelix-mattermost/home /var/lib/aelix-mattermost/aelix-agent
sudo install -o aelix-mattermost -g aelix-mattermost -m 0600 models.json \
  /var/lib/aelix-mattermost/aelix-agent/models.json
```

A `models.json` provider `apiKey` may name an environment variable from `secrets.env`; Aelix
uses that variable's value when it is set. The
[Docker example](../deploy/docker/models.json.example) shows the format. The gateway ignores the agent directory's `mcp.json`: name an MCP config
explicitly with `aelix.mcp_config`. Run `doctor` the way the unit runs, so a broken layout fails
here and not on the first message:

```bash
sudo systemd-run --pty --wait --collect --uid=aelix-mattermost --gid=aelix-mattermost \
  -p EnvironmentFile=/etc/aelix-mattermost/secrets.env \
  -p Environment=PATH=/opt/aelix-mattermost/.venv/bin:/usr/local/bin:/usr/bin:/bin \
  -p Environment=HOME=/var/lib/aelix-mattermost/home \
  -p Environment=AELIX_CODING_AGENT_DIR=/var/lib/aelix-mattermost/aelix-agent \
  -p Environment=PYTHONNOUSERSITE=1 \
  -p ProtectSystem=strict -p ProtectHome=read-only -p PrivateTmp=true \
  -p ReadWritePaths=/var/lib/aelix-mattermost \
  /opt/aelix-mattermost/.venv/bin/aelix-mattermost doctor \
  --config /etc/aelix-mattermost/config.toml --check-aelix
```

`PrivateTmp=true` matters: under `ProtectSystem=strict` the shared `/tmp` is read-only, and
`doctor --check-aelix` starts Aelix in a temporary directory.

`doctor` prints the bot identity, the WebSocket check, Aelix's agent directory and the
resolved model. If the WebSocket check fails while REST works, make sure every reverse proxy
passes WebSocket upgrades and the `Authorization` header. For monitoring, run the health check
as the service account; it needs no token, only read access to the state directory:

```bash
sudo -u aelix-mattermost /opt/aelix-mattermost/.venv/bin/aelix-mattermost healthcheck \
  --config /etc/aelix-mattermost/config.toml
```

On stop, systemd sends SIGTERM to the gateway, which aborts running requests, edits their
placeholders to a shutdown notice and stops its Aelix children; `KillMode=mixed` then kills
anything left. The unit grants a narrow writable state path; adapt approved tool access
deliberately.

## Closed networks

Build/download this wheel, a verified Aelix wheel and dependencies on an approved workstation.
Transfer the wheelhouse and install without index access:

```bash
python -m pip install --no-index --find-links ./wheelhouse aelix-mattermost
```

`aelix extension install` resolves `aiohttp` from your package index, so install `aiohttp`
into Aelix's environment from the wheelhouse first. For containers, see
[closed-network Docker builds](docker.md#closed-networks).

Keep `offline = true`. This disables Aelix's own download/update/catalog requests, while
model requests still need a reachable endpoint. Configure an internal provider and avoid
public fallback credentials when requests must stay inside the network.

## Verify your deployment

| Check | Expected result |
| --- | --- |
| Bot identity and WebSocket | `doctor` reports the expected bot ID/username and a WebSocket `hello` |
| RPC compatibility | `doctor --check-aelix` reports the resolved model and readiness |
| Unauthorized user | No prompt or bot reply |
| Shared channel without mention | No reply with default settings |
| Incoming-webhook post with an allowed user's ID and `@aelix` | Ignored: no placeholder, no reply |
| DM/group/private channel | Threaded reply when the bot is a participant/member |
| Answer delivery | The answer is a new thread reply that notifies like any reply; the placeholder disappears |
| Same-thread followup | Caller context retained |
| Different caller, default scope | Separate transcript |
| Answer containing `@channel` (ask the model to repeat it) | Shown as text; nobody is notified |
| Cancel/timeout | Child stopped; notice posted in the thread |
| Restart during a running request | The placeholder shows the shutdown or restart notice; the request is not rerun |
| Restart | Session resumes; accepted post IDs do not rerun |
| `healthcheck` | Exit 0 while running and connected; exit 1 after the service stops |
| Enabled tools | Policy acknowledgement before the first prompt |

## Windows and upgrades

Use a virtual environment, an absolute Aelix path and the service's secret mechanism.
State locking and taskkill tree cleanup are implemented; this release was tested on Linux and
macOS only. Verify shutdown/descendants on your target Windows server before production deployment.

Stop the service before replacing wheels. Do not share a state directory across live processes.
Back up private state/transcripts according to your retention policy; keep a previous wheel
for rollback. Before upgrading, read the upgrade notes in [CHANGELOG.md](../CHANGELOG.md) and,
with the service stopped, back up the state directory. The first start of 0.2.0 upgrades
`gateway.db` in place to schema version 1 (a new placeholder column), and on the upgraded
database 0.1.0 exits with an SQLite error on the first post it accepts: restore that backup
when rolling back to 0.1.0. A database written by a newer release is refused. Resetting
context retains old files.
