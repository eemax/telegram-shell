"""Telegram Shell Bot entrypoint: handlers, coordinator, state machine."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from command_registry import (
    AppConfig,
    BUILTIN_NAMES,
    CommandRegistry,
    ConfigError,
    MissingArgsError,
    load_config,
)
from formatter import build_error_message, build_messages
from logger import EventLogger
from shell_session import CommandResult, ShellSession


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
ENV_PATH = PROJECT_DIR / ".env"
LOG_DIR = PROJECT_DIR / "logs"


class State(Enum):
    IDLE = auto()
    RUNNING = auto()
    INTERRUPTING = auto()
    RESETTING = auto()
    ERROR = auto()


@dataclass
class Job:
    command: str        # original user input
    expanded: str       # shell string to execute
    reply_target: int
    user_id: int
    message_id: int
    generation: int


@dataclass
class AuthConfig:
    user_id: int


SESSION_RESET_SKIP_MESSAGE = (
    "Shell session changed before this command could run. "
    "To avoid using stale shell state, it was skipped. Please resend it."
)


def _parse_single_id(name: str, raw: str | None) -> int:
    value = (raw or "").strip()
    if not value:
        raise SystemExit(f"{name} must contain exactly one id")
    if "," in value:
        raise SystemExit(f"{name} must contain exactly one id")
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"Invalid ID in {name}: {value!r}")


def load_auth() -> tuple[str, AuthConfig]:
    load_dotenv(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from environment")
    legacy_vars = [
        name for name in ("ALLOWED_USER_IDS", "ALLOWED_CHAT_IDS")
        if os.environ.get(name)
    ]
    if legacy_vars:
        names = ", ".join(legacy_vars)
        raise SystemExit(
            f"Use ALLOWED_USER_ID only; legacy env vars are no longer supported: {names}"
        )
    user_id = _parse_single_id("ALLOWED_USER_ID", os.environ.get("ALLOWED_USER_ID"))
    return token, AuthConfig(user_id=user_id)


def _format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{int(value)}B"


def _format_seconds(value: float) -> str:
    seconds = max(0, int(value))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _get_process_max_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


# -------------------- Coordinator --------------------


class Coordinator:
    def __init__(
        self,
        shell: ShellSession,
        config: AppConfig,
        event_log: EventLogger,
        send_reply,  # async callable(reply_target, messages: list[str])
    ):
        self.shell = shell
        self.config = config
        self.event_log = event_log
        self.send_reply = send_reply

        self.state: State = State.IDLE
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=config.max_pending)
        # Serializes the currently-running command vs. a reset attempt.
        self.execution_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._stopping = False
        self._generation = 0
        self._started_monotonic = time.monotonic()
        self._last_activity_monotonic = self._started_monotonic
        self._shell_started_monotonic = self._started_monotonic

    def start(self) -> None:
        self._consumer_task = asyncio.create_task(self._consume(), name="coordinator")
        if self.config.idle_reset_after_seconds > 0:
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="coordinator-maintenance",
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):
                pass

    def queue_depth(self) -> int:
        return self.queue.qsize()

    def current_generation(self) -> int:
        return self._generation

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity_monotonic

    def shell_age_seconds(self) -> float:
        return time.monotonic() - self._shell_started_monotonic

    def _mark_activity(self) -> None:
        self._last_activity_monotonic = time.monotonic()

    def _mark_shell_reset(self) -> None:
        now = time.monotonic()
        self._generation += 1
        self._last_activity_monotonic = now
        self._shell_started_monotonic = now

    async def try_enqueue(self, job: Job) -> bool:
        """Returns True if enqueued, False if queue full."""
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            self.event_log.log(
                type="queue_rejected",
                user_id=job.user_id,
                input=job.command,
            )
            return False

    # -------- consumer loop --------

    async def _consume(self) -> None:
        while not self._stopping:
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._handle_command(job)
            except Exception as e:
                logging.exception("coordinator error")
                try:
                    msg = build_error_message(f"Internal error: {e}")
                    await self.send_reply(job.reply_target, [msg])
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    async def _maintenance_loop(self) -> None:
        interval = min(max(self.config.idle_reset_after_seconds / 4, 5.0), 60.0)
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._maybe_idle_reset()
            except Exception:
                logging.exception("maintenance loop error")

    async def _maybe_idle_reset(self) -> bool:
        threshold = self.config.idle_reset_after_seconds
        if threshold <= 0:
            return False
        if self.state != State.IDLE or self.queue_depth() > 0:
            return False
        if self.idle_seconds() < threshold:
            return False

        async with self.execution_lock:
            if self._stopping:
                return False
            if self.state != State.IDLE or self.queue_depth() > 0:
                return False
            idle_for = self.idle_seconds()
            if idle_for < threshold:
                return False

            self.state = State.RESETTING
            try:
                await self.shell.reset()
                self._mark_shell_reset()
                self.state = State.IDLE
                self.event_log.log(
                    type="idle_reset",
                    generation=self._generation,
                    idle_seconds=round(idle_for, 3),
                )
                return True
            except Exception as e:
                self.state = State.ERROR
                self.event_log.log(type="idle_reset_failed", error=str(e))
                return False

    async def _handle_command(self, job: Job) -> None:
        if self.state == State.ERROR:
            await self.send_reply(
                job.reply_target,
                [build_error_message(
                    "Shell is in error state. Use /reset to attempt recovery."
                )],
            )
            return

        auto_reset = False
        reset_after_timeout = False
        skip_message: str | None = None
        fatal_message: str | None = None
        result: CommandResult | None = None

        async with self.execution_lock:
            if job.generation != self._generation:
                skip_message = SESSION_RESET_SKIP_MESSAGE
            # Pre-command health check
            elif not self.shell.process_alive() or not await self.shell.probe():
                try:
                    await self.shell.reset()
                    self._mark_shell_reset()
                    auto_reset = True
                    self.state = State.IDLE
                    self.event_log.log(
                        type="auto_reset",
                        reason="pre_command_probe_failed",
                        generation=self._generation,
                    )
                    skip_message = SESSION_RESET_SKIP_MESSAGE
                except Exception as e:
                    self.state = State.ERROR
                    self.event_log.log(type="auto_reset_failed", error=str(e))
                    fatal_message = (
                        "Shell failed to restart. No commands will execute until /reset succeeds."
                    )
            else:
                self.state = State.RUNNING
                try:
                    result = await self.shell.run_command(
                        job.expanded, self.config.timeout_seconds
                    )
                except Exception as e:
                    logging.exception("run_command error")
                    result = CommandResult(
                        command=job.command,
                        expanded=job.expanded,
                        output=f"(internal error: {e})",
                        exit_code=None,
                        duration_seconds=0.0,
                    )

                result.command = job.command
                result.expanded = job.expanded
                result.auto_reset = auto_reset

                if result.timed_out:
                    ok = await self.shell.probe()
                    if not ok:
                        try:
                            await self.shell.reset()
                            self._mark_shell_reset()
                            reset_after_timeout = True
                            self.event_log.log(
                                type="auto_reset",
                                reason="timeout_probe_failed",
                                generation=self._generation,
                            )
                        except Exception as e:
                            self.state = State.ERROR
                            result.recovery_failed = True
                            self.event_log.log(
                                type="auto_reset_failed",
                                reason="timeout_probe_failed",
                                error=str(e),
                            )

                if self.state != State.ERROR:
                    self.state = State.IDLE
                    self._mark_activity()

        if fatal_message is not None:
            await self.send_reply(job.reply_target, [build_error_message(fatal_message)])
            return
        if skip_message is not None:
            self.event_log.log(
                type="command_skipped",
                reason="session_reset_before_execution",
                user_id=job.user_id,
                input=job.command,
                generation=job.generation,
                current_generation=self._generation,
                auto_reset=auto_reset,
            )
            await self.send_reply(job.reply_target, [build_error_message(skip_message)])
            self._mark_activity()
            return
        assert result is not None

        # Send reply (outside the lock)
        messages = build_messages(
            result,
            chunk_hi=self.config.chunk_size,
            chunk_lo=self.config.chunk_min,
            auto_reset_notice=auto_reset,
            reset_after_timeout=reset_after_timeout,
        )
        await self.send_reply(job.reply_target, messages)

        self.event_log.command(
            user_id=job.user_id,
            input_text=job.command,
            expanded=job.expanded,
            exit_code=result.exit_code,
            duration=result.duration_seconds,
            timed_out=result.timed_out,
            interrupted=result.interrupted,
            truncated=result.truncated,
            auto_reset=auto_reset,
            recovery_failed=result.recovery_failed,
            capture_capped=result.capture_capped,
        )

    # -------- out-of-band control --------

    async def request_interrupt(self, user_id: int) -> str:
        """Handle /interrupt. Returns reply text."""
        if self.state == State.RUNNING:
            self.shell.send_intr()
            self.state = State.INTERRUPTING
            self._mark_activity()
            self.event_log.log(
                type="interrupt", had_running_command=True,
                user_id=user_id,
            )
            return "Interrupt sent."
        if self.state == State.INTERRUPTING:
            return "Interrupt already in progress."
        if self.state == State.RESETTING:
            return "Reset in progress."
        self.event_log.log(
                type="interrupt", had_running_command=False,
                user_id=user_id,
            )
        return "No command is currently running."

    async def request_reset(self, user_id: int) -> str:
        """Handle /reset. Returns reply text."""
        if self.state == State.RESETTING:
            return "Reset already in progress."

        # If a command is running, wake it up first
        if self.state in (State.RUNNING, State.INTERRUPTING):
            self.shell.send_intr()

        self.event_log.log(
            type="reset", trigger="user",
            user_id=user_id,
        )

        prev_state = self.state
        # Acquire the execution lock: waits for any current command to finish.
        async with self.execution_lock:
            self.state = State.RESETTING
            try:
                await self.shell.reset()
                self._mark_shell_reset()
                self.state = State.IDLE
            except Exception as e:
                self.state = State.ERROR
                self.event_log.log(type="reset_failed", error=str(e))
                return f"Reset failed: {e}. Use /reset to retry."

        notice = "Shell reset."
        if prev_state != State.IDLE:
            notice += " Previous command was terminated."
        notice += " Commands queued before the reset will be skipped."
        return notice


# -------------------- Telegram glue --------------------


class Bot:
    def __init__(
        self,
        config: AppConfig,
        auth: AuthConfig,
        shell: ShellSession,
        registry: CommandRegistry,
        event_log: EventLogger,
    ):
        self.config = config
        self.auth = auth
        self.shell = shell
        self.registry = registry
        self.event_log = event_log
        self.app: Application | None = None
        self.coordinator: Coordinator | None = None

    # -------- message send --------

    async def _send_messages(self, reply_target: int, messages: list[str]) -> None:
        assert self.app is not None
        delay = self.config.chunk_delay_ms / 1000.0
        for i, msg in enumerate(messages):
            if i > 0:
                await asyncio.sleep(delay)
            try:
                await self.app.bot.send_message(
                    chat_id=reply_target,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.exception("send_message failed, retrying as plain text")
                # Fallback: send the exact text without Markdown parsing.
                try:
                    await self.app.bot.send_message(
                        chat_id=reply_target,
                        text=msg,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logging.exception("send_message plain fallback failed")

    async def _send_error(self, reply_target: int, text: str) -> None:
        await self._send_messages(reply_target, [build_error_message(text)])

    # -------- auth --------

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return False
        if user.id != self.auth.user_id:
            self.event_log.unauthorized(user_id=user.id)
            return False
        return True

    # -------- handlers --------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and update.effective_user and update.message
        text = (update.message.text or "").strip()
        if not text:
            return
        await self._submit_command(
            update.effective_chat.id,
            update.effective_user.id,
            update.message.message_id,
            original=text,
            expanded=text,
        )

    async def on_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and update.effective_user and update.message
        text = update.message.text or ""
        # text looks like "/foo arg1 arg2"
        parts = text.split(None, 1)
        if not parts:
            return
        name = parts[0].lstrip("/")
        # Strip bot mention suffix (e.g., /foo@MyBot)
        if "@" in name:
            name = name.split("@", 1)[0]
        args = parts[1] if len(parts) > 1 else ""

        if name in BUILTIN_NAMES:
            # Built-ins are handled by dedicated CommandHandler; this path
            # shouldn't fire for them, but guard anyway.
            return

        shortcut = self.registry.get(name)
        if shortcut is None:
            await self._send_error(
                update.effective_chat.id,
                f"Unknown command: /{name} — try /help",
            )
            return

        try:
            expanded = self.registry.expand(name, args)
        except MissingArgsError:
            example = "<args>"
            await self._send_error(
                update.effective_chat.id,
                f"/{name} requires text — e.g. /{name} {example}",
            )
            return

        await self._submit_command(
            update.effective_chat.id,
            update.effective_user.id,
            update.message.message_id,
            original=f"/{name} {args}".strip(),
            expanded=expanded,
        )

    async def _submit_command(
        self,
        reply_target: int,
        user_id: int,
        message_id: int,
        *,
        original: str,
        expanded: str,
    ) -> None:
        assert self.coordinator is not None
        job = Job(
            command=original,
            expanded=expanded,
            reply_target=reply_target,
            user_id=user_id,
            message_id=message_id,
            generation=self.coordinator.current_generation(),
        )
        ok = await self.coordinator.try_enqueue(job)
        if not ok:
            await self._send_error(
                reply_target,
                f"Queue full ({self.config.max_pending} pending). Try /interrupt or wait.",
            )

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat
        lines: list[str] = [
            "Telegram Shell Bot",
            "",
            "Send any text to run it in a persistent shell.",
            "",
            "Built-in commands:",
            "  /help — this help",
            "  /status — shell + queue state",
            "  /pwd — print working directory",
            "  /interrupt — send Ctrl-C to running command",
            "  /reset — restart the shell",
        ]
        shortcuts = self.registry.names()
        if shortcuts:
            lines.append("")
            lines.append("Shortcuts:")
            for name in shortcuts:
                sc = self.registry.get(name)
                if sc and sc.template:
                    hint = sc.template
                else:
                    hint = "(script)"
                lines.append(f"  /{name} — {hint}")
        text = "\n".join(lines)
        await self._send_messages(
            update.effective_chat.id, [build_error_message(text)]
        )

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and self.coordinator is not None
        alive = self.shell.process_alive()
        state = self.coordinator.state.name.lower()
        depth = self.coordinator.queue_depth()
        log_size = self.event_log.total_size_bytes()
        log_budget = self.event_log.retention_budget_bytes()
        max_rss = _get_process_max_rss_bytes()
        idle_reset_after = (
            f"{int(self.config.idle_reset_after_seconds)}s"
            if self.config.idle_reset_after_seconds > 0
            else "disabled"
        )
        lines = [
            "Status:",
            f"  shell_alive: {alive}",
            f"  state: {state}",
            f"  shell_generation: {self.coordinator.current_generation()}",
            f"  queue_depth: {depth}/{self.config.max_pending}",
            f"  uptime: {_format_seconds(self.coordinator.uptime_seconds())}",
            f"  idle_for: {_format_seconds(self.coordinator.idle_seconds())}",
            f"  shell_age: {_format_seconds(self.coordinator.shell_age_seconds())}",
            f"  timeout: {int(self.config.timeout_seconds)}s",
            f"  max_output: {self.config.max_output_chars} chars",
            f"  max_capture: {self.config.max_capture_chars} chars",
            f"  idle_reset_after: {idle_reset_after}",
            f"  logs_size: {_format_bytes(log_size)} / {_format_bytes(log_budget)}",
        ]
        if max_rss is not None:
            lines.append(f"  max_rss: {_format_bytes(max_rss)}")
        await self._send_messages(
            update.effective_chat.id, [build_error_message("\n".join(lines))]
        )

    async def on_pwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and update.effective_user and update.message
        await self._submit_command(
            update.effective_chat.id,
            update.effective_user.id,
            update.message.message_id,
            original="/pwd",
            expanded="pwd",
        )

    async def on_interrupt(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and update.effective_user and self.coordinator is not None
        text = await self.coordinator.request_interrupt(
            update.effective_user.id
        )
        await self._send_messages(
            update.effective_chat.id, [build_error_message(text)]
        )

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        assert update.effective_chat and update.effective_user and self.coordinator is not None
        text = await self.coordinator.request_reset(
            update.effective_user.id
        )
        await self._send_messages(
            update.effective_chat.id, [build_error_message(text)]
        )

    # -------- lifecycle --------

    async def setup(self, token: str) -> None:
        await self.shell.start()

        self.app = ApplicationBuilder().token(token).build()
        self.coordinator = Coordinator(
            shell=self.shell,
            config=self.config,
            event_log=self.event_log,
            send_reply=self._send_messages,
        )

        # Register handlers
        self.app.add_handler(CommandHandler("help", self.on_help))
        self.app.add_handler(CommandHandler("status", self.on_status))
        self.app.add_handler(CommandHandler("pwd", self.on_pwd))
        self.app.add_handler(CommandHandler("interrupt", self.on_interrupt))
        self.app.add_handler(CommandHandler("reset", self.on_reset))
        # Any other slash command -> shortcut lookup
        self.app.add_handler(
            MessageHandler(filters.COMMAND, self.on_unknown_command)
        )
        # Plain text -> shell
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )

    async def run(self) -> None:
        assert self.app is not None and self.coordinator is not None
        self.event_log.startup(
            shell=self.config.shell_program,
            cwd=self.config.shell_cwd,
        )
        self.coordinator.start()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        # Park here until cancelled
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await stop_event.wait()

    async def shutdown(self) -> None:
        self.event_log.shutdown()
        if self.app is not None:
            try:
                if self.app.updater:
                    await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                logging.exception("app shutdown error")
        if self.coordinator is not None:
            await self.coordinator.stop()
        await self.shell.stop()


# -------------------- main --------------------


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet python-telegram-bot's httpx chatter
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}")

    token, auth = load_auth()

    event_log = EventLogger(
        LOG_DIR,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )

    shell = ShellSession(
        program=config.shell_program,
        cwd=config.shell_cwd,
        probe_timeout_seconds=config.probe_timeout_seconds,
        max_output_chars=config.max_output_chars,
        max_capture_chars=config.max_capture_chars,
    )
    registry = CommandRegistry(config.shortcuts)
    bot = Bot(
        config=config,
        auth=auth,
        shell=shell,
        registry=registry,
        event_log=event_log,
    )
    try:
        await bot.setup(token)
        await bot.run()
    finally:
        await bot.shutdown()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
