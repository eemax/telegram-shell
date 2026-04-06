import unittest
from types import SimpleNamespace

from main import AuthConfig, Bot
from command_registry import AppConfig


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


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("markdown failed")


class BotFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_fallback_preserves_backslashes(self) -> None:
        fake_bot = _FakeTelegramBot()
        bot = Bot(
            config=make_config(),
            auth=AuthConfig(user_id=1),
            shell=None,
            registry=None,
            event_log=None,
        )
        bot.app = SimpleNamespace(bot=fake_bot)

        message = r"C:\temp\logs\current.txt"
        await bot._send_messages(123, [message])

        self.assertEqual(fake_bot.calls[0]["text"], message)
        self.assertEqual(fake_bot.calls[1]["text"], message)
        self.assertNotIn("parse_mode", fake_bot.calls[1])
