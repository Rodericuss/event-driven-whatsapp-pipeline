from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.config import ConfigurationError, load_settings


class ConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "settings.example.json").write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "allowed_chat_ids": [],
                    "allowed_media_roots": [],
                    "group_intake": {"enabled": False},
                    "extraction_model": {},
                    "marketplace_api": {"enabled": False},
                    "personal_publication": {"enabled": False},
                    "group_publication": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_safe_example_is_the_last_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.root)
        self.assertTrue(settings["dry_run"])
        self.assertFalse(settings["group_intake"]["enabled"])
        self.assertFalse(settings["group_publication"]["enabled"])

    def test_local_settings_take_precedence_over_example(self) -> None:
        (self.root / "config" / "settings.local.json").write_text(
            json.dumps({"dry_run": False}), encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.root)
        self.assertFalse(settings["dry_run"])

    def test_dotenv_overrides_nested_destinations(self) -> None:
        (self.root / ".env").write_text(
            "\n".join(
                (
                    "DRY_RUN=false",
                    "OPENCLAW_PERSONAL_CHAT_ID=5500000000000",
                    "SOURCE_GROUP_ENABLED=true",
                    "SOURCE_GROUP_NAME=GRUPO DE ORIGEM EXEMPLO",
                    "SOURCE_GROUP_JID=100000000000000001@g.us",
                    "APPROVAL_CHAT_ID=5500000000000",
                    "GROUP_PUBLICATION_ENABLED=true",
                    "PUBLICATION_GROUP_NAME=GRUPO DE PUBLICAÇÃO EXEMPLO",
                    "PUBLICATION_GROUP_JID=100000000000000002@g.us",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self.root)
        self.assertFalse(settings["dry_run"])
        self.assertEqual(["5500000000000"], settings["allowed_chat_ids"])
        self.assertTrue(settings["group_intake"]["enabled"])
        self.assertEqual(
            "100000000000000001@g.us", settings["group_intake"]["group_jid"]
        )
        self.assertTrue(settings["group_publication"]["enabled"])
        self.assertEqual(
            "100000000000000002@g.us",
            settings["group_publication"]["group_jid"],
        )

    def test_invalid_boolean_fails_closed(self) -> None:
        with patch.dict(os.environ, {"GROUP_PUBLICATION_ENABLED": "maybe"}, clear=True):
            with self.assertRaises(ConfigurationError):
                load_settings(self.root)


if __name__ == "__main__":
    unittest.main()
