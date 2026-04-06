import unittest

from command_registry import AppConfig
from main import Coordinator, Job, State
from shell_session import CommandResult


def make_config() -> AppConfig:
    return AppConfig(
        shell_program="/bin/zsh",
        shell_cwd="/tmp",
        timeout_seconds=120,
        max_output_chars=12000,
        max_capture_chars=200000,
        probe_timeout_seconds=5,
        chunk_size=3500,
        chunk_min=2500,
        chunk_delay_ms=0,
        max_pending=5,
        log_max_bytes=1024 * 1024,
        log_backup_count=3,
        idle_reset_after_seconds=0,
        shortcuts={},
    )


class _RecordingLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def log(self, **fields) -> None:
        self.entries.append(fields)

    def command(self, **fields) -> None:
        self.entries.append(fields)


class _TimeoutRecoveryFailureShell:
    def __init__(self) -> None:
        self.probe_calls = 0

    def process_alive(self) -> bool:
        return True

    async def probe(self) -> bool:
        self.probe_calls += 1
        return self.probe_calls == 1

    async def run_command(self, command: str, timeout_seconds: float) -> CommandResult:
        return CommandResult(
            command=command,
            expanded=command,
            output="partial",
            exit_code=None,
            duration_seconds=3.0,
            timed_out=True,
            interrupted=True,
        )

    async def reset(self) -> None:
        raise RuntimeError("reset failed")

    def send_intr(self) -> None:
        pass


class _ResettableShell:
    def __init__(self) -> None:
        self.run_calls = 0
        self.reset_calls = 0

    def process_alive(self) -> bool:
        return True

    async def probe(self) -> bool:
        return True

    async def run_command(self, command: str, timeout_seconds: float) -> CommandResult:
        self.run_calls += 1
        return CommandResult(
            command=command,
            expanded=command,
            output="ok",
            exit_code=0,
            duration_seconds=0.1,
        )

    async def reset(self) -> None:
        self.reset_calls += 1

    def send_intr(self) -> None:
        pass


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_recovery_failure_keeps_error_state(self) -> None:
        shell = _TimeoutRecoveryFailureShell()
        log = _RecordingLog()
        replies: list[str] = []

        async def send_reply(reply_target: int, messages: list[str]) -> None:
            replies.extend(messages)

        coord = Coordinator(shell, make_config(), log, send_reply)

        await coord._handle_command(
            Job(
                command="sleep 999",
                expanded="sleep 999",
                reply_target=1,
                user_id=1,
                message_id=1,
                generation=coord.current_generation(),
            )
        )

        self.assertEqual(coord.state, State.ERROR)
        self.assertEqual(len(replies), 1)
        self.assertIn("shell recovery failed", replies[0].lower())

    async def test_reset_invalidates_queued_jobs_from_old_generation(self) -> None:
        shell = _ResettableShell()
        log = _RecordingLog()
        replies: list[str] = []

        async def send_reply(reply_target: int, messages: list[str]) -> None:
            replies.extend(messages)

        coord = Coordinator(shell, make_config(), log, send_reply)
        old_job = Job(
            command="pwd",
            expanded="pwd",
            reply_target=1,
            user_id=1,
            message_id=1,
            generation=coord.current_generation(),
        )

        notice = await coord.request_reset(user_id=1)
        await coord._handle_command(old_job)

        self.assertIn("queued before the reset will be skipped", notice.lower())
        self.assertEqual(coord.current_generation(), 1)
        self.assertEqual(shell.run_calls, 0)
        self.assertEqual(len(replies), 1)
        self.assertIn("stale shell state", replies[0].lower())

    async def test_idle_reset_only_runs_when_idle_and_enabled(self) -> None:
        shell = _ResettableShell()
        log = _RecordingLog()

        async def send_reply(reply_target: int, messages: list[str]) -> None:
            return None

        config = make_config()
        config.idle_reset_after_seconds = 10
        coord = Coordinator(shell, config, log, send_reply)
        coord._last_activity_monotonic -= 11

        did_reset = await coord._maybe_idle_reset()

        self.assertTrue(did_reset)
        self.assertEqual(shell.reset_calls, 1)
        self.assertEqual(coord.current_generation(), 1)
