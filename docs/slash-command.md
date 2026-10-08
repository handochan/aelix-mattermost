# The `/aelix` slash command (optional)

Message commands (`!status`, ` /status`, `@aelix /status`) always work. A registered custom
slash command adds autocompletion and answers only to the person who typed it
("ephemeral"). It runs the control commands: `help`, `status`, `stop`, `new`, `model`,
`usage`, `compact` and `tools`. Questions, `steer` and `queue` stay messages, because a slash
command's text is not posted to the channel, and so does `!pair`: whoever holds the command
token can name any user ID, so admin actions never go through it.

## How it works

Mattermost POSTs a form to the command's Request URL with the command token, the caller's
user ID, the channel ID and, when typed in a thread's reply box, the thread's `root_id`.
The gateway checks the token (constant-time), then applies the same rules as messages:
allowed users (or paired users and admins), `allowed_channels`, and the session of that DM
or thread. In a channel, conversations are per thread, so `new`, `status`, `model`, `usage`
and `compact` must be typed in the thread's reply box; `stop` outside a thread stops all of
your running requests in that channel. Answers that take longer than 20 seconds follow
through the `response_url`, whose hook ID the gateway sends to `mattermost.url` (never to the
host in the URL).

## Setup

1. **Configuration.**

   ```toml
   [slash_command]
   listen = "127.0.0.1:8066"     # Docker: "0.0.0.0:8066" and publish the port
   token_file = "/run/secrets/mattermost_slash_token"   # or token_env
   trigger = "aelix"
   ```

2. **Make it reachable from Mattermost.** The Mattermost server (not the user's browser)
   calls the endpoint. When both run on one host, `http://127.0.0.1:8066/` works; from a
   Mattermost container use `http://host.docker.internal:8066/` (Docker Desktop, OrbStack) or
   a shared Docker network. Mattermost refuses requests to private addresses unless they are
   listed in **System Console → Environment → Developer → Allow untrusted internal
   connections to** (`ServiceSettings.AllowedUntrustedInternalConnections`): add the host
   name you use, e.g. `host.docker.internal` or `127.0.0.1`. Do not expose the port beyond what
   Mattermost needs; the token is the only credential.

3. **Register the command** (a team admin, per team): **Integrations → Slash Commands → Add
   Slash Command**:

   | Field | Value |
   | --- | --- |
   | Title | Aelix |
   | Command Trigger Word | `aelix` (the same as `trigger`) |
   | Request URL | e.g. `http://host.docker.internal:8066/` (any path works) |
   | Request Method | POST |
   | Autocomplete | on; hint `[status|stop|new|model|usage|compact|tools|help]` |

   Save, copy the **Token** into the token file (`umask 077; printf '%s' TOKEN > ...`), and
   restart the gateway. `doctor` prints the configured endpoint.

4. **Try it**: `/aelix help` in a DM with the bot, then `/aelix status`.

Slash commands are registered per team; DMs work from any team the command exists in.
Mattermost only sends `root_id` in recent versions; an older server treats every invocation
as made outside a thread.
