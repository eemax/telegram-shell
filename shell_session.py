"""Persistent pexpect-backed PTY shell session."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass

import pexpect

ANSI_ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]')


@dataclass
class CommandResult:
    command: str
    expanded: str
    output: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    interrupted: bool = False
    truncated: bool = False
    auto_reset: bool = False
    recovery_failed: bool = False
    capture_capped: bool = False


def clean_output(raw: str) -> str:
    """Normalize PTY output for Telegram delivery."""
    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    text = ANSI_ESCAPE.sub('', text)
    return text.strip()


class ShellSession:
    """Wraps a single long-lived pexpect PTY shell."""

    def __init__(
        self,
        program: str,
        cwd: str,
        probe_timeout_seconds: float = 5.0,
        max_output_chars: int = 12000,
        max_capture_chars: int = 200000,
    ):
        self.program = program
        self.cwd = cwd
        self.probe_timeout_seconds = probe_timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_capture_chars = max_capture_chars
        self._child: pexpect.spawn | None = None
        self._lock = asyncio.Lock()
        self._last_probe_ok: bool = False

    # ---------- lifecycle ----------

    async def start(self) -> None:
        await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> None:
        env = os.environ.copy()
        env['TERM'] = 'dumb'
        env['PAGER'] = 'cat'
        env['GIT_PAGER'] = 'cat'
        env['LESS'] = '-FRX'
        env['HISTFILE'] = '/dev/null'
        env['HISTSIZE'] = '0'
        env['SAVEHIST'] = '0'

        self._child = pexpect.spawn(
            self.program,
            cwd=self.cwd,
            env=env,
            encoding='utf-8',
            codec_errors='replace',
            dimensions=(40, 1000),
            timeout=self.probe_timeout_seconds,
        )
        # Disable PTY echo so the command doesn't appear in our output
        try:
            self._child.setecho(False)
            self._child.waitnoecho(timeout=2)
        except Exception:
            pass

        # Quiet zsh prompt filler, line editor noise, and the missing-EOL marker.
        # PROMPT_SP / PROMPT_CR cause the `%<spaces>` filler between commands.
        init = (
            "unsetopt zle promptcr promptsp promptsubst "
            "sharehistory incappendhistory appendhistory extendedhistory "
            "banghist 2>/dev/null; "
            "HISTFILE=/dev/null; HISTSIZE=0; SAVEHIST=0; "
            "export HISTFILE HISTSIZE SAVEHIST; "
            "fc -p /dev/null 2>/dev/null; "
            "PS1=''; PS2=''; PS3=''; PS4=''; "
            "PROMPT=''; RPROMPT=''; PROMPT2=''; PROMPT3=''; PROMPT4=''; "
            "PROMPT_EOL_MARK=''; "
            "stty -echo 2>/dev/null; true\n"
        )
        self._child.send(init)

        # Sync using a readiness sentinel
        ready_token = f"__TGSH_READY_{secrets.token_hex(4)}__"
        self._child.send(f"printf '%s\\n' '{ready_token}'\n")
        self._child.expect_exact(ready_token, timeout=5)
        # Drain any prompt residue that arrives just after the sentinel.
        self._drain_prompt_residue()
        self._last_probe_ok = True

    def _drain_prompt_residue(self) -> None:
        """Best-effort: swallow any prompt characters zsh prints after the
        last matched marker (e.g. between commands)."""
        if self._child is None:
            return
        try:
            self._child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=0.05)
        except Exception:
            pass

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_sync)

    def _stop_sync(self) -> None:
        if self._child is not None:
            try:
                self._child.close(force=True)
            except Exception:
                pass
            self._child = None
            self._last_probe_ok = False

    async def reset(self) -> None:
        await self.stop()
        await self.start()

    # ---------- health ----------

    def process_alive(self) -> bool:
        return self._child is not None and self._child.isalive()

    async def probe(self) -> bool:
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> bool:
        if self._child is None or not self._child.isalive():
            self._last_probe_ok = False
            return False
        token = f"__TGSH_PROBE_{secrets.token_hex(4)}__"
        try:
            self._child.send(f"printf '%s\\n' '{token}'\n")
            self._child.expect_exact(token, timeout=self.probe_timeout_seconds)
            self._last_probe_ok = True
            return True
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            self._last_probe_ok = False
            return False

    def is_alive(self) -> bool:
        return self.process_alive() and self._last_probe_ok

    # ---------- execution ----------

    async def run_command(
        self, command: str, timeout_seconds: float
    ) -> CommandResult:
        return await asyncio.to_thread(
            self._run_command_sync, command, timeout_seconds
        )

    def _run_command_sync(
        self, command: str, timeout_seconds: float
    ) -> CommandResult:
        if self._child is None or not self._child.isalive():
            return CommandResult(
                command=command,
                expanded=command,
                output="(shell not running)",
                exit_code=None,
                duration_seconds=0.0,
                timed_out=False,
                interrupted=False,
            )

        token = f"__TGSH_EXIT_{secrets.token_hex(6)}__"
        # Use a newline to terminate the user command, then emit exit marker.
        # Using a braced subshell-like pattern keeps exit code capture simple.
        wrapped = (
            f"{command}\n"
            f"__tgsh_ec=$?; printf '\\n%s:%s\\n' '{token}' \"$__tgsh_ec\"\n"
        )

        marker_re = re.escape(token) + r':(-?\d+)'
        marker_pattern = re.compile(marker_re)
        marker_tail_keep = len(token) + 32
        start = time.monotonic()
        timed_out = False
        interrupted = False
        capture_capped = False
        marker_found = False
        exit_code: int | None = None
        raw_parts: list[str] = []
        raw_len = 0
        pending = ""

        def append_captured(text: str) -> None:
            nonlocal raw_len, capture_capped
            if not text:
                return
            remaining = self.max_capture_chars - raw_len
            if remaining <= 0:
                capture_capped = True
                return
            if len(text) <= remaining:
                raw_parts.append(text)
                raw_len += len(text)
                return
            raw_parts.append(text[:remaining])
            raw_len += remaining
            capture_capped = True

        try:
            self._child.send(wrapped)
        except (OSError, pexpect.ExceptionPexpect):
            return CommandResult(
                command=command,
                expanded=command,
                output="(failed to send command to shell)",
                exit_code=None,
                duration_seconds=time.monotonic() - start,
            )

        deadline = start + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = self._child.read_nonblocking(
                    size=4096,
                    timeout=min(0.2, remaining),
                )
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                append_captured(pending)
                pending = ""
                duration = time.monotonic() - start
                output = clean_output(''.join(raw_parts))
                if capture_capped:
                    output = (
                        f"{output}\n[capture capped at {self.max_capture_chars} chars]"
                        if output
                        else f"[capture capped at {self.max_capture_chars} chars]"
                    )
                return CommandResult(
                    command=command,
                    expanded=command,
                    output=f"{output}\n(shell closed)" if output else "(shell closed)",
                    exit_code=None,
                    duration_seconds=duration,
                    capture_capped=capture_capped,
                    truncated=capture_capped,
                )

            pending += chunk
            match = marker_pattern.search(pending)
            if match:
                append_captured(pending[:match.start()])
                exit_code = int(match.group(1))
                marker_found = True
                break

            flush_upto = max(0, len(pending) - marker_tail_keep)
            if flush_upto:
                append_captured(pending[:flush_upto])
                pending = pending[flush_upto:]

        if not marker_found:
            append_captured(pending)

        duration = time.monotonic() - start

        if timed_out:
            # Capture whatever we got so far, send interrupt
            try:
                self._child.sendintr()
            except Exception:
                pass
            interrupted = True
        else:
            # Drain any trailing prompt residue so it doesn't land in the
            # next command's output.
            self._drain_prompt_residue()

        output = clean_output(''.join(raw_parts))
        if capture_capped:
            cap_notice = f"[capture capped at {self.max_capture_chars} chars]"
            output = f"{output}\n{cap_notice}" if output else cap_notice

        truncated = capture_capped
        if len(output) > self.max_output_chars:
            output = output[: self.max_output_chars] + f"\n[truncated to {self.max_output_chars} chars]"
            truncated = True

        return CommandResult(
            command=command,
            expanded=command,
            output=output,
            exit_code=exit_code,
            duration_seconds=duration,
            timed_out=timed_out,
            interrupted=interrupted,
            truncated=truncated,
            capture_capped=capture_capped,
        )

    # ---------- interrupt ----------

    async def interrupt(self) -> bool:
        """Send Ctrl-C and probe for recovery. Returns True if shell recovered."""
        return await asyncio.to_thread(self._interrupt_sync)

    def _interrupt_sync(self) -> bool:
        if self._child is None or not self._child.isalive():
            return False
        try:
            self._child.sendintr()
        except Exception:
            return False
        # Drain any pending output briefly, then probe
        try:
            self._child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=0.3)
        except Exception:
            pass
        return self._probe_sync()

    def send_intr(self) -> None:
        """Fire-and-forget Ctrl-C (used for out-of-band interrupt)."""
        if self._child is not None and self._child.isalive():
            try:
                self._child.sendintr()
            except Exception:
                pass
