# Security and operating boundaries

- Use a dedicated Member-role bot, HTTPS and an internal CA bundle where needed.
- Bot membership limits event visibility; the gateway additionally checks caller/channel IDs.
- An empty user allowlist refuses startup unless an administrator explicitly enables all users.
- Never put credentials in Git, config examples, tests or logs.
- The gateway token is removed from Aelix child environments; model credentials remain available
  to Aelix. Allowed tools can access them. This is not a credentials sandbox.
- Tools require exact-name gating and a pre-execution call budget. Explicit extension code is
  trusted deployment code and executes during startup.
- Default children disable tools, ambient extensions/skills/context discovery and delegation.
- Separate working directories do not stop an allowed shell/read tool from accessing other paths.
- Domain tools must authorize the identity from `request_context()` against backend permissions.
  Shared threads expose answers to all participants: check the intended audience.
- SQLite stores IDs/status/mappings, but Aelix records full transcripts. Set retention, backup
  and filesystem ACL policies. Resetting context retains old files.
- WebSocket reconnect is not historical backfill. Delivery/side effects are not exactly once.
- Time and tool-call budgets are not token-cost budgets.
- POSIX cleanup terminates the owned process group; descendants creating new groups can escape.
  Windows taskkill cleanup is implemented but was not tested here. This is not a Job Object sandbox.
- Splitting long replies can cut Markdown code fences; operators may need to reformat those replies.

For this initial private project, report issues to the owner without exposing credentials,
customer data, transcripts or equipment identifiers in public issues.
