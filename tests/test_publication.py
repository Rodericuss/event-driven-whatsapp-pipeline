from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.publication import (
    PublicationDeliveryError,
    PublicationError,
    publish_to_group,
    publish_to_personal_chat,
)


class PersonalPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        self.import_id = "1ded194e-0c9a-4e6a-99e3-547accae7553"
        self.chat_id = "5500000000000"
        self.package = self.root / "anuncios" / "pendentes" / self.import_id
        (self.package / "fotos").mkdir(parents=True)
        (self.package / "fotos" / "001.jpg").write_bytes(b"first")
        (self.package / "fotos" / "002.jpg").write_bytes(b"second")
        self._write_settings(enabled=False)
        (self.package / "status.json").write_text(
            json.dumps(
                {
                    "status": "ready_for_review",
                    "product_id": 224,
                    "registered": True,
                    "images_uploaded": True,
                    "published": False,
                }
            ),
            encoding="utf-8",
        )
        (self.package / "metadata.json").write_text(
            json.dumps(
                {
                    "media": [
                        {
                            "sequence": 1,
                            "filename": "001.jpg",
                            "sha256": self._sha256(b"first"),
                        },
                        {
                            "sequence": 2,
                            "filename": "002.jpg",
                            "sha256": self._sha256(b"second"),
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.package / "marketplace-live-response.json").write_text(
            json.dumps(
                {
                    "finalize": {
                        "data": {
                            "product_id": 224,
                            "visible": False,
                            "publication": {
                                "text": "Official product publication",
                                "images": ["https://example/1.jpg", "https://example/2.jpg"],
                                "published": False,
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_is_disabled_by_default(self) -> None:
        with self.assertRaises(PublicationError):
            publish_to_personal_chat(
                self.root,
                self.import_id,
                approval=self._approval(),
                runner=lambda _params: self.fail("runner must not be called"),
            )

    def test_sends_one_album_with_caption_and_replays_without_duplicates(self) -> None:
        self._write_settings(enabled=True)
        calls: list[dict] = []

        def runner(params):
            calls.append(params)
            return {"messageId": "album-message", "deliveryMode": "native_album"}

        result = publish_to_personal_chat(
            self.root,
            self.import_id,
            approval=self._approval(),
            runner=runner,
            now=datetime(2026, 7, 19, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(["album-message"], result["message_ids"])
        self.assertEqual(1, len(calls))
        params = calls[0]
        self.assertEqual("Official product publication", params["message"])
        self.assertEqual(2, len(params["mediaUrls"]))
        self.assertEqual(self.import_id, params["importId"])
        self.assertEqual(self.chat_id, params["chatId"])
        self.assertEqual(self._approval(), params["approval"])
        self.assertEqual("native_album", result["delivery_mode"])
        self.assertTrue(result["personal_test_published"])
        self.assertFalse(result["published_to_group"])

        replay = publish_to_personal_chat(
            self.root,
            self.import_id,
            approval=self._approval(),
            runner=lambda _params: self.fail("complete publication must not resend"),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["message_ids"], replay["message_ids"])

        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["personal_test_published"])
        self.assertEqual("native_album", status["personal_test_delivery_mode"])
        self.assertFalse(status["published"])

    def test_sends_single_image_without_requiring_an_album(self) -> None:
        self._write_settings(enabled=True)
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata["media"] = metadata["media"][:1]
        (self.package / "metadata.json").write_text(json.dumps(metadata))
        live = json.loads(
            (self.package / "marketplace-live-response.json").read_text()
        )
        live["finalize"]["data"]["publication"]["images"] = [
            "https://example/1.jpg"
        ]
        (self.package / "marketplace-live-response.json").write_text(
            json.dumps(live)
        )

        captured = {}

        def runner(params):
            captured.update(params)
            return {"messageId": "single-image-message"}

        result = publish_to_personal_chat(
            self.root,
            self.import_id,
            approval=self._approval(),
            runner=runner,
        )

        self.assertEqual(1, len(captured["mediaUrls"]))
        self.assertEqual("single_media", result["delivery_mode"])

    def test_safe_failure_retries_the_same_native_album(self) -> None:
        self._write_settings(enabled=True)
        first_params = None

        def first_runner(params):
            nonlocal first_params
            first_params = params
            raise PublicationDeliveryError("not sent", uncertain=False)

        with self.assertRaises(PublicationDeliveryError):
            publish_to_personal_chat(
                self.root,
                self.import_id,
                approval=self._approval(),
                runner=first_runner,
            )

        resumed_params = None

        def resumed_runner(params):
            nonlocal resumed_params
            resumed_params = params
            return {"messageId": "resumed-album"}

        result = publish_to_personal_chat(
            self.root,
            self.import_id,
            approval=self._approval(),
            runner=resumed_runner,
        )

        self.assertEqual(["resumed-album"], result["message_ids"])
        self.assertEqual(first_params, resumed_params)
        audit = json.loads(
            (self.package / "whatsapp-personal-album-publication.json").read_text()
        )
        self.assertEqual(
            f"romildonegocios:personal-native-album:{self.import_id}:{self.chat_id}",
            audit["idempotency_key"],
        )
        self.assertNotIn("error", audit["operation"])

    def test_uncertain_delivery_blocks_automatic_retry(self) -> None:
        self._write_settings(enabled=True)

        with self.assertRaises(PublicationDeliveryError):
            publish_to_personal_chat(
                self.root,
                self.import_id,
                approval=self._approval(),
                runner=lambda _params: (_ for _ in ()).throw(
                    PublicationDeliveryError("unknown delivery", uncertain=True)
                ),
            )

        with self.assertRaises(PublicationError):
            publish_to_personal_chat(
                self.root,
                self.import_id,
                approval=self._approval(),
                runner=lambda _params: self.fail("uncertain message must not resend"),
            )

    def test_requires_exact_personal_chat_approval(self) -> None:
        self._write_settings(enabled=True)

        with self.assertRaises(PublicationError):
            publish_to_personal_chat(
                self.root,
                self.import_id,
                approval="PUBLISH",
                runner=lambda _params: self.fail("runner must not be called"),
            )

    def test_group_publication_uses_only_the_configured_group_and_is_idempotent(self) -> None:
        group_jid = "100000000000000002@g.us"
        self._write_group_settings(enabled=True, group_jid=group_jid)
        calls: list[dict] = []

        def runner(params):
            calls.append(params)
            return {"messageId": "group-album-message", "toJid": group_jid}

        approval = f"PUBLISH_GROUP:{self.import_id}:{group_jid}"
        result = publish_to_group(
            self.root,
            self.import_id,
            approval=approval,
            runner=runner,
            now=datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(group_jid, calls[0]["groupJid"])
        self.assertEqual("GRUPO DE PUBLICAÇÃO EXEMPLO", calls[0]["groupName"])
        self.assertEqual(approval, calls[0]["approval"])
        self.assertEqual(["group-album-message"], result["message_ids"])
        self.assertTrue(result["published_to_group"])
        self.assertFalse(result["personal_test_published"])

        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["published"])
        self.assertTrue(status["published_to_group"])
        self.assertEqual("GRUPO DE PUBLICAÇÃO EXEMPLO", status["group_publication_name"])

        replay = publish_to_group(
            self.root,
            self.import_id,
            approval=approval,
            runner=lambda _params: self.fail("complete publication must not resend"),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["message_ids"], replay["message_ids"])

    def test_group_publication_requires_exact_literal_approval(self) -> None:
        self._write_group_settings(enabled=True)
        with self.assertRaises(PublicationError):
            publish_to_group(
                self.root,
                self.import_id,
                approval="PUBLISH_GROUP",
                runner=lambda _params: self.fail("runner must not be called"),
            )

    def test_group_fallback_publishes_validated_album_without_site_link(self) -> None:
        group_jid = "100000000000000002@g.us"
        self._write_group_settings(enabled=True, group_jid=group_jid)
        self._prepare_fallback_package()
        calls: list[dict] = []

        def runner(params):
            calls.append(params)
            return {"messageId": "fallback-group-message", "toJid": group_jid}

        approval = f"PUBLISH_GROUP:{self.import_id}:{group_jid}"
        result = publish_to_group(
            self.root,
            self.import_id,
            approval=approval,
            without_site=True,
            runner=runner,
            now=datetime(2026, 7, 25, 22, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(1, len(calls))
        self.assertNotIn("http", calls[0]["message"])
        self.assertNotIn("Ver no site", calls[0]["message"])
        self.assertNotIn("Telefone", calls[0]["message"])
        self.assertIn("R$ 250.000,00", calls[0]["message"])
        self.assertEqual(2, len(calls[0]["mediaUrls"]))
        self.assertTrue(result["without_site"])
        self.assertTrue(result["published_to_group"])
        self.assertIsNone(result["product_id"])

        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["published"])
        self.assertTrue(status["published_to_group"])
        self.assertTrue(status["site_registration_pending"])
        self.assertTrue(status["publication_without_site"])
        self.assertFalse(status["registered"])

        replay = publish_to_group(
            self.root,
            self.import_id,
            approval=approval,
            without_site=True,
            runner=lambda _params: self.fail("complete fallback must not resend"),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["message_ids"], replay["message_ids"])

    def test_group_fallback_rejects_package_with_partial_site_registration(self) -> None:
        group_jid = "100000000000000002@g.us"
        self._write_group_settings(enabled=True, group_jid=group_jid)
        self._prepare_fallback_package()
        status = json.loads((self.package / "status.json").read_text())
        status["product_id"] = 999
        status["registered"] = True
        (self.package / "status.json").write_text(json.dumps(status))

        with self.assertRaises(PublicationError):
            publish_to_group(
                self.root,
                self.import_id,
                approval=(
                    f"PUBLISH_GROUP:{self.import_id}:{group_jid}"
                ),
                without_site=True,
                runner=lambda _params: self.fail("partial product must block fallback"),
            )

    def test_group_publication_blocks_retry_after_uncertain_delivery(self) -> None:
        group_jid = "100000000000000002@g.us"
        self._write_group_settings(enabled=True, group_jid=group_jid)
        approval = f"PUBLISH_GROUP:{self.import_id}:{group_jid}"
        with self.assertRaises(PublicationDeliveryError):
            publish_to_group(
                self.root,
                self.import_id,
                approval=approval,
                runner=lambda _params: (_ for _ in ()).throw(
                    PublicationDeliveryError("unknown delivery", uncertain=True)
                ),
            )
        with self.assertRaises(PublicationError):
            publish_to_group(
                self.root,
                self.import_id,
                approval=approval,
                runner=lambda _params: self.fail("uncertain message must not resend"),
            )

    def _write_settings(self, *, enabled: bool) -> None:
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "allowed_chat_ids": [self.chat_id],
                    "personal_publication": {
                        "enabled": enabled,
                        "channel": "whatsapp",
                    },
                }
            ),
            encoding="utf-8",
        )

    def _write_group_settings(
        self,
        *,
        enabled: bool,
        group_jid: str = "100000000000000002@g.us",
    ) -> None:
        live = json.loads(
            (self.package / "marketplace-live-response.json").read_text()
        )
        live["finalize"]["data"]["visible"] = True
        (self.package / "marketplace-live-response.json").write_text(
            json.dumps(live), encoding="utf-8"
        )
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "allowed_chat_ids": [self.chat_id],
                    "personal_publication": {
                        "enabled": False,
                        "channel": "whatsapp",
                    },
                    "group_publication": {
                        "enabled": enabled,
                        "channel": "whatsapp",
                        "group_name": "GRUPO DE PUBLICAÇÃO EXEMPLO",
                        "group_jid": group_jid,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _approval(self) -> str:
        return f"PUBLISH_PERSONAL:{self.import_id}:{self.chat_id}"

    def _prepare_fallback_package(self) -> None:
        (self.package / "status.json").write_text(
            json.dumps(
                {
                    "status": "ready_for_review",
                    "validated": True,
                    "extracted": True,
                    "registered": False,
                    "images_uploaded": False,
                    "published": False,
                }
            ),
            encoding="utf-8",
        )
        (self.package / "anuncio-extraido.json").write_text(
            json.dumps(
                {
                    "title": "Escavadeira Hidráulica Hyundai R220Lc 2015",
                    "price_in_cents": 25_000_000,
                    "description": "Máquina trabalhando, toda operacional.",
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _sha256(contents: bytes) -> str:
        import hashlib

        return hashlib.sha256(contents).hexdigest()


if __name__ == "__main__":
    unittest.main()
