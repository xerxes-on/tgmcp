# mcp-xerxes-tg

`mcp-xerxes-tg` is an MCP server for Telegram built on top of Telethon and MTProto.

It provides:

- Telegram read and write tools for MCP clients
- chat-level access control for read and write operations
- optional aliases for dialogs
- an optional listener mode that can draft or send replies through an external agent CLI
- a setup wizard that writes client configuration for supported coding agents

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
xerxes-tg setup
xerxes-tg run
xerxes-tg listen
xerxes-tg sign-in --api-id <id> --api-hash <hash> --phone-number <number>
xerxes-tg logout
```

## Runtime files

Secrets and session data are not stored in this repository.

- config: `~/.config/mcp-telegram/config.env`
- runtime state: `~/.local/state/mcp-telegram/`

The setup wizard creates the config file and can update MCP settings for supported clients.

## MCP config example

Example for a TOML-based client config:

```toml
[mcp_servers."mcp-telegram"]
command = "bash"
args = ["-c", "set -a && . ~/.config/mcp-telegram/config.env && set +a && uvx --from mcp-xerxes-tg xerxes-tg"]
```

## Access control

The server supports separate read and write allowlists per chat. Write access implies read access. If both allowlists are empty, access is unrestricted.

Aliases can be configured so tools can use stable names instead of raw Telegram IDs.

## Listener mode

Listener mode can watch selected chats and choose one of these behaviors per chat:

- `read`: observe only
- `ask`: draft a reply and require approval
- `auto`: reply automatically when appropriate
- `decide`: let the external agent decide whether to reply

Use `xerxes-tg setup` to configure the listener instead of editing env vars by hand.

## Development

Create a local environment and run the package from source:

```bash
uv sync
uv run xerxes-tg --help
uv build
```

## Security notes

- Telegram API credentials are user-provided at setup time.
- Session files and listener state live outside the repository.
- Restrict `TELEGRAM_WRITE_CHATS` carefully before enabling any automated replies.
