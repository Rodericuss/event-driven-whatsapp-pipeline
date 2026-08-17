import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from whatsapp_importer.queue import (
    approved_for_live,
    publication_lock,
    requires_publication_without_site,
    resolve_fly_binary,
)


class QueueSafetyTest(unittest.TestCase):
    def test_worker_routes_the_terminal_publication_to_the_configured_group(self) -> None:
        worker = (
            Path(__file__).resolve().parents[1] / "scripts" / "queue-worker"
        ).read_text(encoding="utf-8")
        self.assertIn('settings.get("group_publication")', worker)
        self.assertIn('approval_mode = "VISIBLE" if visible is True', worker)
        self.assertIn('root / "scripts" / "publish-group"', worker)
        self.assertIn('"--without-site"', worker)
        self.assertIn('settings.get("marketplace_api")', worker)
        self.assertIn('status.get("visual_validation_approved") is not True', worker)
        self.assertIn('"Cadastro no site desativado:', worker)
        self.assertIn('"group_published_without_site"', worker)
        self.assertIn('final_status["site_registration_skipped"] = True', worker)
        self.assertIn("with publication_lock(root):", worker)
        self.assertIn('"group_published_site_pending"', worker)
        self.assertIn('final_status["status"] = "group_published"', worker)
        self.assertNotIn('root / "scripts" / "publish-personal-test"', worker)
        self.assertNotIn("INTERNAL_IMPORT_WRITES_ENABLED", worker)
        self.assertNotIn('"secrets", "set"', worker)
        self.assertIn('"awaiting_clarification"', worker)
        self.assertIn('"publication_confirmation_bypassed"', worker)

    def test_publication_lock_can_be_reentered_sequentially(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with publication_lock(root):
                self.assertTrue(
                    (root / "anuncios" / "recebendo" / ".publication.lock").is_file()
                )
            with publication_lock(root):
                pass

    def test_unmapped_machine_is_published_without_site_registration(self) -> None:
        self.assertTrue(
            requires_publication_without_site(
                {
                    "category": "maquinas",
                    "type": "Confirmar com o vendedor",
                    "seller_confirmation_required": True,
                }
            )
        )
        self.assertFalse(
            requires_publication_without_site(
                {
                    "category": "maquinas",
                    "type": "Escavadeira",
                    "seller_confirmation_required": False,
                }
            )
        )

    def test_live_flow_auto_approves_validated_listing(self) -> None:
        self.assertFalse(approved_for_live({"status": "ready_for_review"}))
        self.assertFalse(
            approved_for_live(
                {
                    "status": "review_required",
                    "validated": True,
                }
            )
        )
        self.assertTrue(
            approved_for_live(
                {
                    "status": "ready_for_review",
                    "validated": True,
                }
            )
        )
        self.assertFalse(
            approved_for_live(
                {
                    "status": "ready_for_review",
                    "validated": True,
                },
                shadow_mode=True,
            )
        )
        self.assertTrue(
            approved_for_live(
                {
                    "status": "ready_for_review",
                    "validated": True,
                    "publication_confirmed": True,
                }
            )
        )

    def test_resolves_fly_from_the_user_installation_outside_service_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fly = Path(temporary) / ".fly" / "bin" / "fly"
            fly.parent.mkdir(parents=True)
            fly.write_text("#!/bin/sh\n")
            fly.chmod(0o700)
            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
                self.assertEqual(str(fly.resolve()), resolve_fly_binary(home=Path(temporary)))

    def test_fly_bin_environment_override_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fly = Path(temporary) / "custom-fly"
            fly.write_text("#!/bin/sh\n")
            fly.chmod(0o700)
            with patch.dict(os.environ, {"FLY_BIN": str(fly)}, clear=False):
                self.assertEqual(str(fly.resolve()), resolve_fly_binary())


if __name__ == "__main__":
    unittest.main()
