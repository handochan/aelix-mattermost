# Security and operating boundaries

- Use a dedicated Member-role bot, HTTPS and an internal CA bundle where needed.
- Bot membership limits event visibility; the gateway additionally checks caller/channel IDs.
- An empty user allowlist refuses startup unless an administrator explicitly enables all users,
  names admins, or enables pairing. Pairing codes are 8 characters from a 32-letter alphabet
  (40 bits), valid for an hour, at most 50 pending, and approved only by `admins` in a DM with
  the bot or by the CLI on the host; unknown users get at most one reply per 10 minutes and
  never in channels, and a denied user gets no new code for 24 hours. On a server where anyone
  can sign up, 50 accounts can keep the pending list full: keep pairing off there, or watch
  `aelix-mattermost pairing list`. Approved users are stored in the state database: back it up and review
  `aelix-mattermost pairing list` like any allowlist.
- Integration posts carry a person's user ID: an incoming webhook its owner's, a custom
  slash-command response its caller's. Posts that set `from_webhook`, `from_oauth_app` or
  `from_plugin` are ignored. A plugin slash command that posts as its caller through the plugin
  API sets none of these and cannot be told apart from that person.
- Never put credentials in Git, config examples, tests or logs. Supply the bot token through
  `token_file` or an owner-readable environment file.
- The gateway token is removed from Aelix child environments; model credentials remain available
  to Aelix. A shell or read tool running as the service account can still read a `token_file`,
  provider keys and the gateway's `/proc/<pid>/environ`. This is not a credentials sandbox.
- Explicit extension code is trusted deployment code and executes during startup. Tools require
  exact-name gating and a pre-execution call budget per Mattermost message. RPC mode has no
  approval dialog, so this policy is the only allowlist; naming a built-in (`bash`, `edit`,
  `find`, `grep`, `ls`, `read`, `write`, `aelix_status`) in `allowed_tools` activates it.
- Children disable tools by default, ambient extensions/skills/context discovery, delegation and
  project trust (`--no-approve`), and never inherit `AELIX_SUBAGENT_DEPTH`. MCP servers start only
  from `aelix.mcp_config`, and then with every child, even when tools are disabled.
- Work directories persist per conversation. `.aelix` resources a tool writes there never load,
  but Aelix still reads a `.env` there for provider credentials that are not already set.
- Separate working directories do not stop an allowed shell/read tool from accessing other paths,
  other conversations' work directories and transcripts, or the gateway state.
- Domain tools must authorize the identity from `request_context()` against backend permissions.
  Shared threads expose answers to all participants, and with `session_scope = "thread"` also the
  raw tool results of earlier callers: check the intended audience.
- `@channel`, `@all`, `@here`, `@username` and group mentions in model output are neutralized
  outside code by an invisible U+2060 word joiner after the `@`. Members' own notification
  keywords and first-name mentions match plain words without an `@` and still fire, so a
  prompt injection can still notify those members. Every bot post and edit sets
  `unsafe_links`, so the server fetches no link previews or images for it. Clients may still
  load markdown images when an answer is viewed: treat links and images in answers as
  untrusted.
- The neutralization ports Mattermost's markdown and mention rules (`mentions.py`). It was
  checked against Mattermost 11.11's own parser with generated messages, which is not a
  proof. Vertical tabs and form feeds become spaces first, also in code: where one starts a
  paragraph, Mattermost's parser cuts that line short at its end. Fenced and indented code
  blocks never change otherwise. Where the port cannot follow the server, it treats a whole
  paragraph as plain text and gives every `@` before a word a joiner, also in inline code,
  link targets, bare URLs and email addresses:
  - a bracketed label that differs from a reference definition's label only in non-ASCII
    letters with upper and lower case, such as `[é]` and `[ü]` (Unicode case folding is not
    ported): that paragraph only, apart from the reference definitions it starts with;
  - reference links that still change after eight rounds of neutralization: every paragraph
    of the post, reference definitions included.

  A reference link that shows a mention as its label, `[@here]` or `[@here][]` with a
  `[@here]: ...` definition, loses its link to the joiner. The rest of that paragraph can then
  parse differently, which can leave a joiner in a link target or inline code there.
- Logs carry short IDs and the gateway's own error summaries. `log_aelix_stderr` adds an Aelix
  stderr tail whose redaction is pattern-based; enable it only while troubleshooting.
- SQLite stores IDs/status/mappings/placeholder IDs, but Aelix records full transcripts. Set
  retention, backup and filesystem ACL policies. Resetting context retains old files.
- WebSocket resume replays only recent events from the same server node; it is not historical
  backfill. Delivery/side effects are not exactly once, and interrupted requests are not replayed.
- Time and tool-call budgets are not token-cost budgets. `!model` switches only among
  `aelix.models`, which may cost more than the default.
- Thread history quotes posts into the prompt, marked as untrusted, and attachments are
  inlined: both are prompt-injection input like the message itself. History quotes only
  people allowed to use the bot and the bot's own answers; other members, webhooks,
  integrations and other bots are left out. The bot only reads threads in channels it is a
  member of, and only when an allowed user asks there. Attachments are saved to disk only for
  conversations with tools (200 MB per conversation, removed by `!new`); delete work
  directories according to your retention policy.
- Per-channel `allowed_tools` decides what a channel's model may call; it is not an isolation
  boundary. Every conversation runs as the same account, so a shell or file tool allowed in one
  channel can read other conversations' files.
- Files the model writes into its `outbox/` are uploaded to the thread it answers in (regular
  files only; symbolic links and special files are refused). With a shell or file tool, the
  model can copy anything the service account can read into the outbox, so allowing such tools
  means the people who can talk to the bot can receive those files.
- The optional slash command endpoint trusts the Mattermost command token alone: whoever holds
  it can act as any user ID it names (`status`, `stop`, `new`, `model`, `usage`, `compact` on
  that user's conversations; never `pair`). Keep the token in a file secret, bind the endpoint
  where only Mattermost reaches it, and regenerate the token in Mattermost if it leaks.
  Follow-ups go only to `mattermost.url` (only the hook ID of `response_url` is used).
- `aelix.extensions` module names are loaded through a generated file in the state directory,
  because Aelix would otherwise resolve a bare module name against the working directory,
  which tools can write to.
- Cancel, timeout and shutdown first ask Aelix to abort the run, which stops its tool process
  trees, then terminate the child's process group. A descendant that escapes both lives until the
  service stops (systemd `KillMode=mixed`, or the container's PID namespace). Windows taskkill
  cleanup is implemented but was not tested here. This is not a Job Object sandbox.
- The [Docker image](docs/docker.md#security-model) adds a read-only root filesystem, no
  capabilities and resource limits, but all conversations still share one uid and one volume.

For this initial private project, report issues to the owner without exposing credentials,
customer data, transcripts or equipment identifiers in public issues.
