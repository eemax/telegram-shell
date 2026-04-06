# Configuration Reference

This project reads configuration from two places:

- `.env` for secrets and access control
- `config.yaml` for runtime behavior and shortcuts

Both are required for a real deployment.

## Environment Variables

The bot loads `.env` from the project root with `python-dotenv`.

Supported variables:

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | string | Telegram bot token from BotFather. |
| `ALLOWED_USER_ID` | yes | integer | The only Telegram user ID allowed to use the bot. |

Example:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABCDEF_example_token
ALLOWED_USER_ID=123456789
```

Validation rules:

- `TELEGRAM_BOT_TOKEN` must be non-empty.
- `ALLOWED_USER_ID` must contain exactly one integer.
- Comma-separated values are rejected.

Deprecated and rejected variables:

- `ALLOWED_USER_IDS`
- `ALLOWED_CHAT_IDS`

If either legacy variable is present, startup fails with a clear error telling you to use `ALLOWED_USER_ID`.

## `config.yaml`

The YAML config file is loaded from the project root at `config.yaml`.

The root value must be a mapping.

Top-level sections:

- `shell`
- `telegram`
- `queue`
- `logging`
- `maintenance`
- `commands`

Example:

```yaml
shell:
  program: /bin/zsh
  cwd: /Users/max
  timeout_seconds: 120
  max_output_chars: 12000
  max_capture_chars: 200000
  probe_timeout_seconds: 5

telegram:
  message_chunk_size: 3500
  message_chunk_min: 2500
  chunk_delay_ms: 250

queue:
  max_pending: 5

logging:
  max_bytes: 1048576
  backup_count: 3

maintenance:
  idle_reset_after_seconds: 21600

commands:
  gs:
    template: "git status"

  gco:
    template: "git checkout {args}"
    require_args: true
    quote_args: true
```

## `shell`

Controls the managed PTY shell process.

| Key | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `program` | string | `"/bin/zsh"` | must be a string | Shell executable passed to `pexpect.spawn`. |
| `cwd` | string | `Path.home()` | must be a string | Working directory used when the shell starts or resets. |
| `timeout_seconds` | number | `120` | must be `> 0` | Maximum time a command may run before timeout handling starts. |
| `max_output_chars` | integer | `12000` | must be `>= 1` | Maximum cleaned output size retained from a command. Longer output is truncated. |
| `max_capture_chars` | integer | `200000` | must be `>= 1` and `>= max_output_chars` | Maximum raw output retained in memory while waiting for command completion. Extra output is discarded after the cap is reached, but the command continues running. |
| `probe_timeout_seconds` | number | `5` | must be `> 0` | Timeout used for shell readiness and health probes. |

Notes:

- `cwd` is reapplied when the shell restarts.
- If the shell resets, any queued commands from the previous shell generation are skipped.
- The managed PTY disables shell history persistence by setting history-related environment and shell variables.

## `telegram`

Controls reply formatting and pacing.

| Key | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `message_chunk_size` | integer | `3500` | must be `>= 1` | Preferred upper bound for a single outgoing Telegram message chunk. |
| `message_chunk_min` | integer | `2500` | must be `>= 1` and `<= message_chunk_size` | Lower bound used when searching for a good split point in long output. |
| `chunk_delay_ms` | integer | `250` | must be `>= 0` | Delay between sending successive output chunks. |

Notes:

- Output is sent with Telegram MarkdownV2.
- Long output is chunked into fenced code blocks.
- If Markdown sending fails, the bot retries the exact text as plain text without a parse mode.

## `queue`

Controls queued command depth.

| Key | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `max_pending` | integer | `5` | must be `>= 1` | Maximum number of queued jobs waiting behind the current command. |

Notes:

- The queue is in-memory only.
- Commands run strictly one at a time.
- If the queue is full, the new command is rejected and logged.

## `logging`

Controls JSONL log rotation.

| Key | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `max_bytes` | integer | `1048576` | must be `>= 1` | Maximum size of a single log file before rotation. |
| `backup_count` | integer | `3` | must be `>= 0` | Number of rotated backup files to keep. |

Notes:

- Total retained log budget is approximately `max_bytes * (backup_count + 1)`.
- Logs are written to `logs/commands.jsonl` and rotated by size.

## `maintenance`

Controls background maintenance behavior.

| Key | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `idle_reset_after_seconds` | number | `0` | must be `>= 0` | If greater than zero, the shell is reset after being idle for at least this many seconds, but only when no command is running and the queue is empty. |

Notes:

- `0` disables idle recycling.
- Idle recycling increments the shell generation just like any other reset.
- Because recycling only happens when idle and the queue is empty, it does not interrupt work in progress.

## `commands`

Defines custom slash-command shortcuts.

Each entry under `commands` becomes a Telegram slash command like `/name`.

Shortcut names:

- must match `^[a-zA-Z][a-zA-Z0-9_]*$`
- must not collide with built-in commands

Reserved built-in names:

- `help`
- `status`
- `pwd`
- `interrupt`
- `reset`

Each shortcut must be a mapping with exactly one of:

- `template`
- `script`

## Shortcut Fields

| Field | Type | Default | Allowed With | Description |
| --- | --- | --- | --- | --- |
| `template` | string | none | template shortcuts | Shell command template. May contain `{args}`. |
| `script` | string | none | script shortcuts | Absolute script path to execute. Must start with `/`. |
| `require_args` | boolean | `false` | both | If `true`, the user must pass arguments. |
| `quote_args` | boolean | `false` | both | If `true`, the entire argument string is shell-quoted with `shlex.quote()`. |
| `pass_args` | boolean | `false` | script only | If `true`, append the user argument string to the script invocation. |

Validation rules:

- A shortcut must define exactly one of `template` or `script`.
- `script` must be an absolute path.
- `pass_args` is only valid with `script`.
- Boolean flags must be actual YAML booleans, not strings like `"true"`.

## Template Shortcuts

Template shortcuts are string expansions.

Example:

```yaml
commands:
  gco:
    template: "git checkout {args}"
    require_args: true
    quote_args: true
```

Behavior:

- `/gco feature/my-branch` becomes `git checkout 'feature/my-branch'`
- if `{args}` is absent from `template`, the template is returned unchanged
- if `require_args: true` and no args are supplied, the bot replies with a usage error

## Script Shortcuts

Script shortcuts run a fixed script path.

Without argument passing:

```yaml
commands:
  deploy:
    script: "/Users/max/bin/deploy-prod.sh"
```

With argument passing:

```yaml
commands:
  note:
    script: "/Users/max/bin/note.sh"
    pass_args: true
    quote_args: true
```

Behavior:

- `/note buy milk` becomes `/Users/max/bin/note.sh 'buy milk'`
- if `pass_args: false`, any extra user text is ignored

## Defaults Summary

If a section or field is omitted, these defaults apply:

```yaml
shell:
  program: /bin/zsh
  cwd: <home directory>
  timeout_seconds: 120
  max_output_chars: 12000
  max_capture_chars: 200000
  probe_timeout_seconds: 5

telegram:
  message_chunk_size: 3500
  message_chunk_min: 2500
  chunk_delay_ms: 250

queue:
  max_pending: 5

logging:
  max_bytes: 1048576
  backup_count: 3

maintenance:
  idle_reset_after_seconds: 0

commands: {}
```

## Startup Failure Cases

The app exits at startup if any of the following are true:

- `.env` is missing required values
- `ALLOWED_USER_ID` is not a single integer
- deprecated auth env vars are present
- `config.yaml` is missing
- YAML is invalid
- the YAML root or sections are not mappings
- numeric fields have the wrong type or invalid range
- shortcut names or shortcut field combinations are invalid

## Related Files

- `config.yaml`
- `.env`
- `.env.example`
- [Architecture](architecture.md)
