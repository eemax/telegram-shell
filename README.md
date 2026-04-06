# Telegram Shell

`telegram-shell` is a personal Telegram bot that runs commands in a single persistent shell session.

It is designed for one operator: one Telegram bot token, one allowed Telegram user ID, one long-lived shell process, and a small layer around that shell for queueing, formatting, logging, interrupting, and resetting.

## What It Does

- Runs plain text messages as shell commands.
- Keeps shell state between commands.
- Supports built-in bot commands such as `/status`, `/interrupt`, and `/reset`.
- Supports YAML-defined slash-command shortcuts like `/gs` or `/gco branch-name`.
- Splits long output into Telegram-safe message chunks.
- Logs structured events to `logs/commands.jsonl`.
- Auto-recovers from some shell failures and protects against stale queued commands after a reset.
- Caps retained command output so very noisy commands do not keep growing process memory.
- Can recycle the shell automatically after a long idle period.

## Important Behavior

- The shell is persistent. If you run `cd`, `export`, define aliases, or modify shell state, later commands see those changes.
- The bot only allows one Telegram user, configured with `ALLOWED_USER_ID`.
- Commands run serially through a queue. Only one command executes at a time.
- If the shell resets, commands that were queued before the reset are skipped on purpose so they do not run in the wrong shell context.
- Every command is logged, including the original input and expanded shortcut text.
- Logs are rotated with a bounded retention budget.
- The shell session disables history persistence for the managed PTY.

## Requirements

- Python 3.11+
- A Telegram bot token from BotFather
- Your Telegram numeric user ID
- A Unix-like environment with the configured shell program available

## Quick Start

1. Install dependencies:

```bash
uv sync
```

2. Create your environment file:

```bash
cp .env.example .env
```

3. Edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USER_ID=123456789
```

4. Review `config.yaml` and set the shell working directory, timeouts, log retention, idle reset, and shortcuts you want.

5. Run the bot:

```bash
uv run main.py
```

If you already have the virtual environment created, this also works:

```bash
.venv/bin/python main.py
```

## Built-In Commands

- `/help`: Show help text and configured shortcuts.
- `/status`: Show shell liveness, coordinator state, queue depth, timeout, and output limit.
- `/pwd`: Run `pwd` in the managed shell.
- `/interrupt`: Send `Ctrl-C` to the running command.
- `/reset`: Restart the shell session. Queued commands from the previous shell generation are skipped.

## Shortcut Commands

Shortcut commands are configured in `config.yaml` under `commands:`.

Examples from the default config:

- `/gs` -> `git status`
- `/gl` -> `git log --oneline -20`
- `/gd` -> `git diff`
- `/gco my-branch` -> `git checkout 'my-branch'`
- `/claude fix this` -> `claude -p 'fix this'`

Shortcuts can either:

- expand a `template`
- execute an absolute `script` path

See [docs/config.md](docs/config.md) for the full schema.

## Logs

Runtime logs are written to:

```text
logs/commands.jsonl
```

The file is rotated automatically with `RotatingFileHandler`. The default config keeps logs small:

- `max_bytes: 1048576`
- `backup_count: 3`

Logged events include startup, shutdown, unauthorized access attempts, resets, auto-resets, skipped commands, queue rejection, and command execution results.

## Long-Running Notes

If you plan to keep the bot running for weeks:

- tune `shell.max_capture_chars` to bound retained command output
- tune `logging.max_bytes` and `logging.backup_count` to cap on-disk logs
- use `maintenance.idle_reset_after_seconds` to recycle the shell only when idle
- use `/status` to inspect uptime, shell generation, idle time, max RSS, and log size

## Running Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Project Structure

- `main.py`: Telegram handlers, auth loading, coordinator, lifecycle wiring
- `shell_session.py`: long-lived PTY shell process and command execution
- `command_registry.py`: config loading and shortcut expansion
- `formatter.py`: Telegram MarkdownV2 rendering and output chunking
- `logger.py`: structured JSONL event logging
- `config.yaml`: app configuration and shortcuts
- `tests/`: focused unit tests for auth, config validation, coordinator behavior, and send fallback

## More Docs

- [Architecture](docs/architecture.md)
- [Configuration Reference](docs/config.md)
