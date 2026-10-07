# Deployment

## Linux without containers

1. Create a dedicated service account and `/opt/aelix-mattermost` installation directory.
2. Install a verified Aelix release and configure the account's internal model endpoint.
3. Install this wheel in the same environment, or set an absolute executable in `aelix.command`.
4. Configure the server URL, users/channels, persistent state paths, model and CA bundle.
5. Supply the token through an owner-readable EnvironmentFile or your service secret mechanism.
6. Run `check-config` and `doctor --check-aelix`; neither submits a model prompt.
7. Verify one DM, one group mention and a same-thread followup.
8. Enable domain tools individually after checking backend authorization and side effects.

The sample systemd unit uses `/opt/aelix-mattermost/.venv/bin/aelix-mattermost`,
`/etc/aelix-mattermost/config.toml` and `/etc/aelix-mattermost/secrets.env`.
Set `state_dir = "/var/lib/aelix-mattermost"` and
`work_dir = "/var/lib/aelix-mattermost/workspace"`. The service account must be able to
execute Aelix and read its provider configuration. Restrict the EnvironmentFile permissions.
The unit provides a narrow writable state path; adapt approved tool access deliberately.

## Closed networks

Build/download this wheel, a verified Aelix wheel and dependencies on an approved workstation.
Transfer the wheelhouse and install without index access:

```bash
python -m pip install --no-index --find-links ./wheelhouse aelix-mattermost
```

Keep `offline = true`. This disables Aelix's own download/update/catalog requests, while
model requests still need a reachable endpoint. Configure an internal provider and avoid
public fallback credentials when requests must stay inside the network.

## Verify your deployment

| Check | Expected result |
| --- | --- |
| Bot identity | Doctor reports the expected bot ID/username |
| RPC compatibility | Doctor with `--check-aelix` reports readiness |
| Unauthorized user | No prompt or bot reply |
| Shared channel without mention | No reply with default settings |
| DM/group/private channel | Threaded reply when the bot is a participant/member |
| Same-thread followup | Caller context retained |
| Different caller, default scope | Separate transcript |
| Cancel/timeout | Child stopped; status updated |
| Restart | Session resumes; accepted post IDs do not rerun |
| Enabled tools | Policy acknowledgement before the first prompt |

## Windows and upgrades

Use a virtual environment, an absolute Aelix path and the service's secret mechanism.
State locking and taskkill tree cleanup are implemented; this release was tested on Linux only.
Verify shutdown/descendants on your target Windows server before production deployment.

Stop the service before replacing wheels. Do not share a state directory across live processes.
Back up private state/transcripts according to your retention policy; keep a previous wheel
for rollback. Schema version 1 has no destructive migration. Resetting context retains old files.
