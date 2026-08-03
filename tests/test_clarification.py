from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.clarification import (
    handle_clarification_event,
    mark_question_sent,
    parse_price_answer,
    prepare_clarification,
    prepare_publication_confirmation,
)


CHAT = "5500000000000"
GROUP = "100000000000000001"
GROUP_JID = f"{GROUP}@g.us"
GROUP_SENDER = "554399999999"
IMPORT_ID = "90bc01fe-324b-4c4e-993c-9b3becb1bc6e"


class ClarificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        source_config = Path(__file__).resolve().parents[1] / "config"
        (self.root / "config" / "marketplace-catalog.json").write_bytes(
            (source_config / "marketplace-catalog.json").read_bytes()
        )
        (self.root / "config" / "settings.json").write_text(
            json.dumps({"allowed_chat_ids": [CHAT]})
        )
        self.package = self.root / "anuncios" / "pendentes" / IMPORT_ID
        (self.package / "fotos").mkdir(parents=True)
        (self.package / "mensagem-original.txt").write_text(
            "Pá carregadeira Volvo L110F ano 2018, valor 240,000,00"
        )
        (self.package / "metadata.json").write_text(
            json.dumps(
                {
                    "import_id": IMPORT_ID,
                    "chat_id": CHAT,
                    "sender_id": CHAT,
                    "media_count": 8,
                }
            )
        )
        (self.package / "anuncio-extraido.json").write_text(
            json.dumps(
                {
                    "title": "Pá Carregadeira Volvo L110F 2018",
                    "year": 2018,
                    "price_in_cents": 24000000,
                    "description": "8.000 horas.",
                    "category": "maquinas",
                    "type": "Pá Carregadeira",
                    "seller_confirmation_required": False,
                    "missing_fields": [],
                    "warnings": [],
                    "confidence": 1,
                }
            )
        )
        (self.package / "status.json").write_text(
            json.dumps(
                {
                    "status": "review_required",
                    "validated": False,
                    "registered": False,
                    "published": False,
                    "errors": [
                        "price_in_cents não corresponde a um preço explícito no texto original."
                    ],
                    "warnings": [],
                }
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def event(self, message_id: str, text: str, **changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "chat_id": CHAT,
            "sender_id": CHAT,
            "message_id": message_id,
            "text": text,
            "is_group": False,
        }
        event.update(changes)
        return event

    def make_group_shadow_candidate(self) -> None:
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata.update(
            {
                "chat_id": GROUP,
                "chat_jid": GROUP_JID,
                "sender_id": GROUP_SENDER,
                "is_group": True,
                "approval_chat_id": CHAT,
                "approval_sender_id": CHAT,
                "intake_shadow_mode": True,
            }
        )
        (self.package / "metadata.json").write_text(json.dumps(metadata))
        status = json.loads((self.package / "status.json").read_text())
        status.update({"status": "ready_for_review", "validated": True})
        (self.package / "status.json").write_text(json.dumps(status))

    def test_common_price_answers_are_normalized(self) -> None:
        for answer in (
            "240.000,00",
            "240000",
            "240 mil",
            "240,000",
            "240,000,00",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(24000000, parse_price_answer(answer))

    def test_price_question_uses_existing_suggestion(self) -> None:
        clarification = prepare_clarification(self.root, IMPORT_ID)

        self.assertIsNotNone(clarification)
        self.assertEqual("price_in_cents", clarification["field"])
        self.assertIn("R$ 240.000,00", clarification["question"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual("awaiting_clarification", status["status"])

    def test_question_sent_checkpoint_prevents_restart_duplicates(self) -> None:
        prepare_clarification(self.root, IMPORT_ID)

        result = mark_question_sent(self.root, IMPORT_ID, "QUESTION-MESSAGE-ID")

        self.assertEqual("clarification_question_marked_sent", result["action"])
        clarification = json.loads((self.package / "clarification.json").read_text())
        self.assertTrue(clarification["question_sent_at"])
        self.assertEqual(
            "QUESTION-MESSAGE-ID", clarification["question_message_id"]
        )

    def test_publication_question_offers_reactions_and_text_fallback(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))

        clarification = prepare_publication_confirmation(self.root, IMPORT_ID)

        self.assertIn("👍", clarification["question"])
        self.assertIn("👎", clarification["question"])
        self.assertIn("Imagens vinculadas: 8", clarification["question"])
        self.assertIn("autorizar a publicação", clarification["question"])
        self.assertNotIn("autorizar o cadastro", clarification["question"])
        self.assertIn("PUBLICAR 90bc01fe", clarification["question"])

    def test_group_shadow_card_goes_to_personal_chat_and_cannot_publish(self) -> None:
        self.make_group_shadow_candidate()

        clarification = prepare_publication_confirmation(self.root, IMPORT_ID)

        self.assertEqual(CHAT, clarification["chat_id"])
        self.assertEqual(GROUP, clarification["source_chat_id"])
        self.assertTrue(clarification["shadow_mode"])
        self.assertIn("MODO SOMBRA", clarification["question"])
        mark_question_sent(self.root, IMPORT_ID, "SHADOW-CARD-MESSAGE-ID")
        result = handle_clarification_event(
            self.root,
            self.event(
                "SHADOW-REACTION-ID",
                "",
                reaction_target_message_id="SHADOW-CARD-MESSAGE-ID",
                reaction_action="APPROVE",
            ),
        )

        self.assertEqual("shadow_approval_recorded", result["action"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual("shadow_approval_recorded", status["status"])
        self.assertFalse(status["publication_confirmed"])
        self.assertFalse(status["published"])

    def test_confirmed_price_is_not_asked_again_for_description_error(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "review_required"
        status["errors"] = ["description ainda contém preço."]
        (self.package / "status.json").write_text(json.dumps(status))
        (self.package / "review-overrides.json").write_text(
            json.dumps(
                {
                    "confirmed_by": "user",
                    "confirmed_at": "2026-08-02T01:26:17Z",
                    "field_answers": {"price_in_cents": 24000000},
                }
            )
        )

        clarification = prepare_clarification(self.root, IMPORT_ID)

        self.assertIsNotNone(clarification)
        self.assertEqual("description", clarification["field"])
        self.assertNotIn("dúvida no preço", clarification["question"])

    def test_confirmation_records_audited_override_without_changing_source(self) -> None:
        original = (self.package / "mensagem-original.txt").read_text()
        prepare_clarification(self.root, IMPORT_ID)

        result = handle_clarification_event(
            self.root, self.event("answer-1", f"SIM {IMPORT_ID[:8]}")
        )

        self.assertEqual("clarification_recorded", result["action"])
        overrides = json.loads((self.package / "review-overrides.json").read_text())
        self.assertEqual(24000000, overrides["field_answers"]["price_in_cents"])
        self.assertEqual("user", overrides["confirmed_by"])
        self.assertEqual(original, (self.package / "mensagem-original.txt").read_text())
        self.assertEqual(
            "review_required",
            json.loads((self.package / "status.json").read_text())["status"],
        )

    def test_invalid_answer_keeps_question_pending(self) -> None:
        prepare_clarification(self.root, IMPORT_ID)

        result = handle_clarification_event(
            self.root, self.event("bad-answer", "PREÇO 90bc01fe banana")
        )

        self.assertEqual("clarification_invalid", result["action"])
        clarification = json.loads((self.package / "clarification.json").read_text())
        self.assertEqual("pending", clarification["status"])
        self.assertEqual(1, clarification["attempts"])

    def test_duplicate_answer_is_idempotent(self) -> None:
        prepare_clarification(self.root, IMPORT_ID)
        event = self.event("same-answer", "SIM 90bc01fe")
        first = handle_clarification_event(self.root, event)
        second = handle_clarification_event(self.root, event)

        self.assertEqual("clarification_recorded", first["action"])
        self.assertEqual("clarification_duplicate_ignored", second["action"])

    def test_other_chat_cannot_answer(self) -> None:
        prepare_clarification(self.root, IMPORT_ID)
        result = handle_clarification_event(
            self.root,
            self.event(
                "other-answer",
                "SIM 90bc01fe",
                chat_id="5511999999999",
                sender_id="5511999999999",
            ),
        )
        self.assertFalse(result["handled"])
        self.assertFalse((self.package / "review-overrides.json").exists())

    def test_publication_requires_exact_command_and_code(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))
        clarification = prepare_publication_confirmation(self.root, IMPORT_ID)
        self.assertIsNotNone(clarification)

        invalid = handle_clarification_event(
            self.root, self.event("publish-no-code", "pode publicar")
        )
        approved = handle_clarification_event(
            self.root, self.event("publish-code", "PUBLICAR 90bc01fe")
        )

        self.assertEqual("clarification_invalid", invalid["action"])
        self.assertEqual("publication_confirmed", approved["action"])
        final_status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(final_status["publication_confirmed"])
        self.assertEqual("ready_for_review", final_status["status"])

    def test_new_listing_bypasses_pending_publication_confirmation(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))
        prepare_publication_confirmation(self.root, IMPORT_ID)

        result = handle_clarification_event(
            self.root,
            self.event(
                "new-listing",
                "Escavadeira Caterpillar 320 ano 2019, valor 400.000,00",
            ),
        )

        self.assertFalse(result["handled"])
        self.assertEqual("not_a_clarification_answer", result["action"])

    def test_cancel_never_authorizes_publication(self) -> None:
        prepare_clarification(self.root, IMPORT_ID)
        result = handle_clarification_event(
            self.root, self.event("cancel", "CANCELAR 90bc01fe")
        )
        self.assertEqual("clarification_cancelled", result["action"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual("cancelled_by_user", status["status"])
        self.assertFalse(status["published"])

    def test_thumb_up_reaction_confirms_matching_publication_message(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))
        prepare_publication_confirmation(self.root, IMPORT_ID)
        mark_question_sent(self.root, IMPORT_ID, "QUESTION-MESSAGE-ID")

        event = self.event(
            "REACTION-EVENT-ID",
            "",
            reaction_target_message_id="QUESTION-MESSAGE-ID",
            reaction_action="APPROVE",
        )
        first = handle_clarification_event(self.root, event)
        duplicate = handle_clarification_event(self.root, event)

        self.assertEqual("publication_confirmed", first["action"])
        self.assertEqual("clarification_duplicate_ignored", duplicate["action"])
        clarification = json.loads((self.package / "clarification.json").read_text())
        self.assertEqual("reaction", clarification["answer_method"])
        self.assertEqual("👍", clarification["answer"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["publication_confirmed"])
        self.assertEqual("reaction", status["publication_confirmation_method"])

    def test_thumb_down_reaction_cancels_without_publication(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))
        prepare_publication_confirmation(self.root, IMPORT_ID)
        mark_question_sent(self.root, IMPORT_ID, "QUESTION-MESSAGE-ID")

        result = handle_clarification_event(
            self.root,
            self.event(
                "REACTION-CANCEL-ID",
                "",
                reaction_target_message_id="QUESTION-MESSAGE-ID",
                reaction_action="CANCEL",
            ),
        )

        self.assertEqual("clarification_cancelled", result["action"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual("cancelled_by_user", status["status"])
        self.assertFalse(status["published"])

    def test_reaction_to_other_message_is_ignored(self) -> None:
        status = json.loads((self.package / "status.json").read_text())
        status["status"] = "ready_for_review"
        status["validated"] = True
        (self.package / "status.json").write_text(json.dumps(status))
        prepare_publication_confirmation(self.root, IMPORT_ID)
        mark_question_sent(self.root, IMPORT_ID, "QUESTION-MESSAGE-ID")

        result = handle_clarification_event(
            self.root,
            self.event(
                "UNRELATED-REACTION-ID",
                "",
                reaction_target_message_id="OTHER-MESSAGE-ID",
                reaction_action="APPROVE",
            ),
        )

        self.assertEqual("reaction_ignored", result["action"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertFalse(status.get("publication_confirmed", False))


if __name__ == "__main__":
    unittest.main()
