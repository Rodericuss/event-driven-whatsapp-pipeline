from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.process import (
    OllamaVisionModel,
    apply_confirmed_field_answers,
    apply_review_corrections,
    apply_visual_confirmation,
    prices_in_text,
    process_listing,
    normalize_short_years,
    normalize_visual,
    parse_model_json,
    validate_extraction,
    validate_visual,
)


class FakeModel:
    name = "fake-text-model"
    supports_images = False

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.last_text = None

    def extract(self, text, schema, catalog):
        self.last_text = text
        return self.result


class FakeVisionModel:
    name = "fake-vision-model"
    supports_images = True

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.last_text = None

    def analyze(self, text, image_paths, schema):
        self.last_text = text
        return self.result


class FailingVisionModel:
    name = "failing-vision-model"
    supports_images = True

    def analyze(self, text, image_paths, schema):
        from whatsapp_importer.process import ProcessError

        raise ProcessError("Falha visual simulada.")


class ScriptedOllamaVisionModel(OllamaVisionModel):
    def __init__(self, responses: list[dict[str, object]]) -> None:
        super().__init__("scripted", "http://unused", 1)
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def _request(self, prompt, images):
        self.prompts.append(prompt)
        return next(self.responses)


class ProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        source_config = Path(__file__).resolve().parents[1] / "config"
        for filename in (
            "extraction-schema.json",
            "marketplace-catalog.json",
            "vision-schema.json",
        ):
            (self.root / "config" / filename).write_bytes((source_config / filename).read_bytes())
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "extraction_model": {"name": "fake"},
                    "redacted_terms": ["Empresa Exemplo"],
                }
            ),
            encoding="utf-8",
        )
        self.import_id = "b5fd61c0-9b11-4393-8e62-8e62c4e6b965"
        self.package = self.root / "anuncios" / "pendentes" / self.import_id
        (self.package / "fotos").mkdir(parents=True)
        (self.package / "mensagem-original.txt").write_text(
            "Pá carregadeira Volvo L70F ano 2013, pintura nova, toda operacional, "
            "valor 300.000,00. Fone (00) 00000-0000 Vendedor Exemplo, Empresa Exemplo.",
            encoding="utf-8",
        )
        (self.package / "metadata.json").write_text(
            json.dumps({"import_id": self.import_id, "media_count": 2}),
            encoding="utf-8",
        )
        (self.package / "status.json").write_text(
            json.dumps(
                {
                    "status": "captured",
                    "validated": False,
                    "extracted": False,
                    "registered": False,
                    "images_uploaded": False,
                    "published": False,
                    "errors": [],
                    "warnings": [],
                    "dry_run": True,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def valid_result(self) -> dict[str, object]:
        return {
            "title": "Pá Carregadeira Volvo L70F 2013",
            "year": 2013,
            "price_in_cents": 30000000,
            "description": "Pintura nova, toda operacional.",
            "category": "maquinas",
            "type": "Pá Carregadeira",
            "seller_confirmation_required": False,
            "missing_fields": [],
            "warnings": [],
            "confidence": 0.94,
        }

    def valid_visual_result(self) -> dict[str, object]:
        return {
            "is_relevant": True,
            "matches_text": True,
            "detected_type": "Pá Carregadeira",
            "detected_brand": "Volvo",
            "detected_model": "L70F",
            "same_item": True,
            "contradictions": [],
            "irrelevant_images": [],
            "promotional_or_document_images": [],
            "confidence": 0.97,
        }

    def test_brazilian_prices(self) -> None:
        self.assertEqual([15000000], prices_in_text("valor 150.000,00"))
        self.assertEqual([15000000], prices_in_text("R$ 150.000"))
        self.assertEqual([15000000], prices_in_text("preço 150 mil"))
        self.assertEqual([10000000], prices_in_text("valor *100.000,00*"))
        self.assertEqual([], prices_in_text("baixo km, apenas 6 mil km"))
        self.assertEqual([], prices_in_text("trabalhou apenas 8 mil horas"))

    def test_hilux_mileage_does_not_trigger_description_price_error(self) -> None:
        (self.package / "mensagem-original.txt").write_text(
            "Toyota Hilux CD SRX 4x4 2.8 diesel automática ano 2024, "
            "baixo km, apenas 6 mil km, valor 275.000,00",
            encoding="utf-8",
        )
        proposal = {
            "title": "Toyota Hilux CD SRX 4x4 2024",
            "year": 2024,
            "price_in_cents": 27500000,
            "description": "2.8 diesel, automática, baixo km, apenas 6 mil km",
            "category": "maquinas",
            "type": "Confirmar com o vendedor",
            "seller_confirmation_required": True,
            "missing_fields": [],
            "warnings": [],
            "confidence": 0.9,
        }

        result = process_listing(self.root, self.import_id, FakeModel(proposal))

        self.assertTrue(result["valid"])
        self.assertFalse(any("description" in error for error in result["errors"]))

    def test_two_digit_year_is_normalized_for_extraction(self) -> None:
        normalized, notices = normalize_short_years(
            "Escavadeira Liebherr 942 ano 99, operacional"
        )
        self.assertEqual(
            "Escavadeira Liebherr 942 ano 1999, operacional", normalized
        )
        self.assertEqual(["Ano abreviado normalizado: 99 → 1999."], notices)

    def test_valid_extraction_is_persisted_for_review(self) -> None:
        result = process_listing(self.root, self.import_id, FakeModel(self.valid_result()))
        self.assertTrue(result["valid"])
        self.assertEqual("ready_for_review", result["status"])
        extracted = json.loads((self.package / "anuncio-extraido.json").read_text())
        status = json.loads((self.package / "status.json").read_text())
        visual = json.loads((self.package / "analise-imagens.json").read_text())
        self.assertEqual(30000000, extracted["price_in_cents"])
        self.assertTrue(status["validated"])
        self.assertFalse(status["registered"])
        self.assertFalse(status["published"])
        self.assertFalse(visual["performed"])

    def test_phone_seller_name_and_price_are_rejected_from_description(self) -> None:
        invalid = self.valid_result()
        invalid["description"] = (
            "Valor 300.000,00, ligue (00) 00000-0000 na Empresa Exemplo."
        )
        result = process_listing(self.root, self.import_id, FakeModel(invalid))
        self.assertFalse(result["valid"])
        self.assertEqual("review_required", result["status"])
        self.assertTrue(any("telefone" in error for error in result["errors"]))
        self.assertTrue(any("vendedor" in error for error in result["errors"]))
        self.assertTrue(any("preço" in error for error in result["errors"]))

    def test_confirmed_review_correction_preserves_original_message(self) -> None:
        original = (
            "Carreta Basculante marca Pastee 3 eixos, ano 2004, "
            "valor 50.000,00."
        )
        (self.package / "mensagem-original.txt").write_text(original)
        (self.package / "review-overrides.json").write_text(
            json.dumps(
                {
                    "confirmed_by": "user",
                    "confirmed_at": "2026-07-20T13:05:00Z",
                    "text_replacements": [{"from": "Pastee", "to": "Pastre"}],
                }
            )
        )
        proposal = {
            "title": "Carreta Basculante Pastre 3 eixos 2004",
            "year": 2004,
            "price_in_cents": 5000000,
            "description": "Carreta basculante de 3 eixos.",
            "category": "maquinas",
            "type": "Caminhão",
            "seller_confirmation_required": False,
            "missing_fields": [],
            "warnings": [],
            "confidence": 0.9,
        }
        model = FakeModel(proposal)

        result = process_listing(self.root, self.import_id, model)

        self.assertTrue(result["valid"])
        self.assertIn("marca Pastre", model.last_text)
        self.assertEqual(
            original, (self.package / "mensagem-original.txt").read_text()
        )
        report = json.loads((self.package / "validacao.json").read_text())
        self.assertEqual(
            ["Correção confirmada pelo usuário: Pastee → Pastre."],
            report["review_corrections"],
        )

    def test_confirmed_price_answer_overrides_model_and_preserves_original(self) -> None:
        original = (
            "Pá carregadeira Volvo L70F ano 2013, operacional, valor 300,000,00"
        )
        (self.package / "mensagem-original.txt").write_text(original)
        (self.package / "review-overrides.json").write_text(
            json.dumps(
                {
                    "confirmed_by": "user",
                    "confirmed_at": "2026-07-29T21:00:00Z",
                    "field_answers": {"price_in_cents": 30000000},
                }
            )
        )
        model_result = self.valid_result()
        model_result["price_in_cents"] = 30000
        model = FakeModel(model_result)

        result = process_listing(self.root, self.import_id, model)

        self.assertTrue(result["valid"])
        self.assertIn("Preço: 300.000,00 (confirmado pelo usuário)", model.last_text)
        extracted = json.loads((self.package / "anuncio-extraido.json").read_text())
        self.assertEqual(30000000, extracted["price_in_cents"])
        self.assertEqual(
            original, (self.package / "mensagem-original.txt").read_text()
        )

    def test_confirmed_answer_removes_field_from_missing_fields(self) -> None:
        proposal = self.valid_result()
        proposal["price_in_cents"] = None
        proposal["missing_fields"] = ["price_in_cents"]

        corrected, notices = apply_confirmed_field_answers(
            proposal,
            {"field_answers": {"price_in_cents": 30000000}},
        )

        self.assertEqual(30000000, corrected["price_in_cents"])
        self.assertEqual([], corrected["missing_fields"])
        self.assertEqual(1, len(notices))

    def test_declared_missing_field_requires_clarification(self) -> None:
        proposal = self.valid_result()
        proposal["price_in_cents"] = None
        proposal["missing_fields"] = ["price_in_cents"]

        result = process_listing(self.root, self.import_id, FakeModel(proposal))

        self.assertFalse(result["valid"])
        self.assertTrue(
            any("price_in_cents" in error for error in result["errors"])
        )

    def test_review_correction_requires_exactly_one_source_match(self) -> None:
        with self.assertRaisesRegex(Exception, "exatamente uma vez"):
            apply_review_corrections(
                "Pastee e Pastee",
                {
                    "confirmed_by": "user",
                    "confirmed_at": "2026-07-20T13:05:00Z",
                    "text_replacements": [{"from": "Pastee", "to": "Pastre"}],
                },
            )

    def test_confirmed_visual_review_clears_only_confirmed_image_conflicts(self) -> None:
        visual = self.valid_visual_result()
        visual["matches_text"] = False
        visual["contradictions"] = [
            "Imagem 1: o caminhão-trator foi confundido com a carreta.",
            "Imagem 2: conflito não confirmado pelo usuário.",
        ]
        visual["irrelevant_images"] = [1]
        corrected, notices = apply_visual_confirmation(
            visual,
            {
                "visual_confirmation": {
                    "sequences": [1],
                    "same_item": True,
                    "matches_corrected_listing": True,
                    "reason": "A foto mostra a mesma carreta acoplada.",
                }
            },
            2,
        )
        self.assertEqual([], corrected["irrelevant_images"])
        self.assertEqual(
            ["Imagem 2: conflito não confirmado pelo usuário."],
            corrected["contradictions"],
        )
        self.assertFalse(corrected["matches_text"])
        self.assertEqual(1, len(notices))

    def test_confirmed_complete_visual_review_matches_corrected_listing(self) -> None:
        visual = self.valid_visual_result()
        visual["matches_text"] = False
        visual["contradictions"] = ["Imagem 1: falsa contradição."]
        corrected, _notices = apply_visual_confirmation(
            visual,
            {
                "visual_confirmation": {
                    "sequences": [1, 2],
                    "same_item": True,
                    "matches_corrected_listing": True,
                    "reason": "O usuário confirmou que são o mesmo item.",
                }
            },
            2,
        )
        self.assertTrue(corrected["matches_text"])
        self.assertTrue(corrected["same_item"])
        self.assertEqual([], corrected["contradictions"])

    def test_category_type_mismatch_requires_review(self) -> None:
        invalid = self.valid_result()
        invalid["category"] = "geral"
        report = validate_extraction(
            invalid,
            (self.package / "mensagem-original.txt").read_text(),
            json.loads((self.root / "config" / "extraction-schema.json").read_text()),
            json.loads((self.root / "config" / "marketplace-catalog.json").read_text()),
            2,
        )
        self.assertFalse(report["valid"])
        self.assertTrue(any("não pertence" in error for error in report["errors"]))

    def test_unknown_type_requires_review(self) -> None:
        invalid = self.valid_result()
        invalid["type"] = "Carro"
        result = process_listing(self.root, self.import_id, FakeModel(invalid))
        self.assertFalse(result["valid"])
        self.assertTrue(any("não existe" in error for error in result["errors"]))

    def test_invented_year_and_price_require_review(self) -> None:
        invalid = self.valid_result()
        invalid["year"] = 2014
        invalid["price_in_cents"] = 31000000
        result = process_listing(self.root, self.import_id, FakeModel(invalid))
        self.assertFalse(result["valid"])
        self.assertTrue(any("year não está" in error for error in result["errors"]))
        self.assertTrue(any("preço explícito" in error for error in result["errors"]))

    def test_title_without_extracted_year_requires_review(self) -> None:
        invalid = self.valid_result()
        invalid["title"] = "Pá Carregadeira Volvo L70F"
        result = process_listing(self.root, self.import_id, FakeModel(invalid))
        self.assertFalse(result["valid"])
        self.assertTrue(any("title deve conter" in error for error in result["errors"]))

    def test_fallback_requires_seller_confirmation(self) -> None:
        fallback = self.valid_result()
        fallback["category"] = "geral"
        fallback["type"] = "Confirmar com o vendedor"
        fallback["seller_confirmation_required"] = False
        result = process_listing(self.root, self.import_id, FakeModel(fallback))
        self.assertFalse(result["valid"])
        self.assertTrue(any("fallback" in error for error in result["errors"]))

    def test_unmapped_machine_uses_seller_confirmation_fallback(self) -> None:
        fallback = self.valid_result()
        fallback["category"] = "maquinas"
        fallback["type"] = "Confirmar com o vendedor"
        fallback["seller_confirmation_required"] = True

        result = process_listing(self.root, self.import_id, FakeModel(fallback))

        self.assertTrue(result["valid"])
        self.assertEqual("ready_for_review", result["status"])
        self.assertEqual([], result["errors"])

    def test_visual_validation_approves_consistent_album(self) -> None:
        report = validate_visual(self.valid_visual_result(), 2)
        self.assertTrue(report["valid"])
        self.assertTrue(report["approved"])

    def test_visual_album_accepts_compatible_dashboard_detail(self) -> None:
        album = self.valid_visual_result()
        full_vehicle = {
            "is_relevant": True,
            "matches_text": True,
            "detected_type": "Caminhão",
            "detected_brand": "Mercedes-Benz",
            "detected_model": "Axor 3344",
            "contradictions": [],
            "promotional_or_document": False,
            "confidence": 0.95,
        }
        dashboard_detail = {
            "is_relevant": True,
            "matches_text": False,
            "detected_type": None,
            "detected_brand": None,
            "detected_model": None,
            "contradictions": ["O painel isolado não confirma marca ou modelo."],
            "promotional_or_document": False,
            "confidence": 0.6,
        }
        detail_verification = {
            "advertised_brand": "Mercedes-Benz",
            "advertised_model": "Axor 3344",
            "advertised_brand_visible": False,
            "advertised_model_visible": False,
            "body_brand_visible": None,
            "visible_evidence": ["painel e hodômetro de caminhão"],
            "contradiction_confirmed": False,
            "direct_item_photo": True,
            "confidence": 0.8,
        }
        model = ScriptedOllamaVisionModel(
            [album, full_vehicle, dashboard_detail, detail_verification]
        )
        image_paths = []
        for index in (1, 2):
            path = self.package / "fotos" / f"{index:03d}.jpg"
            path.write_bytes(f"image-{index}".encode())
            image_paths.append(path)

        result = model.analyze("Mercedes-Benz Axor 3344 2018", image_paths, {})

        self.assertTrue(result["matches_text"])
        self.assertTrue(result["same_item"])
        self.assertEqual([], result["irrelevant_images"])
        self.assertEqual([], result["contradictions"])
        self.assertNotIn('"advertised_brand": "Volkswagen"', model.prompts[-1])
        self.assertNotIn('"visible_evidence": ["VW logo"', model.prompts[-1])

    def test_visual_album_keeps_relevant_brand_conflict_for_review(self) -> None:
        album = self.valid_visual_result()
        conflict = {
            "is_relevant": True,
            "matches_text": False,
            "detected_type": "Carreta Basculante",
            "detected_brand": "Pastre",
            "detected_model": None,
            "contradictions": ["A foto mostra Pastre, mas o texto informa Pastee."],
            "promotional_or_document": False,
            "confidence": 0.9,
        }
        verification = {
            "advertised_brand": "Pastee",
            "advertised_model": None,
            "advertised_brand_visible": False,
            "advertised_model_visible": False,
            "body_brand_visible": "Pastre",
            "visible_evidence": ["PASTRE"],
            "contradiction_confirmed": True,
            "direct_item_photo": True,
            "confidence": 0.95,
        }
        model = ScriptedOllamaVisionModel([album, conflict, verification])
        image_path = self.package / "fotos" / "001.jpg"
        image_path.write_bytes(b"trailer")

        result = model.analyze(
            "Carreta Basculante marca Pastee 3 eixos 2004",
            [image_path],
            {},
        )

        self.assertTrue(result["is_relevant"])
        self.assertFalse(result["matches_text"])
        self.assertEqual([], result["irrelevant_images"])
        self.assertIn("Pastre", result["contradictions"][0])

    def test_visual_prompt_distinguishes_towing_vehicle_from_trailer(self) -> None:
        album = self.valid_visual_result()
        trailer = {
            "is_relevant": True,
            "matches_text": True,
            "detected_type": "Carreta Basculante",
            "detected_brand": "Pastre",
            "detected_model": None,
            "contradictions": [],
            "promotional_or_document": False,
            "confidence": 0.9,
        }
        model = ScriptedOllamaVisionModel([album, trailer])
        image_path = self.package / "fotos" / "001.jpg"
        image_path.write_bytes(b"truck-and-trailer")

        result = model.analyze(
            "Carreta Basculante marca Pastre 3 eixos 2004",
            [image_path],
            {},
        )

        self.assertTrue(result["matches_text"])
        self.assertTrue(
            all(
                "veículo trator não contradizem a marca do implemento" in prompt
                for prompt in model.prompts
            )
        )

    def test_visual_album_recovers_detail_marked_irrelevant_without_conflict(self) -> None:
        album = self.valid_visual_result()
        detail = {
            "is_relevant": False,
            "matches_text": False,
            "detected_type": None,
            "detected_brand": None,
            "detected_model": None,
            "contradictions": ["O detalhe isolado não identifica o item."],
            "promotional_or_document": False,
            "confidence": 0.6,
        }
        verification = {
            "advertised_brand": None,
            "advertised_model": None,
            "advertised_brand_visible": False,
            "advertised_model_visible": False,
            "body_brand_visible": None,
            "visible_evidence": ["pé de apoio da carreta"],
            "contradiction_confirmed": False,
            "direct_item_photo": True,
            "confidence": 0.8,
        }
        model = ScriptedOllamaVisionModel([album, detail, verification])
        image_path = self.package / "fotos" / "001.jpg"
        image_path.write_bytes(b"trailer-detail")

        result = model.analyze(
            "Carreta Basculante marca Pastee 3 eixos 2004",
            [image_path],
            {},
        )

        self.assertTrue(result["matches_text"])
        self.assertEqual([], result["irrelevant_images"])
        self.assertEqual([], result["contradictions"])

    def test_visual_album_does_not_treat_direct_vehicle_photo_as_promotion(self) -> None:
        album = self.valid_visual_result()
        vehicle = {
            "is_relevant": True,
            "matches_text": True,
            "detected_type": "Caminhão",
            "detected_brand": "Mercedes-Benz",
            "detected_model": "Axor 3344",
            "contradictions": [],
            "promotional_or_document": True,
            "confidence": 0.95,
        }
        verification = {
            "advertised_brand": "Mercedes-Benz",
            "advertised_model": "Axor 3344",
            "advertised_brand_visible": True,
            "advertised_model_visible": True,
            "body_brand_visible": None,
            "visible_evidence": ["emblema Mercedes-Benz", "3344"],
            "contradiction_confirmed": False,
            "direct_item_photo": True,
            "confidence": 0.95,
        }
        model = ScriptedOllamaVisionModel([album, vehicle, verification])
        image_path = self.package / "fotos" / "001.jpg"
        image_path.write_bytes(b"vehicle")

        result = model.analyze("Mercedes-Benz Axor 3344 2018", [image_path], {})

        self.assertEqual([], result["promotional_or_document_images"])
        self.assertTrue(result["same_item"])

    def test_visual_conflict_is_valid_json_but_not_approved(self) -> None:
        conflict = self.valid_visual_result()
        conflict["matches_text"] = False
        conflict["contradictions"] = ["A imagem mostra uma escavadeira."]
        report = validate_visual(conflict, 2)
        self.assertTrue(report["valid"])
        self.assertFalse(report["approved"])

    def test_visual_negative_result_must_explain_and_index_images(self) -> None:
        invalid = self.valid_visual_result()
        invalid["is_relevant"] = False
        invalid["matches_text"] = False
        invalid["same_item"] = False
        report = validate_visual(invalid, 2)
        self.assertFalse(report["valid"])
        self.assertTrue(any("contradição" in error for error in report["errors"]))
        self.assertTrue(any("índices" in error for error in report["errors"]))

    def test_irrelevant_image_is_excluded_without_blocking_listing(self) -> None:
        visual = self.valid_visual_result()
        visual["irrelevant_images"] = [2]
        visual["contradictions"] = ["Imagem 2: foto mostra somente pneus."]
        report = validate_visual(visual, 2)
        self.assertTrue(report["valid"])
        self.assertTrue(report["approved"])
        self.assertEqual([2], report["excluded_images"])
        self.assertEqual([], report["relevant_contradictions"])

    def test_visual_normalization_is_limited_and_auditable(self) -> None:
        raw = self.valid_visual_result()
        raw["contradictions"] = "Não mostra um trator."
        raw["irrelevant_images"] = ["Imagem 1", "2"]
        raw["detected_model"] = "null"
        normalized, changes = normalize_visual(raw)
        self.assertEqual(["Não mostra um trator."], normalized["contradictions"])
        self.assertEqual([1, 2], normalized["irrelevant_images"])
        self.assertIsNone(normalized["detected_model"])
        self.assertEqual(3, len(changes))
        self.assertEqual("Volvo", normalized["detected_brand"])

    def test_fenced_model_json_is_parsed_without_relaxing_shape(self) -> None:
        self.assertEqual({"ok": True}, parse_model_json('```json\n{"ok": true}\n```'))

    def test_visual_image_indexes_must_exist(self) -> None:
        invalid = self.valid_visual_result()
        invalid["irrelevant_images"] = [3]
        report = validate_visual(invalid, 2)
        self.assertFalse(report["valid"])
        self.assertFalse(report["approved"])

    def test_process_combines_text_and_visual_validation(self) -> None:
        for sequence in (1, 2):
            filename = f"{sequence:03d}.jpg"
            (self.package / "fotos" / filename).write_bytes(f"image-{sequence}".encode())
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata["media"] = [{"filename": "001.jpg"}, {"filename": "002.jpg"}]
        (self.package / "metadata.json").write_text(json.dumps(metadata))
        result = process_listing(
            self.root,
            self.import_id,
            FakeModel(self.valid_result()),
            FakeVisionModel(self.valid_visual_result()),
        )
        self.assertTrue(result["valid"])
        visual = json.loads((self.package / "analise-imagens.json").read_text())
        self.assertTrue(visual["performed"])
        self.assertTrue(visual["approved"])

    def test_processing_closes_active_ingest_group(self) -> None:
        state_root = self.root / "anuncios" / "recebendo" / "state"
        state_root.mkdir(parents=True)
        state_path = state_root / "chat.json"
        state_path.write_text(
            json.dumps(
                {
                    "state": "awaiting_media",
                    "import_id": self.import_id,
                    "chat_id": "5500000000000",
                    "sender_id": "5500000000000",
                }
            )
        )
        process_listing(self.root, self.import_id, FakeModel(self.valid_result()))
        state = json.loads(state_path.read_text())
        self.assertEqual("idle", state["state"])
        self.assertEqual(self.import_id, state["last_completed_import_id"])
        self.assertNotIn("import_id", state)

    def test_visual_model_failure_is_persisted_as_non_blocking_warning(self) -> None:
        (self.package / "fotos" / "001.jpg").write_bytes(b"image")
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata["media"] = [{"filename": "001.jpg"}]
        metadata["media_count"] = 1
        (self.package / "metadata.json").write_text(json.dumps(metadata))
        result = process_listing(
            self.root,
            self.import_id,
            FakeModel(self.valid_result()),
            FailingVisionModel(),
        )
        self.assertTrue(result["valid"])
        self.assertEqual("ready_for_review", result["status"])
        self.assertTrue(
            any("Análise visual informativa" in item for item in result["warnings"])
        )
        visual = json.loads((self.package / "analise-imagens.json").read_text())
        self.assertTrue(visual["attempted"])
        self.assertFalse(visual["performed"])

    def test_required_visual_guard_blocks_model_failure(self) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["visual_validation"] = {"required": True}
        settings_path.write_text(json.dumps(settings))
        (self.package / "fotos" / "001.jpg").write_bytes(b"image")
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata["media"] = [{"filename": "001.jpg"}]
        metadata["media_count"] = 1
        (self.package / "metadata.json").write_text(json.dumps(metadata))

        result = process_listing(
            self.root,
            self.import_id,
            FakeModel(self.valid_result()),
            FailingVisionModel(),
        )

        self.assertFalse(result["valid"])
        self.assertEqual("review_required", result["status"])
        self.assertTrue(any("visual obrigatória" in error for error in result["errors"]))

    def test_required_visual_guard_blocks_mixed_album(self) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["visual_validation"] = {"required": True}
        settings_path.write_text(json.dumps(settings))
        (self.package / "fotos" / "001.jpg").write_bytes(b"image")
        metadata = json.loads((self.package / "metadata.json").read_text())
        metadata["media"] = [{"filename": "001.jpg"}]
        metadata["media_count"] = 1
        (self.package / "metadata.json").write_text(json.dumps(metadata))
        visual = self.valid_visual_result()
        visual["matches_text"] = False
        visual["same_item"] = False
        visual["contradictions"] = ["A imagem mostra outro equipamento."]

        result = process_listing(
            self.root,
            self.import_id,
            FakeModel(self.valid_result()),
            FakeVisionModel(visual),
        )

        self.assertFalse(result["valid"])
        self.assertTrue(any("visual obrigatória" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
