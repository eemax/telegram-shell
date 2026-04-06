"""Structured JSON Lines logger with rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        payload = getattr(record, "payload", None)
        if isinstance(payload, dict):
            data.update(payload)
        else:
            data["msg"] = record.getMessage()
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class EventLogger:
    def __init__(
        self,
        log_dir: Path,
        filename: str = "commands.jsonl",
        max_bytes: int = 1024 * 1024,
        backup_count: int = 3,
    ):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.path = log_dir / filename
        self._logger = logging.getLogger("tgsh")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Avoid duplicate handlers if constructed twice
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(_JsonFormatter())
        self._logger.addHandler(handler)

    def log(self, **fields: Any) -> None:
        self._logger.info("", extra={"payload": fields})

    def command(
        self,
        *,
        user_id: int,
        input_text: str,
        expanded: str,
        exit_code: int | None,
        duration: float,
        timed_out: bool,
        interrupted: bool,
        truncated: bool,
        auto_reset: bool = False,
        recovery_failed: bool = False,
        capture_capped: bool = False,
    ) -> None:
        self.log(
            type="command",
            user_id=user_id,
            input=input_text,
            expanded=expanded,
            exit_code=exit_code,
            duration=round(duration, 3),
            timed_out=timed_out,
            interrupted=interrupted,
            truncated=truncated,
            auto_reset=auto_reset,
            recovery_failed=recovery_failed,
            capture_capped=capture_capped,
        )

    def unauthorized(self, *, user_id: int | None) -> None:
        self.log(type="unauthorized", user_id=user_id)

    def startup(self, **extra: Any) -> None:
        self.log(type="startup", **extra)

    def shutdown(self, **extra: Any) -> None:
        self.log(type="shutdown", **extra)

    def total_size_bytes(self) -> int:
        total = 0
        pattern = f"{self.path.name}*"
        for candidate in self.log_dir.glob(pattern):
            if candidate.is_file():
                try:
                    total += candidate.stat().st_size
                except OSError:
                    pass
        return total

    def retention_budget_bytes(self) -> int:
        return self.max_bytes * (self.backup_count + 1)
