# xerxes-tg

`xerxes-tg` is an MCP server for Telegram built on top of Telethon and MTProto.

It provides:

- Telegram read and write MCP tools for any MCP client
- chat-level read/write access control with aliasing
- scheduled sends, reply-by-reference, thread view, global search
- temporary reply watches with signed webhook delivery and polling fallback
- Telegram Mini App WebView URL generation for browser automation
- a local SQLite + FTS5 mirror with on-demand sync for cross-chat search
- an on-disk audit log (48h retention) and local rate limiting
- OS-keychain-protected session storage (macOS Keychain / Windows Credential Locker / Secret Service)
- a setup wizard that writes MCP client configuration for popular coding agents

## Install

With `uv`:

```bash
uv tool install .
```

Run the setup wizard:

```bash
xerxes-tg setup
```

Or run directly from the source tree:

```bash
uv run xerxes-tg setup
```

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
args = ["-c", "set -a && . ~/.config/xerxes-tg/config.env && set +a && uvx --from xerxes-tg xerxes-tg"]
```

The setup wizard writes this automatically for Claude Code, Codex CLI, Gemini CLI,
Cursor, VS Code (Copilot), Windsurf, Zed, Amp, OpenCode, and Roo Code / Cline.

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

Core read: `ListDialogs`, `ListMessages`, `GetMessageInfo`, `GetChatInfo`,
`GetChatMembers`, `GetMe`, `ListAliases`, `SearchMessages`, `SearchAllMessages`,
`GetThread`, `SearchLocal`.

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

Use `after_message_id` to ignore older messages. In groups, also use
`reply_to_message_id` and/or `sender_id` so unrelated traffic does not match.

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

The webhook receiver is the agent boundary: it should enqueue the event, return
`2xx`, then start or resume its AI run with `watch_id`, `chat_id`, and
`message_id`. An MCP server cannot assume that an inactive interactive client
will turn a server notification into a new model turn. The agent can reconstruct
context with `GetThread` or `ListMessages`, respond with `ReplyTo`, and call
`StopWatch` when the conversation is finished.

## Development

Create a local environment and run the package from source:

```bash
uv sync
uv run xerxes-tg --help
uv build
```

## Security notes

- Telegram API credentials are user-provided at setup time.
- Session data is encrypted at rest and decrypted only in-memory.
- Restrict `TELEGRAM_WRITE_CHATS` carefully before any write operations.
- The audit log redacts `api_id`, `api_hash`, and token-like field names.
