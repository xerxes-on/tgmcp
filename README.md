# xerxes-tg

`xerxes-tg` is an MCP server for Telegram built on top of Telethon and MTProto.

It provides:

- Telegram read and write MCP tools for any MCP client
- chat-level read/write access control with aliasing
- scheduled sends, reply-by-reference, thread view, global search
- temporary reply watches with signed webhook delivery and polling fallback
- chat-scoped delivery into Codex, including active-turn steering
- live reply injection into an open Claude Code session through MCP channels
- Telegram Mini App WebView URL generation for browser automation
- a local SQLite + FTS5 mirror with on-demand sync for cross-chat search
- an on-disk audit log (48h retention) and local rate limiting
- encrypted session storage with an owner-only key file and legacy keychain compatibility
- a setup wizard that writes MCP client configuration for popular coding agents

## Install

Install the latest Git build (version 0.6.1) with `uv`:

```bash
uv tool install 'git+https://github.com/xerxes-on/tgmcp.git@main'
```

Or install a local checkout:

```bash
uv tool install .
```

Run the setup wizard:

```bash
xerxes-tg setup
```

The wizard currently generates a PyPI-based MCP launcher. For this Git build,
replace its generated package source with the Git source shown in the
[MCP config example](#mcp-config-example) before opening your coding client.

Or run directly from the source tree:

```bash
uv run xerxes-tg setup
```

### Upgrade

```bash
uv tool install --force --refresh-package xerxes-tg 'git+https://github.com/xerxes-on/tgmcp.git@main'
xerxes-tg watcher stop
xerxes-tg watcher start
```

Reconnect the MCP server in your coding client so it loads the installed version.
If your MCP configuration runs a source checkout, update that checkout instead;
upgrading the installed tool does not change the source checkout it uses.
For configurations using `uvx`, use the Git source above and pass
`--refresh-package xerxes-tg` when upgrading until this version is published to
PyPI. Existing credentials and event storage
are retained. Restart Claude with channels enabled and create a fresh watch there
because channel bindings belong to the MCP process that created them.

### Changes in 0.6.1

- Find chats by title, contact name, username, alias, or ID with `SearchChats`.
- Resolve numeric chat IDs even with an empty session entity cache.
- Announce temporary watches with the local stop time and auto-reply intent.
- Acknowledge watched messages with brief text replies.

### Changes in 0.6.0

- Deliver watched messages into the originating session through native queue,
  turn, and channel APIs.
- Recover missed Telegram updates for active watches through bounded history
  checks, with filtering and deduplication shared with live delivery.
- Keep Telegram text separate from session instructions; acknowledge receipt
  with 👀 when the receiving session handles the event.
- Test session routing, filtering, recovery, and package builds in CI before
  the release publishing workflow can run.

## Commands

```bash
xerxes-tg setup             # interactive configuration wizard
xerxes-tg run               # start the MCP server (what your coding agent calls)
xerxes-tg doctor            # health checks: config, session, aliases, agent configs
xerxes-tg audit [--tail N]  # view recent tool-call history (last 48h)
xerxes-tg sync --all        # pull messages into the local mirror (cross-chat search)
xerxes-tg watcher start     # start the temporary-watch daemon (detached)
xerxes-tg watcher status    # show daemon, webhook and subscription status
xerxes-tg watcher stop      # stop delivery without deleting stored state
xerxes-tg uninstall         # remove xerxes-tg entries from agent configs
xerxes-tg sign-in --api-id <id> --api-hash <hash> --phone-number <n>
xerxes-tg logout
```

### Useful flags

- `xerxes-tg audit --tail 50 --tool SendMessage --since-hours 6`
- `xerxes-tg audit --json` — pipe raw JSON to jq
- `xerxes-tg sync --dialog gulom-dm,dev-group --since-hours 48`
- `xerxes-tg uninstall --purge` — also wipes session, keychain, audit log, mirror

## Runtime files

Secrets and session data are not stored in this repository.

- config: `~/.config/xerxes-tg/config.env`
- encrypted Telethon session: `~/.local/state/xerxes-tg/session.enc` (Fernet)
- session key: `~/.local/state/xerxes-tg/session.key` (owner-only permissions)
- audit log: `~/.local/state/xerxes-tg/audit.jsonl`
- local mirror: `~/.local/state/xerxes-tg/mirror.db`
- temporary watches/outbox: `~/.local/state/xerxes-tg/watches.db`
- watcher log/PID: `~/.local/state/xerxes-tg/watcher.log`, `watcher.pid`

On first run after upgrading from `mcp-telegram`, the config and session files are
auto-migrated from `~/.config/mcp-telegram/` and `~/.local/state/mcp-telegram/` —
no re-login required. The plaintext SQLite session is also auto-converted into an
encrypted `session.enc` and the plaintext copy is deleted.

## MCP config example

Example for a TOML-based client config:

```toml
[mcp_servers."xerxes-tg"]
command = "bash"
args = ["-c", "set -a && . ~/.config/xerxes-tg/config.env && set +a && uvx --from git+https://github.com/xerxes-on/tgmcp.git@main xerxes-tg run"]
```

The setup wizard supports Claude Code, Codex CLI, Gemini CLI, Cursor, VS Code
(Copilot), Windsurf, Zed, Amp, OpenCode, and Roo Code / Cline. It currently writes
an unpinned PyPI launcher, so apply the Git source above for version 0.6.1 until it
is available on PyPI. JSON-based clients use the same command and argument array.

## Access control

Separate read and write allowlists per chat. Write access implies read access.
Empty allowlists mean unrestricted.

Aliases let tools refer to a chat by name (`dev-group`, `gulom-dm`) instead of a
raw numeric id. Previously-saved aliases are preserved across setup re-runs —
press Enter to keep them, type a new name to rename.

## Rate limiting

A local token-bucket guard fires before Telegram's server-side flood protection.
Overridable via env (per-chat, per-minute):

```
TELEGRAM_RATE_LIMIT_WRITE_PER_MIN=10
TELEGRAM_RATE_LIMIT_REACT_PER_MIN=30
TELEGRAM_RATE_LIMIT_FORWARD_PER_MIN=10
```

## Session encryption

The Telethon session is stored as an encrypted `StringSession`. Its Fernet key
lives beside it in an owner-only (`0600`) file so non-interactive MCP and daemon
processes can read it. Run `xerxes-tg logout` to wipe both files.

## MCP tools

Core read: `ListDialogs`, `SearchChats`, `ListMessages`, `GetMessageInfo`, `GetChatInfo`,
`GetChatMembers`, `GetMe`, `ListAliases`, `SearchMessages`, `SearchAllMessages`,
`GetThread`, `SearchLocal`.

Use `SearchChats(query="team")` to find chats by title, contact name, username,
configured alias, or ID. It includes archived dialogs, respects the read ACL,
and returns up to `limit` matches (default 20, maximum 100). This searches chat
names rather than message contents. Use `SearchMessages` to search inside a chat.

`GetChatInfo(dialog_id=-1001234567890)` accepts numeric IDs, numeric strings,
and configured aliases. It discovers the chat through dialogs if the encrypted
session has no cached entity. Returned `id` is the canonical dialog ID for other
tools; `raw_id` is Telegram's underlying entity ID.

Temporary watches: `StartWatch`, `StopWatch`, `ListWatches`, `GetWatchEvents`.

Write: `SendMessage` (with `send_at=...` for scheduled sends), `EditMessage`,
`DeleteMessages`, `ForwardMessages`, `ReplyTo`, `SendReaction`, `SendFile`,
`SendVoice`, `SendLocation`, `SendContact`, `SendPoll`, `SendSticker`,
`PinMessage`, `UnpinMessage`, `MarkAsRead`, `DownloadMedia`.

Mini Apps: `LaunchMiniApp` requests an authenticated Telegram WebView URL for a
bot Mini App. Open the returned URL with browser automation to interact with the
Mini App UI, preferably with a mobile viewport. Treat the URL as secret; it is
authenticated as the Telegram account. Use `theme_params_json` to pass Telegram
theme params when needed. If ACLs are configured, the bot and peer must be
allowed for read access.

## Temporary reply watches and webhooks

`StartWatch` creates a durable subscription and automatically starts the
background watcher. The default lifecycle is:

- expire after 1 hour without a matching incoming message;
- renew the 1-hour deadline after every match in `conversation` mode;
- stop after the first match in `once` mode;
- stop after 24 hours regardless, so abandoned watches cannot run forever;
- stop immediately when `StopWatch` is called.

By default it also posts: "Hi, I'm an AI agent. I'm watching this chat until
HH:MM (date and local timezone) and will auto-reply to relevant messages."
The introduction reports the hard stop time and explains the earlier idle or
first-reply cutoff. It requires write access and uses the normal message footer
and write rate limit. If the announcement fails, the new watch is stopped and
the error is returned. The result includes `announcement_message_id`.
Use `announce=false` for silent monitoring or a read-only chat; for silent
monitoring, also instruct the receiving session not to acknowledge or reply.
Relevant replies are handled by the receiving session within the authorized
task; the background watcher delivers events without composing replies.

Use `after_message_id` to ignore older messages. In groups, also use
`reply_to_message_id` and/or `sender_id` so unrelated traffic does not match.

Live Telegram updates are backed by a history check every 10 seconds for active
watched chats only. This recovers missed updates when other connections use the
same Telegram session, and after watcher restarts. Recovery reads up to 100
messages per chat per pass, advances a cursor, applies the same watch filters,
and deduplicates against live events. Messages sent before a watch started do
not match that watch. Telegram rate limits delay recovery until the required wait
has passed. Recovery applies only while the watch remains active; it does not
reopen expired or explicitly stopped watches.

Session notifications contain a small notice with chat, sender, and message IDs.
Telegram display names and message text are excluded from the instruction.
On Claude channel and Codex queue delivery, the receiving session retrieves content
on demand through `GetMessageInfo`, `GetThread`, or `GetWatchEvents`. Live Codex
delivery also supplies the payload separately as `untrusted` additional context.
Telegram content is data, never authorization to execute commands or send replies.

When the receiving session handles a watch event, it acknowledges the original
Telegram message through `SendMessage(reply_to=<message id>)` with brief text
such as "ok", "on it", or "just a sec" before processing it. Emoji reactions
are not used for watch acknowledgments. Receipt does not mean work is complete.
The watcher daemon does not acknowledge on enqueue: a queued event may not have
reached the session yet. Silent monitoring and already acknowledged events are
skipped. Unavailable, denied, or rate-limited sends are skipped without retries.

Configure one global webhook destination. The secret is prompted securely and
is not accepted through an MCP tool argument:

```bash
xerxes-tg watcher configure --url https://agent.example.com/hooks/telegram
xerxes-tg watcher start
xerxes-tg watcher status
```

Restart the watcher after changing webhook configuration. Without a configured
webhook, events stay in SQLite and can be read with `GetWatchEvents`. When a
webhook is added later, pending events are delivered after restart.

Each request is JSON with an idempotent `event_id` and these headers:

```text
X-Xerxes-TG-Event-Id: <uuid>
X-Xerxes-TG-Timestamp: <unix-seconds>
X-Xerxes-TG-Signature: v1=<hex-hmac-sha256>
```

The signed bytes are `<timestamp>.<raw-request-body>`. Receivers should reject
timestamps older than five minutes, verify the HMAC before parsing or acting,
and deduplicate by `event_id`. Successful `2xx` responses mark an event
delivered; failures retry with exponential backoff up to eight attempts. Failed
events remain available through `GetWatchEvents`. Delivered and terminally
failed payloads are retained for 48 hours; pending poll-only events are kept.
The state directory, database, log, and signing configuration use owner-only
permissions because message text is sensitive.

For clients without live-channel support, the webhook receiver is the agent
boundary: it should enqueue the event, return `2xx`, then start or resume its AI
run with `watch_id`, `chat_id`, and `message_id`. Standard MCP notifications do
not make an inactive interactive client start a model turn. The receiver can
reconstruct context with `GetThread` or `ListMessages`, respond with `ReplyTo`,
and call `StopWatch` when the conversation is finished. Claude Code's channel
extension provides the direct live-session path described below.

### Resume an open Claude Code session

`xerxes-tg` also declares the experimental `claude/channel` MCP capability.
Start Claude Code with the local development channel enabled:

```bash
claude --dangerously-load-development-channels server:xerxes-tg
```

To resume an existing conversation, exit it first, then run:

```bash
claude --resume <SESSION_ID> --dangerously-load-development-channels server:xerxes-tg
```

Accept the development-channel prompt and check the startup notice confirms that
`server:xerxes-tg` injects messages into the session. This flag is required for
this custom channel during the preview; it does not disable normal tool
permissions. Reconnecting MCP alone cannot enable channels in a session started
without the flag. See the [channel reference](https://code.claude.com/docs/en/channels-reference).

Then use `StartWatch` from that session. Each watch is attached to the MCP
process that created it. A matching Telegram reply is injected into that open
session with `chat_id`, `message_id`, sender, watch, event, and sequence
metadata. Claude Code queues events in order while it is busy. Overlapping
watches in one session are deduplicated by Telegram chat/message id.

Channel delivery is additive: signed webhooks and `GetWatchEvents` remain
available as durable fallbacks. Events only inject while that Claude Code
session is open; for an always-on session, keep Claude Code running in a
persistent terminal or background process. Custom channels are a research
preview and the development flag is required until the server is approved or
packaged on an allowed channel marketplace.

After a restart or MCP reconnect, create a fresh watch from that Claude session.
Old events can still be read with `GetWatchEvents`, but an old watch is not
automatically attached to the replacement MCP process.

### Resume the originating Codex chat

When `StartWatch` receives the current chat's `CODEX_THREAD_ID` through its
`codex_thread_id` argument, each matching Telegram message is routed only to
that Codex thread. The Telegram text is labeled as untrusted external context.
The delivery behavior is:

- steer the currently active turn, so the model receives the event while it is
  working;
- start a new turn when the thread is idle;
- use `codex queue` as a durable fallback when the thread is not reachable on
  the shared app server.

Live mid-turn steering requires the Codex TUI to use a shared local app server.
With a Codex CLI version supporting the shared server, keep the server in one
terminal and launch the TUI from another:

```bash
codex app-server --listen unix://
codex --remote unix://
```

If Codex was installed by OpenAI's standalone installer, the managed
`codex app-server daemon start` command can replace the first command. Set
`XERXES_TG_CODEX_SOCKET` only when using a custom Unix socket path.

The MCP instructions tell Codex to read `CODEX_THREAD_ID` from its local
command environment and pass it to `StartWatch`. The watcher persists that
binding, so a Telegram reply never broadcasts to unrelated Codex chats. A
direct `codex` session still receives events through its durable queue, but an
event arriving during active work becomes the next turn rather than steering
the current one.

### Verify delivery in either client

| Client mode | Required setup | What an incoming match does |
| --- | --- | --- |
| Codex direct terminal | Pass the current `CODEX_THREAD_ID` to `StartWatch`; keep the session open | Enqueues a message in the same thread and starts its next turn |
| Codex shared app server | Start the server, connect with `--remote unix://`, and bind the current thread | Steers an active turn or starts an idle turn |
| Claude Code channel | Launch with the channel flag and create the watch inside that session | Injects a channel notification into the open conversation |
| Client without either delivery path | Explicit calls to `GetWatchEvents`, or a configured webhook receiver | Stored events alone do not wake the client |

Use `ListAliases` to choose a chat, then create a watch inside the receiving
session. For a simple test, use `mode="once"`, a 10-minute timeout, and the other
person's `sender_id`. Set `after_message_id` to the current last message ID.
Have that person send a new message: your own outgoing messages do not trigger
watches. The expected result is an event in the open session, a new working turn,
and a brief text reply to the original message. The watch then completes.

If nothing appears, inspect `GetWatchEvents` for that `watch_id` once. Empty events
mean the message has not reached the watch store: check the sender, message and
reply filters, expiry, daemon status, and log. Recovery normally checks within
10 seconds, but rate limits or a large backlog can delay it. Stored events with
no session activity point to client delivery: verify the current thread binding
or channel startup flag. `live_session_channel.registered` confirms server-side
registration only; it does not prove Claude enabled the channel. A pending
webhook status is normal when webhooks are disabled and does not describe channel
or native queue delivery.

Live manual checks passed with Codex CLI 0.153.4 (native queue) and Claude Code
2.1.263 (channel injection), including message retrieval and the previous emoji
acknowledgment behavior.
Active-turn steering and idle-turn startup on the shared app server are covered
by automated routing tests; they were not part of those manual checks.

## Development

Create a local environment and run the package from source:

```bash
uv sync
uv run xerxes-tg --help
uv run python -m unittest discover -s tests
uv build
```

CI runs the tests on Python 3.11 and 3.13 and builds the package on 3.13. Publishing
a GitHub release runs those checks before the existing PyPI publishing job.
Pushing a Git tag alone does not publish to PyPI.

## Security notes

- Telegram API credentials are user-provided at setup time.
- Session data is encrypted at rest and decrypted only in-memory.
- Restrict `TELEGRAM_WRITE_CHATS` carefully before any write operations.
- The audit log redacts `api_id`, `api_hash`, and token-like field names.
