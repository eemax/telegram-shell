import unittest

from shell_session import ShellSession


class ShellSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_cap_limits_retained_output(self) -> None:
        shell = ShellSession(
            program="/bin/zsh",
            cwd="/tmp",
            max_output_chars=1000,
            max_capture_chars=200,
        )
        await shell.start()
        try:
            result = await shell.run_command(
                "i=0; while [ $i -lt 1000 ]; do printf x; i=$((i+1)); done",
                timeout_seconds=5,
            )
        finally:
            await shell.stop()

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.capture_capped)
        self.assertTrue(result.truncated)
        self.assertIn("capture capped", result.output)
