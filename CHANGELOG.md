# Changelog

All notable changes to aelix-mattermost are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-08

### Security

- `@channel`, `@all`, `@here`, `@username` and group mentions in model output get an invisible
  U+2060 after the `@` outside code, so the server does not read them as mentions. 0.1.0 set
  `props.disable_mentions`, which the server does not use. The port of the server's parser is
  tested, not proven, and members' own notification keywords and first-name mentions still
  match plain words: see [SECURITY.md](SECURITY.md).
- Every bot post and edit sets `props.unsafe_links`, so the server fetches no external link
  previews or images for model output.
- Posts that set `from_webhook`, `from_oauth_app` or `from_plugin` are ignored. Incoming webhooks
  and custom slash-command responses carry a person's user ID and passed the allowlist in 0.1.0.
- Aelix children start MCP servers only from `aelix.mcp_config`: the agent directory's
  `mcp.json`, a work directory's `.aelix/mcp.json` and an inherited `AELIX_MCP_CONFIG` no longer
  apply. Children run with `--no-approve`, so `.aelix` resources that a tool writes into a work
  directory never load, and they no longer inherit `AELIX_SUBAGENT_DEPTH`.

### Fixed

- The WebSocket authenticates with the `Authorization` header alone and waits for the server's
  `hello`. 0.1.0 also sent an `authentication_challenge` and waited for a reply that the server
  never sends once the header has authenticated the connection, so it kept reconnecting without
  handling posts.
- Mention detection follows the server's rules: `@aelix.` and `@aelix_` now count, mentions in
  code spans and code blocks no longer do.
- A failed run no longer ends a prompt: the gateway follows automatic retries and
  context-overflow recovery until a run answers or Aelix goes idle (0.1.0 gave up at the first
  `agent_end` and stopped the child). An answer is posted as soon as its run ends, and a
  follow-up waits for a running compaction instead of failing.
- Non-JSON lines on Aelix's stdout are skipped with one warning, and RPC lines up to 64 MiB are
  read. In 0.1.0 a stray line or a line over 1 MiB failed the request.
- Slow event handling no longer stalls the WebSocket reader: events pass through a bounded queue
  (1000 posts; more are dropped with a warning), and a failing event no longer stops the gateway.
- Long answers are split without breaking fenced code blocks.
- A stored transcript path outside its session directory, e.g. after `state_dir` moved, is
  resumed by file name or dropped for a fresh conversation. 0.1.0 failed that conversation's
  requests until `!reset`.
- Cancel, timeout and shutdown of a running prompt ask Aelix to abort it first, which also stops
  its tool processes, before the child's process group is signalled (SIGKILL after 5 s, was 2 s).
- A cold Aelix start and the tool-policy acknowledgement get `startup_timeout` (default 60 s)
  instead of `rpc_timeout` (default 20 s).
- Post IDs older than `dedup_days` are also forgotten while the gateway runs, not only at start.

### Added

- `gateway.max_live_processes` (default 8, at least `max_concurrent_runs`) caps live Aelix
  children: the least recently used idle child stops before another starts.
- `run` rewrites `<state_dir>/health.json` every 10 s. The new `healthcheck` command exits 0 only
  while that file is fresh (`--max-age`, default 60 s) and the WebSocket is connected; it reads
  no token and makes no network request.
- Restart and shutdown notices: placeholder post IDs are stored, a graceful stop edits the
  placeholders of running requests to a shutdown notice, and the next start edits those a crash
  left behind to a restart notice or to the request's outcome.
- REST retries of network errors, HTTP 5xx and 429 (honouring `Retry-After`), up to three
  attempts; new posts carry a `pending_post_id` that the server deduplicates.
- WebSocket session resume (`connection_id`, `sequence_number`), so the server can replay events
  missed while reconnecting.
- `doctor` checks that the WebSocket authenticates (the server sends `hello`) and, like `run`,
  warns when the bot has a System Console role. `doctor --check-aelix` prints Aelix's agent
  directory and the resolved model.
- Settings `mattermost.token_file` (e.g. a Docker or Kubernetes secret; takes precedence over
  `token_env`), `aelix.mcp_config`, `gateway.startup_timeout` and `gateway.log_aelix_stderr`
  (default off: log a redacted tail of Aelix's stderr when a request fails).
- An optional hardened single-container [Docker deployment](docs/docker.md): a `Dockerfile`
  (uid 10001, `tini` as PID 1, `HEALTHCHECK`), a Compose file with a read-only root filesystem,
  no capabilities, resource limits and the token as a file secret, and a smoke test.

### Changed

- Answers and the failure, timeout and cancellation notices are new thread posts, which notify
  like any reply; the "preparing" placeholder is then deleted or, if deletion fails, edited to
  point at the answer (or to show the notice). 0.1.0 edited the placeholder, and edits do not
  notify.
- `doctor --check-aelix` fails when Aelix resolves no model: none is set, the provider is
  unknown, or neither `models.json` nor Aelix's catalog defines the id.
- The systemd unit uses `KillMode=mixed` and `TimeoutStopSec=30`, so the gateway can post
  shutdown notices and stop its children before systemd kills what is left. It sets `HOME` and
  `AELIX_CODING_AGENT_DIR` under `/var/lib/aelix-mattermost` and `PYTHONNOUSERSITE=1`.
- Request failures are logged with a short post ID, a short session key and the gateway's own
  bounded reason, never provider text.
- Vertical tabs and form feeds in bot output become spaces, also in code.
- The state database moves to schema version 1 (a `posts.placeholder` column) on first start; a
  database written by a newer release is refused.

### Upgrading from 0.1.0

- **State database.** With the service stopped, back up the state directory. The first start
  of 0.2.0 upgrades `gateway.db` to schema version 1. 0.1.0 still opens the upgraded database
  but exits with an SQLite error on the first post it accepts, so roll back only by restoring
  the backup.
- **Configuration.** `max_live_processes` (default 8) must be at least `max_concurrent_runs`:
  a configuration with `max_concurrent_runs` above 8 must also set
  `gateway.max_live_processes`. Empty path strings are now rejected. The new keys `token_file`,
  `mcp_config`, `max_live_processes`, `startup_timeout` and `log_aelix_stderr` are optional;
  0.1.0 refuses them as unknown settings, so remove them before rolling back.
- **Delivery.** Answers are new thread posts and the placeholder is deleted: anything that read
  the answer from the edited placeholder must read the thread instead.
- **systemd.** Install the new unit and run `systemctl daemon-reload`. Besides `KillMode=mixed`
  it sets a writable `HOME=/var/lib/aelix-mattermost/home` and
  `AELIX_CODING_AGENT_DIR=/var/lib/aelix-mattermost/aelix-agent`: move `models.json`,
  `auth.json` and any `settings.json` from the old agent directory (by default `~/.aelix/agent`
  of the service account) there, owned by `aelix-mattermost` (see
  [docs/deployment.md](docs/deployment.md)).
- **MCP.** Ambient MCP servers are off: set `aelix.mcp_config` to the MCP configuration the
  children should load.
- **Project trust.** Children run with `--no-approve`, so `.aelix` resources in their work
  directories no longer load. `doctor --check-aelix` starts Aelix with the same flags.
- **Proxies and doctor.** 0.2.0 authenticates the WebSocket only with the `Authorization`
  header, so every reverse proxy must pass that header on WebSocket upgrades. `doctor` now fails
  when the WebSocket does not authenticate and, with `--check-aelix`, when Aelix resolves no
  model.
- **Output.** Vertical tabs and form feeds in bot output become spaces.

## [0.1.0] - 2026-10-07

- Initial alpha: a Mattermost Team Edition bot gateway for Aelix RPC conversations in DMs, group
  DMs, channels and threads, with per-user/thread sessions, an exact-name tool policy, a systemd
  example and a `/mattermost` Aelix extension command.
