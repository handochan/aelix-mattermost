# aelix-mattermost

A Mattermost Team Edition bot gateway for [Aelix](https://github.com/handochan/aelix-ai).
Talk to your own Aelix runtime in DMs, group DMs, public/private channels and threads.
It uses ordinary Bot Accounts, the v4 REST API and authenticated WebSocket events;
Mattermost's commercial Agents plugin is not required.

**0.1.0 alpha:** the local REST/WebSocket/subprocess integration is tested. Deployment
against your Mattermost server and installed Aelix still requires `doctor` and a first-message
smoke test. No real credentials ship here. [한국어 안내](README.ko.md).

## Features

- DM replies without a mention; group/channel replies require `@aelix` by default.
- Threaded replies, bounded long-message splitting and in-place execution status.
- Durable Aelix session mappings, per-user/thread isolation, optional shared-thread context.
- Session-local serial execution, global concurrency limits and bounded pending requests.
- `!help`, `!cancel`, `!reset`, idle child cleanup and graceful shutdown.
- WebSocket authentication/heartbeat/reconnect and durable post-ID deduplication.
- Private state directories, one-writer locking, tokens removed from child environments.
- Tools disabled by default; opt-in exact-name policy and pre-execution per-turn call budget.
- An installable Aelix extension manifest with a `/mattermost` help command.

The gateway is a **separate Python service**. Loading the optional extension only registers
the help command; reload never silently starts a bot. This is not a Mattermost server-plugin
`.tar.gz` and must not be uploaded through Mattermost's Plugin Management screen.

## Quick start

Requirements: Python 3.11+, a running Mattermost instance, a Member-role bot token and
a working Aelix CLI in the service account's environment. This implementation targets
the current Aelix JSONL RPC protocol and extension API level 1, checked against source on
2026-10-07. Run `doctor --check-aelix` to check your installed version.

```bash
git clone https://github.com/handochan/aelix-mattermost.git
cd aelix-mattermost
python -m venv .venv
. .venv/bin/activate
python -m pip install .
cp config.example.toml config.toml
```

Edit the server URL and `allowed_users` (User IDs, not usernames). Supply the bot token
with your service secret mechanism. For an interactive POSIX test:

```bash
read -r -s -p 'Mattermost bot token: ' MATTERMOST_TOKEN
export MATTERMOST_TOKEN
aelix-mattermost check-config --config config.toml
aelix-mattermost doctor --config config.toml --check-aelix
aelix-mattermost run --config config.toml
```

`check-config` makes no network connection. `doctor` checks the REST bot identity;
`--check-aelix` also boots RPC and verifies session/policy readiness without a model prompt.
Create the bot in **Integrations → Bot Accounts** after an administrator enables bot creation.
Use role **Member**, invite it to the relevant team/channels, and include it as a participant
when creating a group DM. A System Admin token is unnecessary.

Send `hello` in a DM or `@aelix hello` in a shared channel/group. Follow up **in the same
thread**. Commands there are `@aelix !help`, `@aelix !cancel` and `@aelix !reset`;
in DMs the mention is unnecessary. These are message commands, not a registered `/aelix`
slash command. This release supports text; attachments and token-by-token streaming are deferred.

## Aelix extension installation

Build the wheel, then install it in the Aelix environment if you want the `/mattermost` helper:

```bash
python -m pip wheel . --no-deps -w dist
aelix extension install ./dist/aelix_mattermost-0.1.0-py3-none-any.whl --yes
aelix extension verify aelix-mattermost
```

The manifest is explicitly bundled as package data. Use the built wheel for deployment;
editable installations have different Aelix manifest provenance rules.

## Tools and backend permissions

`allowed_tools = []` passes `--no-tools`. Children disable ambient extensions, skills,
context-file discovery and delegation. Tools load only explicitly configured extension files.

When tools are enabled, a mandatory policy extension checks every exact tool name, including
MCP names, and blocks executions beyond `max_tool_calls` per turn. Its nonce-bound ready
acknowledgement must arrive before the first prompt. Missing/broken policy setup fails closed.

**The allowlist is not a sandbox or backend authorization.** A tool called `query` can still
write data or read unrelated resources if its implementation permits it. Domain tools should
read `aelix_mattermost.context.request_context()` and authorize `user_id` against the backend
before execution. The gateway updates this context immediately before each serialized turn,
including shared-thread turns. Do not let the model select its caller identity. Use a dedicated
OS account with narrow filesystem/network access.

## Session and delivery semantics

| Context | Session boundary |
| --- | --- |
| DM | Server + bot + DM channel |
| Default shared channel/group | Server + bot + channel + thread root + caller |
| Explicit `session_scope = "thread"` | Server + bot + channel + thread root |

`sessionFile` must stay inside the assigned conversation directory. SQLite stores mappings,
post IDs and statuses, not message text. Aelix records its own transcripts. Idle RPC children
stop after `idle_timeout`; later requests resume the mapped file. `!reset` starts new model
context while retaining old transcript files. Only a run's caller can cancel that run.

Duplicate WebSocket posts are skipped for `dedup_days`, including across restarts. Accepted
posts interrupted by a crash are **not replayed automatically**, because that could repeat
tool side effects. Disconnects can miss posts; this release has no historical backfill.
REST writes are not retried after uncertain failures. This is not an exactly-once guarantee.

## Deploy and test

See [deployment](docs/deployment.md), [security/limitations](SECURITY.md) and the
[systemd example](deploy/aelix-mattermost.service). Configure model/provider credentials in
the dedicated service account's Aelix environment. `offline = true` disables Aelix's own
download/update/catalog traffic; the configured model still needs a reachable endpoint.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w dist
```

Tests use a local REST/WebSocket fixture and a real subprocess implementing the documented
RPC contract. They cover routing, isolation, durable resume, dedup/reconnect, cancellation,
timeout/child death, malformed output and policy gating. They do not establish live-server
compatibility, model quality, OS sandboxing or Windows containment.

References: [Bot Accounts](https://docs.mattermost.com/developers/integrate/reference/bot-accounts),
[Hermes adapter](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/mattermost/adapter.py),
[Aelix RPC](https://github.com/handochan/aelix-ai/tree/main/packages/aelix-coding-agent/src/aelix_coding_agent/rpc),
[extension packaging](https://github.com/handochan/aelix-ai/blob/main/docs/guides/extension-authoring.md).

Apache-2.0. An independent integration; no Hermes or Mattermost source is bundled.
