# Conversation features

What people can do in Mattermost, and what Aelix is told about it. Settings live in
[config.example.toml](../config.example.toml).

## Commands

Commands are messages. Mattermost clients treat a leading `/` as one of their own slash
commands, so type `!status`, ` /status` (with a leading space) or `@aelix /status`; in
channels the bot must be mentioned as usual. Unknown names are not commands: `/etc/hosts?`
is a question. The optional real [`/aelix` slash command](slash-command.md) runs the same
commands and answers only to you.

| Command | What it does |
| --- | --- |
| `!help` | Usage, including the admin commands for admins |
| `!status` | Running request (phase, elapsed time), queued requests, model, tools, context use |
| `!stop` (`!cancel`) | Stop your running request in this conversation and drop your queued ones; the Aelix child keeps the conversation |
| `!new` (`!reset`) | Start a fresh model context (old transcripts are kept on disk) |
| `!steer <text>` | Inject text into your running request |
| `!queue <text>` | Run text as a new request after the running one |
| `!model [n or name]` | Show or choose a model from `aelix.models` (`!model default` resets) |
| `!usage` | Tokens, cost and context use of this conversation |
| `!compact` | Summarise older context now (Aelix keeps recent turns; short conversations have nothing to compact) |
| `!tools` | Tools the model may use here |
| `!pair ...` | Admins, in a DM: list, approve, deny or revoke pairing (see below; never through `/aelix`) |

## While a request runs

A conversation runs one request at a time. When **the person whose request is running** sends
another message, `gateway.busy_mode` decides:

- `steer` (default): the message is injected into the running request through Aelix's steer
  queue. Aelix takes it in after the current turn (after the current answer or the current tool
  calls), and the bot reacts with 👀. If Aelix had already finished an answer, that answer is
  posted for the earlier message and the new message gets its own answer in its own thread;
  otherwise one answer covers both. A message that arrives just as the run ends is run as a
  request of its own, never lost.
- `interrupt`: the running request is stopped (its thread says so) and the new message runs
  next. The Aelix child stays alive, so the conversation keeps everything so far.
- `queue`: the message waits for its turn (up to `max_queue_per_session`).

`!steer` and `!queue` choose per message. Other people's messages in a shared thread
(`session_scope = "thread"`) always queue: only the request's owner can steer, interrupt or
stop it. A stop that arrives before the prompt reached Aelix keeps it from being sent at all,
and answers Aelix finished before a stop, failure or timeout are still posted.

Posts are handled in arrival order within a conversation, and conversations do not wait for
each other: a slow `!compact` or an attachment download holds up only its own conversation.

## Progress

The "preparing" post is edited every `progress_interval` seconds (default 3) to show the
phase (thinking, writing, running a tool and its name, retrying, compacting), the number of
tool calls and the elapsed time, and the typing indicator runs. `progress = "stream"` also
shows the end of the answer written so far; `"off"` keeps the post static. Edits never
notify; the finished answer is posted as new replies and the preparing post is removed.

## Thread history

When the bot is asked inside a channel thread, the thread's posts that the conversation has
not seen are quoted before the message, newest `thread_history_posts` (default 30) within
`thread_history_chars`. A new conversation sees what was written before the question; later
turns see what was written since, including posts made while the bot was working. Only posts
by people allowed to use the bot (and the bot's own answers to others) are quoted: posts by
other members, incoming webhooks, integrations and other bots never reach the model, so they
cannot steer a model that has tools. The conversation's own requests and the bot's posts for
it are left out (also after `!new`), as are system messages and the gateway's notices. The
quote is marked as untrusted context. DMs need no history: every DM message already reaches
the bot. The whole thread is fetched for each request, which costs more in very long threads.

## Attachments

Files on a message (up to `max_attachments`, each up to `max_attachment_bytes`) are
downloaded and described to the model:

- UTF-8 text files (by type or extension) are inlined, up to `max_inline_text_chars` per message.
- PNG, JPEG, GIF and WebP images are passed as images when the model's `input` includes
  `"image"` (5 MB each, 20 MB per message); other models are told an image arrived that they
  cannot view. `doctor --check-aelix` says which applies.
- When the conversation has tools, every file is also saved under `attachments/<post id>/` in
  its workspace for tools such as `read` or `bash` to open (200 MB per conversation; the oldest
  posts' files go first). Without tools nothing is written to disk.

A message with files and no text is fine. Files that are too large or too many are skipped
and the model is told so. `!new` removes the saved files and any unsent outbox files.

**Sending files back.** When tools that write files are allowed, files the model writes into
`outbox/` in its working directory during a request are uploaded and attached to its answer
(up to `max_upload_bytes` each, ten per post), then removed. Symbolic links and special files
are never sent.

## The system prompt

Every Aelix child gets `--append-system-prompt-file` with, in this order:

1. The built-in Mattermost part: who the bot is, whether this is a DM or a channel thread
   (and the channel's name), how messages reach it, Mattermost Markdown and splitting, that
   mentions never notify, steering, the thread-history and attachment blocks, the allowed
   tools (or that there are none) and the outbox, and the commands people can type.
2. `aelix.system_prompt`, under "Operator instructions".
3. The channel's `prompt`, under "Instructions for this channel".

Aelix's own base prompt (working directory, date, tool descriptions) comes first. The file is
rewritten whenever a child starts, so configuration changes apply after a restart.

## Per-channel settings

```toml
[channels."4xp9fdt5ejfabp3ki6b1rxyd8w"]
prompt = "This channel is about the payment service. Answer briefly."
require_mention = false            # answer every message here
allowed_tools = ["read", "grep"]    # replaces aelix.allowed_tools here
```

The key is the channel ID. `require_mention = false` makes a free-response channel: every
message from an allowed user starts or continues a conversation (top-level posts start a
thread each). A channel's `allowed_tools` replaces the global list there, also to give fewer
tools. `allowed_channels`, when set, still decides where the bot answers at all.

## Models

`aelix.models` lists models people may choose with `!model`; `aelix.model` stays the
default. The choice is stored per conversation; the Aelix child restarts with `--model` on
the next request and resumes the same transcript.

## Pairing

With `pairing = true`, a person who is not allowed and DMs the bot gets an 8-character code
(valid for an hour, at most one reply per 10 minutes, at most 50 pending codes). Admins
(`mattermost.admins`) get a DM about it and approve in their DM with the bot:

```
!pair                     list pending codes and paired users
!pair approve ABCD-EFGH   allow that person
!pair deny ABCD-EFGH
!pair revoke @alice       remove a pairing approval
```

or on the host: `aelix-mattermost pairing list|approve|deny|revoke --config config.toml ...`
(safe while the gateway runs). A denied person gets no new code, and admins no new notice,
for 24 hours. Approved people are stored in the state database and are
allowed like `allowed_users`; people in channels never get codes. Admins are always allowed,
so `admins` alone or `pairing = true` alone is a valid configuration.
