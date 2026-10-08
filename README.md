# aelix-mattermost

A Mattermost Team Edition bot gateway for [Aelix](https://github.com/handochan/aelix-ai).
Talk to your own Aelix runtime in DMs, group DMs, public/private channels and threads.
It uses ordinary Bot Accounts, the v4 REST API and a header-authenticated WebSocket;
Mattermost's commercial Agents plugin is not required.

**0.2.0 alpha:** the REST/WebSocket/subprocess integration is tested against local doubles
that follow Mattermost server and Aelix RPC behaviour. Deployment against your Mattermost
server and installed Aelix still requires `doctor` and a first-message smoke test. No real
credentials ship here. [한국어 안내](README.ko.md).

## Features

- DM replies without a mention; group/channel replies require `@aelix` by default, detected
  with Mattermost's own mention rules (mentions inside code never count).
- Threaded answers posted as new replies, so they notify; the "preparing" placeholder is
  removed. Long answers are split without breaking code fences.
- Durable Aelix session mappings, per-user/thread isolation, optional shared-thread context.
- Session-local serial execution, global concurrency limits, bounded pending requests and a
  cap on live Aelix processes.
- `!help`, `!cancel`, `!reset`, idle child cleanup, graceful shutdown and notices for requests
  interrupted by a restart.
- WebSocket heartbeat and session resume, REST retries and durable post-ID deduplication.
- Output safety: no server-side link previews; `@channel`, `@here`, `@all` and `@username`
  in model output notify nobody (members' own notification keywords still can, see below).
- Webhook, OAuth-app and plugin posts are ignored even when they carry an allowed user's ID.
- Private state directories, one-writer locking, a `health.json` file with a `healthcheck`
  command, and a bot token read from a variable or file and removed from child environments.
- Tools disabled by default; opt-in exact-name policy and a pre-execution per-message call budget.
- An installable Aelix extension manifest with a `/mattermost` help command.
- An optional hardened single-container [Docker deployment](docs/docker.md).

The gateway is a **separate Python service**. Loading the optional extension only registers
the help command; reload never silently starts a bot. This is not a Mattermost server-plugin
`.tar.gz` and must not be uploaded through Mattermost's Plugin Management screen.

## Quick start

Requirements: Python 3.11+, a running Mattermost instance, a Member-role bot token and
a working Aelix CLI in the service account's environment. This implementation targets the
JSONL RPC protocol and extension API level 1 of Aelix 0.1.0b2, checked on 2026-10-08.
Run `doctor --check-aelix` to check your installed version.

```bash
git clone https://github.com/handochan/aelix-mattermost.git
cd aelix-mattermost
python -m venv .venv
. .venv/bin/activate
python -m pip install .
cp config.example.toml config.toml
```

Edit the server URL and `allowed_users` (User IDs, not usernames). Supply the bot token
through the variable named by `token_env` (default `MATTERMOST_TOKEN`) or through
`token_file`, e.g. a Docker or Kubernetes secret, which takes precedence. For an interactive
POSIX test:

```bash
read -r -s -p 'Mattermost bot token: ' MATTERMOST_TOKEN
export MATTERMOST_TOKEN
aelix-mattermost check-config --config config.toml
aelix-mattermost doctor --config config.toml --check-aelix
aelix-mattermost run --config config.toml
```

`check-config` makes no network connection. `doctor` checks the REST bot identity and that
the WebSocket authenticates (the server sends `hello`). `--check-aelix` also boots RPC like
the gateway, requires a model Aelix resolves (a known provider, and an id that `models.json`
or Aelix's catalog defines), prints Aelix's agent directory and the model, and verifies
session/policy readiness without a model prompt. It cannot test provider credentials or the
model endpoint, because that needs a model request: the first message does.
Create the bot in **Integrations → Bot Accounts** after an administrator enables bot creation.
Use role **Member**, invite it to the relevant team/channels, and include it as a participant
when creating a group DM. A System Admin token is unnecessary; `doctor` and `run` warn when the
bot has a System Console role.

Send `hello` in a DM or `@aelix hello` in a shared channel/group. Follow up **in the same
thread**. Commands there are `@aelix !help`, `@aelix !cancel` and `@aelix !reset`;
in DMs the mention is unnecessary. These are message commands, not a registered `/aelix`
slash command. This release supports text; attachments and token-by-token streaming are deferred.

Mentions follow the server's rules: `@aelix.` and `@aelix:` count, while `@aelix-bot`,
`email@aelix`, `@aelix님` and anything in code spans or code blocks do not. Mentions are
removed from the prompt.

## Aelix extension installation

Build the wheel, then install it in the Aelix environment if you want the `/mattermost` helper:

```bash
python -m pip wheel . --no-deps -w dist
aelix extension install ./dist/aelix_mattermost-0.2.0-py3-none-any.whl --yes
aelix extension verify aelix-mattermost
```

Aelix installs the wheel into its own Python environment, and the installer fetches `aiohttp`
from your package index. In a closed network, first install `aiohttp` into that environment
from a wheelhouse. Aelix pins the wheel's SHA-256 on first install: reinstalling a rebuilt
wheel from the same path needs `--repin`. The manifest is bundled as package data. Use the
built wheel for deployment; editable installations have different Aelix manifest provenance rules.

## Tools and backend permissions

Every child runs `aelix --mode rpc` with `--no-extensions --no-skills --no-context-files
--no-agents --no-approve`, without an inherited `AELIX_SUBAGENT_DEPTH`, and with
`AELIX_MCP_CONFIG` pointing at an empty file unless `aelix.mcp_config` names one. The
service account's own `mcp.json` and ambient extensions never load, and `--no-approve` keeps
`.aelix` resources that a tool writes into a work directory from loading.
`allowed_tools = []` also passes `--no-tools`.

With tools enabled, Aelix still registers its built-in tools (`bash`, `edit`, `find`, `grep`,
`ls`, `read`, `write`, `aelix_status`) and the tools of MCP servers from `aelix.mcp_config`;
only names in `allowed_tools` become active. Naming `bash`, `read` or `write` gives the model a
shell or file access as the service account. RPC mode has no approval dialog, so Aelix allows
active tool calls on its own; the gateway's policy extension is the only allowlist. It checks
every exact tool name, including MCP names, and blocks calls beyond `max_tool_calls` per
Mattermost message (one prompt, shared across Aelix's automatic retries). Its nonce-bound
ready acknowledgement must arrive before the first prompt. Missing/broken policy setup fails closed.

**The allowlist is not a sandbox or backend authorization.** A tool called `query` can still
write data or read unrelated resources if its implementation permits it. Domain tools should
call `aelix_mattermost.context.request_context()` and authorize its `user_id` against the
backend before execution. Extension tools run inside Aelix's Python process, so that import
works only when aelix-mattermost is installed in Aelix's environment (the same virtualenv, or
`aelix extension install` as above); otherwise read the JSON file named by
`AELIX_MATTERMOST_CONTEXT_FILE` (`server`, `post_id`, `channel_id`, `user_id`, `root_id`).
The gateway rewrites this context
immediately before each serialized run, including shared-thread runs. Do not let the model
select its caller identity.

Extension code (`setup()`, `session_start` handlers, tools) must never print to stdout, which
carries the RPC protocol: log to stderr. The gateway skips non-JSON stdout lines with a single
warning, but printed JSON can be misread as protocol.

With `session_scope = "thread"` all participants share one model context, including raw tool
results fetched for an earlier caller, and any of them can ask for those. Use it only where
every participant may see that data. Run the gateway under a dedicated OS account with narrow
filesystem/network access, or in the [Docker image](docs/docker.md).

## Session and delivery semantics

| Context | Session boundary |
| --- | --- |
| DM | Server + bot + DM channel |
| Default shared channel/group | Server + bot + channel + thread root + caller |
| Explicit `session_scope = "thread"` | Server + bot + channel + thread root |

`sessionFile` must stay inside the assigned conversation directory. SQLite stores mappings,
post IDs, statuses and placeholder post IDs, not message text. Aelix records its own
transcripts. Idle RPC children stop after `idle_timeout`. When `max_live_processes` children
are running, the gateway stops the least recently used idle one before starting another. A
child is idle when no request of its session is running and Aelix is not compacting, so a
request that only waits for a run slot keeps no child. A child that is running or compacting
is never stopped, so the cap can be exceeded briefly. Later requests resume the mapped file;
a mapping whose file is missing or outside the session directory is dropped and the
conversation starts fresh.
`!reset` starts new model context while retaining old transcript files. Only a run's caller
can cancel that run.

When an accepted prompt starts running, the bot posts `응답을 준비하고 있습니다…` in the thread.
The answer then arrives as new thread posts, which notify like any reply (edits never notify),
and the placeholder is deleted, or edited to point at the answer if deletion fails. Failure,
timeout and cancellation notices are delivered the same way. Delivered chunks are never
edited; if a later chunk fails, a short notice follows them. Aelix's automatic retries and
context-overflow recovery happen within the same request; a follow-up waits for any
compaction still running.

Placeholder IDs are stored, so a graceful shutdown edits the placeholders of running
requests to a shutdown notice, and the next start does the same with a restart notice for
anything left after a crash. A placeholder that Mattermost failed to remove during an outage
stays stored and is later edited to its request's outcome. Requests still queued behind
another have no placeholder yet and end without a notice. Interrupted requests are **not replayed automatically**, because that
could repeat tool side effects. Duplicate WebSocket posts are skipped for `dedup_days`,
including across restarts.

The `Authorization` header authenticates the WebSocket; the gateway waits for the server's
`hello`, and a rejected token stops the gateway. Reconnects resume the WebSocket session, so the
server replays up to 128 missed events if the gateway returns to the same server node within
a few minutes; otherwise it logs that events may have been missed. There is no historical
backfill. REST calls retry network errors, HTTP 5xx and 429 (honouring `Retry-After`) up to
three attempts; new posts carry a `pending_post_id` that the server deduplicates. This is not
an exactly-once guarantee.

## Output safety and integrations

Every post and edit the bot makes carries `props.unsafe_links = "true"`, so the server never
fetches link previews or markdown images from model output, which a prompt injection could
use to leak data. An edit replaces all props, so `from_bot` is sent too (it keeps the BOT
badge on servers before 11.10). Some clients may still load markdown images when an answer is
viewed: treat links and images in answers as untrusted.

`@channel`, `@all`, `@here`, `@username` and group mentions in model output get an invisible
U+2060 after the `@` outside code, so they read the same but do not notify; code, link
targets, bare URLs and reference definitions stay unchanged. Rare markdown changes that, and
this port of Mattermost's parser is tested, not proven: see [SECURITY.md](SECURITY.md).
Notification keywords that members set for themselves, and first-name mentions, match plain
words without an `@`: an answer that contains such a word still notifies that member.

Posts whose props set `from_webhook`, `from_oauth_app` or `from_plugin` to true are ignored:
incoming webhooks post with their owner's user ID and custom slash-command responses with the
caller's, so the user allowlist alone cannot tell them apart. A plugin slash command that
posts through the plugin API as the caller sets none of these and cannot be detected.

## Operations

`run` rewrites `<state_dir>/health.json` (mode 0600) every 10 seconds.
`aelix-mattermost healthcheck --config config.toml` reads only `state_dir` from the
configuration and that file (no token, no network) and exits 0 only while the file is fresh
(`--max-age`, default 60 seconds) and the WebSocket is connected; otherwise it prints the reason
and exits 1. Use it from a monitoring agent or timer running as the service account (the
state directory is private), or as a Docker `HEALTHCHECK`.

Request failures are logged with a short post ID, a short session key and the gateway's own
bounded reason, never provider text. `log_aelix_stderr = true` also logs a redacted tail of
Aelix's stderr; keep it off unless you are troubleshooting. `startup_timeout` (default 60
seconds) bounds a cold Aelix start.

## Deploy and test

See [deployment](docs/deployment.md) (systemd and closed networks), [Docker](docs/docker.md),
[security/limitations](SECURITY.md) and the [systemd example](deploy/aelix-mattermost.service).
Upgrading from 0.1.0 migrates the state database in place and changes the systemd unit:
follow the [upgrade notes](CHANGELOG.md#upgrading-from-010) and back up the state directory first.
The Docker image protects the host (uid 10001, read-only root, no capabilities, resource
limits, token as a file secret) but not one conversation from another: read its
[security model](docs/docker.md#security-model) before enabling shell or file tools.
Configure the model provider in the Aelix agent directory the service uses (`models.json`).
`offline = true` disables Aelix's own download/update/catalog traffic; the configured model
still needs a reachable endpoint.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w dist
python deploy/docker/smoke_test.py --build --python .venv/bin/python   # needs Docker
```

The unit tests use a local Mattermost fixture that follows server rules (header
authentication, `hello`, resume, pending-post deduplication, patch semantics) and a subprocess
double of the Aelix RPC protocol (retries, overflow recovery, busy rejection, abort, huge lines,
stray stdout). They cover routing and mention parsing, isolation, durable resume,
dedup/reconnect, delivery, cancellation, timeout/child death and policy gating. They do not
establish live-server compatibility, model quality, OS sandboxing or Windows containment.

References: [Bot Accounts](https://docs.mattermost.com/developers/integrate/reference/bot-accounts),
[Hermes adapter](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/mattermost/adapter.py),
[Aelix RPC](https://github.com/handochan/aelix-ai/tree/main/packages/aelix-coding-agent/src/aelix_coding_agent/rpc),
[extension packaging](https://github.com/handochan/aelix-ai/blob/main/docs/guides/extension-authoring.md).

Apache-2.0. An independent integration; no Hermes or Mattermost source is bundled.
