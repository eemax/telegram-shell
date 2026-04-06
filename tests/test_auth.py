import os
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class LoadAuthTests(unittest.TestCase):
    def test_load_auth_accepts_one_allowed_user_id(self) -> None:
        with patch.object(main, "ENV_PATH", Path("/tmp/telegram-shell-missing.env")):
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "ALLOWED_USER_ID": "12345",
                },
                clear=True,
            ):
                token, auth = main.load_auth()

        self.assertEqual(token, "token")
        self.assertEqual(auth.user_id, 12345)

    def test_load_auth_rejects_legacy_variables(self) -> None:
        with patch.object(main, "ENV_PATH", Path("/tmp/telegram-shell-missing.env")):
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "ALLOWED_USER_IDS": "12345",
                },
                clear=True,
            ):
                with self.assertRaisesRegex(SystemExit, "ALLOWED_USER_ID"):
                    main.load_auth()
