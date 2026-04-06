import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from command_registry import ConfigError, load_config


class LoadConfigTests(unittest.TestCase):
    def _write_config(self, text: str) -> Path:
        self.tempdir = TemporaryDirectory()
        path = Path(self.tempdir.name) / "config.yaml"
        path.write_text(text)
        return path

    def tearDown(self) -> None:
        tempdir = getattr(self, "tempdir", None)
        if tempdir is not None:
            tempdir.cleanup()

    def test_root_must_be_mapping(self) -> None:
        path = self._write_config("- not-a-mapping\n")

        with self.assertRaisesRegex(ConfigError, "Config root must be a mapping"):
            load_config(path)

    def test_chunk_min_must_not_exceed_chunk_size(self) -> None:
        path = self._write_config(
            "telegram:\n"
            "  message_chunk_size: 100\n"
            "  message_chunk_min: 101\n"
        )

        with self.assertRaisesRegex(
            ConfigError, "telegram.message_chunk_min must be <="
        ):
            load_config(path)

    def test_shortcut_template_must_be_a_string(self) -> None:
        path = self._write_config(
            "commands:\n"
            "  bad:\n"
            "    template: 123\n"
        )

        with self.assertRaisesRegex(ConfigError, "bad.template must be a string"):
            load_config(path)

    def test_capture_limit_must_not_be_smaller_than_output_limit(self) -> None:
        path = self._write_config(
            "shell:\n"
            "  max_output_chars: 1000\n"
            "  max_capture_chars: 999\n"
        )

        with self.assertRaisesRegex(
            ConfigError, "shell.max_capture_chars must be >="
        ):
            load_config(path)

    def test_logging_and_maintenance_settings_load(self) -> None:
        path = self._write_config(
            "logging:\n"
            "  max_bytes: 2048\n"
            "  backup_count: 2\n"
            "maintenance:\n"
            "  idle_reset_after_seconds: 60\n"
        )

        config = load_config(path)

        self.assertEqual(config.log_max_bytes, 2048)
        self.assertEqual(config.log_backup_count, 2)
        self.assertEqual(config.idle_reset_after_seconds, 60.0)
