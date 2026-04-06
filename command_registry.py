"""Slash-command shortcut loader and expander."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

BUILTIN_NAMES = frozenset({"help", "status", "pwd", "interrupt", "reset"})
NAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')


class ConfigError(ValueError):
    pass


class MissingArgsError(ValueError):
    pass


@dataclass
class Shortcut:
    name: str
    template: str | None = None
    script: str | None = None
    require_args: bool = False
    quote_args: bool = False
    pass_args: bool = False


@dataclass
class AppConfig:
    shell_program: str
    shell_cwd: str
    timeout_seconds: float
    max_output_chars: int
    max_capture_chars: int
    probe_timeout_seconds: float
    chunk_size: int
    chunk_min: int
    chunk_delay_ms: int
    max_pending: int
    log_max_bytes: int
    log_backup_count: int
    idle_reset_after_seconds: float
    shortcuts: dict[str, Shortcut]


def _coerce_bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    raise ConfigError(f"{key} must be a boolean")


def _coerce_mapping(v: Any, key: str) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    raise ConfigError(f"{key} must be a mapping")


def _coerce_str(v: Any, key: str) -> str:
    if isinstance(v, str):
        return v
    raise ConfigError(f"{key} must be a string")


def _coerce_int(v: Any, key: str, *, minimum: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{key} must be an integer")
    if minimum is not None and v < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return v


def _coerce_float(v: Any, key: str, *, positive: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{key} must be a number")
    out = float(v)
    if positive and out <= 0:
        raise ConfigError(f"{key} must be > 0")
    return out


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open() as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML: {e}") from e

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    shell = _coerce_mapping(raw.get("shell"), "shell")
    tg = _coerce_mapping(raw.get("telegram"), "telegram")
    queue = _coerce_mapping(raw.get("queue"), "queue")
    logging_cfg = _coerce_mapping(raw.get("logging"), "logging")
    maintenance = _coerce_mapping(raw.get("maintenance"), "maintenance")
    commands = _coerce_mapping(raw.get("commands"), "commands")

    shortcuts: dict[str, Shortcut] = {}
    for name, spec in commands.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(
                f"Invalid shortcut name '{name}': must match [a-zA-Z][a-zA-Z0-9_]*"
            )
        if name in BUILTIN_NAMES:
            raise ConfigError(
                f"Shortcut '{name}' collides with built-in command"
            )
        if not isinstance(spec, dict):
            raise ConfigError(f"Shortcut '{name}' must be a mapping")

        template = spec.get("template")
        script = spec.get("script")
        if (template is None) == (script is None):
            raise ConfigError(
                f"Shortcut '{name}' must have exactly one of 'template' or 'script'"
            )
        if template is not None:
            template = _coerce_str(template, f"{name}.template")
        if script is not None:
            script = _coerce_str(script, f"{name}.script")

        require_args = _coerce_bool(spec.get("require_args", False), f"{name}.require_args")
        quote_args = _coerce_bool(spec.get("quote_args", False), f"{name}.quote_args")
        pass_args = _coerce_bool(spec.get("pass_args", False), f"{name}.pass_args")

        if script is not None:
            if not isinstance(script, str) or not script.startswith("/"):
                raise ConfigError(
                    f"Shortcut '{name}': script path must be absolute"
                )
        else:
            if pass_args:
                raise ConfigError(
                    f"Shortcut '{name}': pass_args is only valid with 'script'"
                )

        shortcuts[name] = Shortcut(
            name=name,
            template=template,
            script=script,
            require_args=require_args,
            quote_args=quote_args,
            pass_args=pass_args,
        )

    shell_program = _coerce_str(shell.get("program", "/bin/zsh"), "shell.program")
    shell_cwd = _coerce_str(shell.get("cwd", str(Path.home())), "shell.cwd")
    timeout_seconds = _coerce_float(
        shell.get("timeout_seconds", 120), "shell.timeout_seconds", positive=True
    )
    max_output_chars = _coerce_int(
        shell.get("max_output_chars", 12000), "shell.max_output_chars", minimum=1
    )
    max_capture_chars = _coerce_int(
        shell.get("max_capture_chars", 200000), "shell.max_capture_chars", minimum=1
    )
    if max_capture_chars < max_output_chars:
        raise ConfigError("shell.max_capture_chars must be >= shell.max_output_chars")
    probe_timeout_seconds = _coerce_float(
        shell.get("probe_timeout_seconds", 5),
        "shell.probe_timeout_seconds",
        positive=True,
    )
    chunk_size = _coerce_int(
        tg.get("message_chunk_size", 3500), "telegram.message_chunk_size", minimum=1
    )
    chunk_min = _coerce_int(
        tg.get("message_chunk_min", 2500), "telegram.message_chunk_min", minimum=1
    )
    if chunk_min > chunk_size:
        raise ConfigError("telegram.message_chunk_min must be <= telegram.message_chunk_size")
    chunk_delay_ms = _coerce_int(
        tg.get("chunk_delay_ms", 250), "telegram.chunk_delay_ms", minimum=0
    )
    max_pending = _coerce_int(queue.get("max_pending", 5), "queue.max_pending", minimum=1)
    log_max_bytes = _coerce_int(
        logging_cfg.get("max_bytes", 1024 * 1024), "logging.max_bytes", minimum=1
    )
    log_backup_count = _coerce_int(
        logging_cfg.get("backup_count", 3), "logging.backup_count", minimum=0
    )
    idle_reset_after_seconds = _coerce_float(
        maintenance.get("idle_reset_after_seconds", 0),
        "maintenance.idle_reset_after_seconds",
    )
    if idle_reset_after_seconds < 0:
        raise ConfigError("maintenance.idle_reset_after_seconds must be >= 0")

    return AppConfig(
        shell_program=shell_program,
        shell_cwd=shell_cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        max_capture_chars=max_capture_chars,
        probe_timeout_seconds=probe_timeout_seconds,
        chunk_size=chunk_size,
        chunk_min=chunk_min,
        chunk_delay_ms=chunk_delay_ms,
        max_pending=max_pending,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        idle_reset_after_seconds=idle_reset_after_seconds,
        shortcuts=shortcuts,
    )


class CommandRegistry:
    def __init__(self, shortcuts: dict[str, Shortcut]):
        self._shortcuts = shortcuts

    def get(self, name: str) -> Shortcut | None:
        return self._shortcuts.get(name)

    def names(self) -> list[str]:
        return sorted(self._shortcuts.keys())

    def all(self) -> dict[str, Shortcut]:
        return dict(self._shortcuts)

    def expand(self, name: str, args: str) -> str:
        """Expand a shortcut with its args into a shell command string.

        Raises KeyError if the shortcut doesn't exist.
        Raises MissingArgsError if args are required but not given.
        """
        sc = self._shortcuts.get(name)
        if sc is None:
            raise KeyError(name)

        args = args.strip()
        if sc.require_args and not args:
            raise MissingArgsError(name)

        if sc.template is not None:
            if "{args}" in sc.template:
                quoted = shlex.quote(args) if (args and sc.quote_args) else args
                return sc.template.replace("{args}", quoted)
            return sc.template

        assert sc.script is not None
        if not sc.pass_args or not args:
            return sc.script
        quoted = shlex.quote(args) if sc.quote_args else args
        return f"{sc.script} {quoted}"
