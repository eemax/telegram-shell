# Architecture

This project is intentionally small. The code is split into a few narrow modules, each with one job:

- `main.py`: application entrypoint, auth loading, Telegram handlers, coordinator, startup, shutdown
- `shell_session.py`: persistent PTY shell management and command execution
- `command_registry.py`: config parsing, validation, and shortcut expansion
- `formatter.py`: Telegram-safe output formatting and chunk splitting
- `logger.py`: JSONL event logging

## High-Level Model

The bot sits between Telegram and a single long-lived shell process:

1. Telegram delivers a message or slash command.
2. The bot checks the sender against `ALLOWED_USER_ID`.
3. Plain text is treated as a shell command.
4. Unknown slash commands are resolved through the shortcut registry.
5. The command is placed onto a bounded in-memory queue.
6. The coordinator executes queued jobs one at a time in the shared shell session.
7. Output is cleaned, formatted, chunked, and sent back to Telegram.
8. Structured events are written to `logs/commands.jsonl`.

## Main Components

## `Bot`

`Bot` is the Telegram-facing layer in `main.py`.

Responsibilities:

- create the `python-telegram-bot` application
- register command and text handlers
- authorize incoming updates
- convert incoming messages into `Job` objects
- send formatted replies back to Telegram

Built-in commands are handled explicitly:

- `/help`
- `/status`
- `/pwd`
- `/interrupt`
- `/reset`

Any other slash command is treated as a configured shortcut lookup.

Plain text messages are executed directly in the shell.

## `Coordinator`

`Coordinator` is the core state machine.

Responsibilities:

- hold the bounded job queue
- serialize command execution
- coordinate resets and interrupts
- check shell health before running a command
- handle timeout recovery
- skip stale queued jobs after a shell generation change
- recycle the shell when it has been idle long enough

The coordinator state enum is:

- `IDLE`
- `RUNNING`
- `INTERRUPTING`
- `RESETTING`
- `ERROR`

Only one command can run at a time. `execution_lock` protects the boundary between:

- running a command
- resetting the shell

## Shell Generation

The coordinator tracks a shell `generation` counter.

Each queued `Job` stores the generation that existed when it was enqueued. If the shell is reset before that job starts, the job is skipped.

This is deliberate. Without that protection, queued commands could accidentally run in a new shell that no longer has the expected:

- working directory
- exported variables
- shell functions
- temporary state created by earlier commands

That is the main guardrail that preserves the "persistent shell" contract after a reset.

## Maintenance Loop

If `maintenance.idle_reset_after_seconds` is greater than zero, the coordinator starts a small background maintenance task.

That task:

- wakes periodically
- checks whether the shell is truly idle
- confirms there is no running command
- confirms the queue is empty
- resets the shell only if the idle threshold has been exceeded

This avoids letting shell-side state accumulate forever while still preserving the persistent-shell behavior during active work.

## `ShellSession`

`ShellSession` wraps a single `pexpect.spawn` PTY process.

Responsibilities:

- start the shell process
- suppress prompt noise and echo
- probe the shell for health
- execute commands with exit-code sentinels
- interrupt long-running commands
- truncate oversized output
- cap retained raw output while a command is still running
- reset the shell by stopping and starting a new PTY

Key details:

- The shell program and working directory come from `config.yaml`.
- The shell is launched with environment adjustments such as `TERM=dumb` and pager suppression.
- Shell history persistence is disabled for the managed PTY.
- Prompt noise is reduced by clearing prompt variables and disabling echo.
- Readiness and exit codes are detected with randomly generated sentinel tokens.

## Command Execution Path

When a command runs:

1. The raw command is sent into the PTY.
2. The wrapper appends a sentinel print that encodes the command exit code.
3. Output is read incrementally from the PTY instead of being buffered all at once.
4. Output is normalized:
   - CRLF and CR are converted to LF
   - ANSI escape codes are removed
5. Only up to `max_capture_chars` of raw output is retained in memory while waiting for the sentinel.
6. Output is truncated for delivery if it exceeds `max_output_chars`.
7. A `CommandResult` is returned to the coordinator.

## Timeout and Recovery

If a command times out:

1. `ShellSession` sends `Ctrl-C`.
2. The coordinator probes the shell.
3. If the probe succeeds, the shell is considered recovered.
4. If the probe fails, the coordinator attempts an automatic reset.
5. If reset succeeds, the shell generation increments and older queued jobs are skipped.
6. If reset fails, the coordinator enters `ERROR`.

Once in `ERROR`, command execution stops until `/reset` succeeds.

## `CommandRegistry`

`CommandRegistry` loads and validates `config.yaml`.

It turns shortcut definitions into `Shortcut` objects and expands slash commands into shell strings.

Supported shortcut styles:

- `template`
- `script`

Argument behavior is controlled with:

- `require_args`
- `quote_args`
- `pass_args`

The loader performs validation for:

- config root shape
- section types
- numeric ranges
- shortcut name format
- reserved built-in command collisions
- shortcut field combinations

## `formatter.py`

Telegram uses MarkdownV2, which is strict and easy to break with shell output.

`formatter.py` is responsible for:

- escaping MarkdownV2 text
- escaping code block content
- building command headers and footers
- splitting long output into safe chunks
- avoiding splits in the middle of escape sequences

Each command reply typically includes:

- a header with the original command
- a fenced code block with output
- a footer with exit code and duration, or timeout/recovery status

## `logger.py`

`logger.py` writes structured JSONL events to `logs/commands.jsonl`.

It uses a rotating file handler with:

- `maxBytes = 10 MiB`
- `backupCount = 5`

Events include:

- startup
- shutdown
- command
- unauthorized
- interrupt
- reset
- auto-reset
- auto-reset failure
- idle reset
- idle reset failure
- queue rejection
- skipped queued commands

## Lifecycle

Startup flow:

1. load config from `config.yaml`
2. load auth from `.env`
3. create logger
4. create shell session
5. build Telegram app and coordinator
6. start shell session
7. initialize and start polling

Shutdown flow:

1. log shutdown
2. stop Telegram updater
3. stop Telegram application
4. stop coordinator consumer task
5. stop the shell PTY

## Design Constraints

This project intentionally optimizes for simplicity over multi-user flexibility.

Current constraints:

- one bot
- one allowed Telegram user
- one shared persistent shell
- one command at a time
- in-memory queue only
- polling mode only
- no persistence for queued jobs across process restarts

That keeps the implementation compact and predictable, but it also means this is a personal operator tool, not a multi-tenant remote execution service.
