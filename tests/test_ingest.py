from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer import IngestError, ingest_event


CHAT = "5500000000000"
GROUP = "100000000000000001"
GROUP_JID = f"{GROUP}@g.us"
GROUP_SENDER = "554399999999"


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "openclaw-media"
        self.media_root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "allowed_chat_ids": [CHAT],
                    "allowed_media_roots": [str(self.media_root)],
                    "item_keywords": [
                        "trator",
                        "ônibus",
                        "caminhão",
                        "escavadeira",
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def event(self, message_id: str, **changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "source": "whatsapp",
            "chat_id": CHAT,
            "sender_id": CHAT,
            "sender_name": "Teste",
            "message_id": message_id,
            "received_at": "2026-07-18T13:00:00-03:00",
            "text": "",
        }
        event.update(changes)
        return event

    def image(self, name: str, content: bytes) -> Path:
        path = self.media_root / name
        path.write_bytes(content)
        return path

    def package(self, import_id: str) -> Path:
        return self.root / "anuncios" / "pendentes" / import_id

    def enable_group_intake(self, *, shadow_mode: bool = True) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["group_intake"] = {
            "enabled": True,
            "shadow_mode": shadow_mode,
            "group_name": "GRUPO DE ORIGEM EXEMPLO",
            "group_jid": GROUP_JID,
            "approval_chat_id": CHAT,
        }
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

    def group_event(self, message_id: str, **changes: object) -> dict[str, object]:
        event = self.event(
            message_id,
            chat_id=GROUP,
            chat_jid=GROUP_JID,
            chat_name="GRUPO DE ORIGEM EXEMPLO",
            sender_id=GROUP_SENDER,
            is_group=True,
            approval_chat_id=CHAT,
        )
        event.update(changes)
        return event

    def test_valid_text_then_album_creates_one_package_in_order(self) -> None:
        text = "Trator John Deere 6110J ano 2018, valor R$ 350.000"
        created = ingest_event(self.root, self.event("text-1", text=text))
        first = self.image("first.jpg", b"first")
        second = self.image("second.jpg", b"second")

        attached_1 = ingest_event(
            self.root,
            self.event(
                "image-1",
                text="<media:image>",
                media_paths=[str(first)],
                media_types=["image/jpeg"],
            ),
        )
        attached_2 = ingest_event(
            self.root,
            self.event(
                "image-2",
                text="<media:image>",
                media_paths=[str(second)],
                media_types=["image/jpeg"],
            ),
        )

        self.assertEqual("candidate_created", created["action"])
        self.assertEqual(created["import_id"], attached_1["import_id"])
        self.assertEqual(created["import_id"], attached_2["import_id"])
        package = self.package(str(created["import_id"]))
        self.assertEqual(text, (package / "mensagem-original.txt").read_text())
        metadata = json.loads((package / "metadata.json").read_text())
        self.assertEqual(["text-1", "image-1", "image-2"], metadata["message_ids"])
        self.assertEqual(["001.jpg", "002.jpg"], [item["filename"] for item in metadata["media"]])
        self.assertEqual(b"first", (package / "fotos" / "001.jpg").read_bytes())
        self.assertEqual(b"second", (package / "fotos" / "002.jpg").read_bytes())
        self.assertEqual("captured", json.loads((package / "status.json").read_text())["status"])

    def test_duplicate_message_does_not_duplicate_package(self) -> None:
        event = self.event("same", text="Escavadeira Caterpillar 320 ano 2017")
        first = ingest_event(self.root, event)
        second = ingest_event(self.root, event)
        packages = list((self.root / "anuncios" / "pendentes").iterdir())
        self.assertEqual("duplicate_ignored", second["action"])
        self.assertEqual(first["import_id"], second["import_id"])
        self.assertEqual(1, len(packages))

    def test_authorized_group_keeps_source_and_personal_approval_separate(self) -> None:
        self.enable_group_intake()
        created = ingest_event(
            self.root,
            self.group_event(
                "source-text",
                text=(
                    "Toyota Hilux SRX ano 2024, valor 275.000,00 "
                    "Fone (00) 00000-0000"
                ),
            ),
        )

        metadata = json.loads(
            (self.package(str(created["import_id"])) / "metadata.json").read_text()
        )
        self.assertEqual("candidate_created", created["action"])
        self.assertEqual(GROUP, metadata["chat_id"])
        self.assertEqual(GROUP_JID, metadata["chat_jid"])
        self.assertEqual(GROUP_SENDER, metadata["sender_id"])
        self.assertEqual(CHAT, metadata["approval_chat_id"])
        self.assertTrue(metadata["is_group"])
        self.assertTrue(metadata["intake_shadow_mode"])

    def test_other_group_remains_rejected_when_source_group_is_enabled(self) -> None:
        self.enable_group_intake()
        with self.assertRaises(IngestError):
            ingest_event(
                self.root,
                self.group_event(
                    "other-group",
                    chat_id="100000000000000099",
                    chat_jid="100000000000000099@g.us",
                    text="Trator Valtra ano 2019",
                ),
            )

    def test_same_day_duplicate_listing_and_its_media_are_ignored(self) -> None:
        text = (
            "Mercedes Benz Axor 3344 ano 2018, valor 250.000,00 "
            "Fone (00) 00000-0000"
        )
        first = ingest_event(self.root, self.event("first-text", text=text))
        first_image = self.image("first-listing.jpg", b"first-listing")
        ingest_event(
            self.root,
            self.event(
                "first-image",
                media_paths=[str(first_image)],
                media_types=["image/jpeg"],
            ),
        )

        duplicate = ingest_event(
            self.root,
            self.event("duplicate-text", text=text),
        )
        duplicate_image = self.image("duplicate-listing.jpg", b"duplicate-listing")
        ignored_media = ingest_event(
            self.root,
            self.event(
                "duplicate-image",
                media_paths=[str(duplicate_image)],
                media_types=["image/jpeg"],
            ),
        )

        self.assertEqual("duplicate_listing_text_ignored", duplicate["action"])
        self.assertEqual(first["import_id"], duplicate["import_id"])
        self.assertEqual("duplicate_listing_media_ignored", ignored_media["action"])
        metadata = json.loads(
            (self.package(str(first["import_id"])) / "metadata.json").read_text()
        )
        self.assertEqual(1, metadata["media_count"])

    def test_repeated_active_listing_without_media_keeps_its_following_images(self) -> None:
        self.enable_group_intake()
        text = (
            "Retro escavadeira Caterpillar 416E ano 2024, valor 320.000,00 "
            "Fone (00) 00000-0000"
        )
        first = ingest_event(
            self.root,
            self.group_event("retro-first-text", text=text),
        )

        repeated = ingest_event(
            self.root,
            self.group_event(
                "retro-repeated-text",
                text=text,
                received_at="2026-07-18T13:17:00-03:00",
            ),
        )
        image = self.image("retro-after-repeat.jpg", b"retro-after-repeat")
        attached = ingest_event(
            self.root,
            self.group_event(
                "retro-after-repeat-image",
                received_at="2026-07-18T13:17:01-03:00",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )

        self.assertEqual("candidate_text_appended", repeated["action"])
        self.assertEqual(first["import_id"], repeated["import_id"])
        self.assertEqual(first["import_id"], attached["import_id"])
        metadata = json.loads(
            (self.package(str(first["import_id"])) / "metadata.json").read_text()
        )
        self.assertEqual(1, metadata["media_count"])
        self.assertEqual(1, len(list((self.root / "anuncios" / "pendentes").iterdir())))

    def test_identical_listing_on_another_day_can_create_a_new_candidate(self) -> None:
        text = "Trator Valtra BM110 ano 2017"
        first = ingest_event(
            self.root,
            self.event(
                "day-one",
                text=text,
                received_at="2026-08-01T11:38:00-03:00",
            ),
        )
        state_path = next(
            (self.root / "anuncios" / "recebendo" / "state").glob("*.json")
        )
        state_path.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
        second = ingest_event(
            self.root,
            self.event(
                "day-two",
                text=text,
                received_at="2026-08-02T11:38:00-03:00",
            ),
        )

        self.assertEqual("candidate_created", second["action"])
        self.assertNotEqual(first["import_id"], second["import_id"])

    def test_duplicate_media_hash_is_not_copied_twice(self) -> None:
        created = ingest_event(
            self.root, self.event("text", text="Caminhão Volvo FH ano 2020")
        )
        first = self.image("a.jpg", b"identical")
        second = self.image("b.jpg", b"identical")
        ingest_event(
            self.root,
            self.event(
                "photo-a",
                media_paths=[str(first)],
                media_types=["image/jpeg"],
            ),
        )
        result = ingest_event(
            self.root,
            self.event(
                "photo-b",
                media_paths=[str(second)],
                media_types=["image/jpeg"],
            ),
        )
        self.assertEqual(0, result["media_copied"])
        self.assertEqual(1, result["duplicate_media"])
        photos = list((self.package(str(created["import_id"])) / "fotos").iterdir())
        self.assertEqual(1, len(photos))

    def test_invalid_text_and_missing_year_are_ignored(self) -> None:
        greeting = ingest_event(self.root, self.event("hello", text="Bom dia"))
        no_year = ingest_event(
            self.root, self.event("no-year", text="Trator John Deere à venda")
        )
        self.assertEqual("invalid_text_ignored", greeting["action"])
        self.assertEqual("invalid_text_ignored", no_year["action"])
        self.assertFalse((self.root / "anuncios" / "pendentes").exists())

    def test_strong_listing_without_year_keeps_its_thirteen_images(self) -> None:
        self.enable_group_intake(shadow_mode=False)
        text = (
            "Caçamba basculante meia cana Rossetti 20m3, completa, pistão "
            "frontal, chapa de aço reforçada, valor 50.000,00 cada, várias "
            "unidades, também de 14 e 16m3. Fone (00) 00000-0000."
        )

        created = ingest_event(
            self.root,
            self.group_event("missing-year-text", text=text),
        )
        attached_ids = []
        for index in range(1, 14):
            image = self.image(f"missing-year-{index}.jpg", f"image-{index}".encode())
            attached = ingest_event(
                self.root,
                self.group_event(
                    f"missing-year-image-{index}",
                    text="<media:image>",
                    media_paths=[str(image)],
                    media_types=["image/jpeg"],
                ),
            )
            attached_ids.append(attached.get("import_id"))

        self.assertEqual("candidate_created", created["action"])
        self.assertEqual(
            [created["import_id"]] * 13,
            attached_ids,
        )
        package = self.package(str(created["import_id"]))
        metadata = json.loads((package / "metadata.json").read_text())
        self.assertEqual(13, metadata["media_count"])
        self.assertEqual(text, (package / "mensagem-original.txt").read_text())

    def test_two_digit_year_after_ano_creates_candidate(self) -> None:
        result = ingest_event(
            self.root,
            self.event(
                "short-year",
                text="Escavadeira Liebherr 942 ano 99, operacional",
            ),
        )
        self.assertEqual("candidate_created", result["action"])
        self.assertEqual(
            "Escavadeira Liebherr 942 ano 99, operacional",
            (self.package(str(result["import_id"])) / "mensagem-original.txt").read_text(),
        )

    def test_strong_sale_signals_accept_unknown_item_types_from_source_group(self) -> None:
        listings = [
            "Mercedes Benz Axor 3344 ano 2018, traçado 6x4, valor "
            "250.000,00 Fone (00) 00000-0000 Vendedor Exemplo",
            "Toyota Hilux CD Srx 4x4, automática, ano 2024, valor "
            "275.000,00 Fone (00) 00000-0000 Antonio",
            "Volkswagen 14.150 ano 94, caçamba basculante, valor "
            "120.000,00 Fone (00) 00000-0000 Vendedor Exemplo",
            "Vw saveiro 1.6, total flex 8v ano 2011, valor 30.000,00 "
            "Fone (00) 00000-0000 Antonio",
            "Vw Saveiro Robust 1.6 ano 2018, total flex, valor 40.000,00 "
            "Fone (00) 00000-0000 Vendedor Exemplo",
            "Fresadora de asfalto Caterpillar PM 102, ano 2008, valor "
            "600.000,00 Fone (00) 00000-0000 Vendedor Exemplo",
        ]
        import_ids = []

        for index, text in enumerate(listings, start=1):
            created = ingest_event(
                self.root,
                self.event(f"unknown-text-{index}", text=text),
            )
            image = self.image(f"unknown-{index}.jpg", f"image-{index}".encode())
            attached = ingest_event(
                self.root,
                self.event(
                    f"unknown-image-{index}",
                    media_paths=[str(image)],
                    media_types=["image/jpeg"],
                ),
            )
            self.assertEqual("candidate_created", created["action"])
            self.assertEqual(created["import_id"], attached["import_id"])
            import_ids.append(created["import_id"])

        self.assertEqual(6, len(set(import_ids)))

    def test_sale_signals_remain_strict_enough_to_ignore_noise(self) -> None:
        freight = ingest_event(
            self.root,
            self.event(
                "freight",
                text=(
                    "Carreta prancha 03 eixos indo de Cidade Exemplo para São Paulo "
                    "vazio a procura de frete."
                ),
            ),
        )
        missing_contact = ingest_event(
            self.root,
            self.event(
                "missing-contact",
                text="Toyota Hilux ano 2024, valor 275.000,00",
            ),
        )

        self.assertEqual("invalid_text_ignored", freight["action"])
        self.assertEqual("invalid_text_ignored", missing_contact["action"])
        self.assertFalse((self.root / "anuncios" / "pendentes").exists())

    def test_similar_models_with_different_years_create_separate_candidates(self) -> None:
        first_text = (
            "Vw saveiro 1.6 total flex ano 2011, valor 30.000,00 "
            "Fone (00) 00000-0000"
        )
        second_text = (
            "Vw Saveiro Robust 1.6 total flex ano 2018, valor 40.000,00 "
            "Fone (00) 00000-0000"
        )
        first = ingest_event(self.root, self.event("saveiro-2011", text=first_text))
        image = self.image("saveiro-2011.jpg", b"saveiro-2011")
        ingest_event(
            self.root,
            self.event(
                "saveiro-2011-image",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )

        second = ingest_event(self.root, self.event("saveiro-2018", text=second_text))

        self.assertEqual("candidate_created", second["action"])
        self.assertNotEqual(first["import_id"], second["import_id"])
        self.assertEqual(2, len(list((self.root / "anuncios" / "pendentes").iterdir())))

    def test_generic_sale_details_do_not_merge_different_same_year_models(self) -> None:
        axor = (
            "Mercedes Benz Axor 3344 ano 2018, excelente estado de mecânica e "
            "conservação, valor 250.000,00 Fone (00) 00000-0000 Vendedor Exemplo"
        )
        excavator = (
            "Escavadeira Hidráulica Caterpillar 320NG ano 2018, único dono, nota "
            "fiscal, excelente estado de mecânica e conservação, valor 400.000,00 "
            "Fone (00) 00000-0000 Vendedor Exemplo"
        )
        first = ingest_event(self.root, self.event("axor", text=axor))
        image = self.image("axor.jpg", b"axor")
        ingest_event(
            self.root,
            self.event(
                "axor-image",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )

        second = ingest_event(self.root, self.event("cat-320", text=excavator))

        self.assertEqual("candidate_created", second["action"])
        self.assertNotEqual(first["import_id"], second["import_id"])

    def test_short_same_model_text_is_appended_as_supplement(self) -> None:
        original = (
            "Pa Carregadeira Foton FL917F ano 2010, toda operacional, "
            "valor 90.000,00 Fone (00) 00000-0000"
        )
        candidate = ingest_event(self.root, self.event("foton", text=original))

        appended = ingest_event(
            self.root,
            self.event("foton-detail", text="FL917F ano 2010"),
        )

        self.assertEqual("candidate_text_appended", appended["action"])
        self.assertEqual(candidate["import_id"], appended["import_id"])

    def test_new_candidate_marks_previous_incomplete(self) -> None:
        first = ingest_event(
            self.root, self.event("one", text="Trator Massey Ferguson ano 2015")
        )
        second = ingest_event(
            self.root, self.event("two", text="Ônibus Volkswagen 15.190 ano 2012")
        )
        status = json.loads(
            (self.package(str(first["import_id"])) / "status.json").read_text()
        )
        self.assertEqual("captured_incomplete", status["status"])
        self.assertEqual(first["import_id"], second["previous_incomplete_import_id"])

    def test_new_candidate_keeps_previous_candidate_with_images_complete(self) -> None:
        first = ingest_event(
            self.root,
            self.event("text-a", text="Trator Massey Ferguson ano 2015"),
        )
        first_image = self.image("first-candidate.jpg", b"first-candidate")
        ingest_event(
            self.root,
            self.event(
                "image-a",
                media_paths=[str(first_image)],
                media_types=["image/jpeg"],
            ),
        )

        second = ingest_event(
            self.root,
            self.event("text-b", text="Ônibus Volkswagen 15.190 ano 2012"),
        )

        first_status = json.loads(
            (self.package(str(first["import_id"])) / "status.json").read_text()
        )
        self.assertEqual("captured", first_status["status"])
        self.assertEqual(first["import_id"], second["previous_completed_import_id"])
        self.assertIsNone(second["previous_incomplete_import_id"])

    def test_random_text_does_not_detach_following_images(self) -> None:
        candidate = ingest_event(
            self.root,
            self.event("text", text="Escavadeira Caterpillar 320 ano 2017"),
        )
        ignored = ingest_event(
            self.root, self.event("noise", text="Bom dia, pessoal")
        )
        image = self.image("after-noise.jpg", b"after-noise")
        attached = ingest_event(
            self.root,
            self.event(
                "image",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )

        self.assertEqual("invalid_text_ignored", ignored["action"])
        self.assertEqual(candidate["import_id"], attached["import_id"])

    def test_detail_text_is_appended_without_changing_original_source(self) -> None:
        original = "Escavadeira Caterpillar 320 ano 2017"
        candidate = ingest_event(
            self.root, self.event("text", text=original)
        )
        supplement = "Valor R$ 400.000, motor seis cilindros e nota fiscal."

        appended = ingest_event(
            self.root, self.event("detail", text=supplement)
        )

        package = self.package(str(candidate["import_id"]))
        self.assertEqual("candidate_text_appended", appended["action"])
        self.assertEqual(original, (package / "mensagem-original.txt").read_text())
        self.assertEqual(
            f"{original}\n{supplement}",
            (package / "mensagem-combinada.txt").read_text(),
        )
        metadata = json.loads((package / "metadata.json").read_text())
        self.assertEqual(["text", "detail"], metadata["message_ids"])
        self.assertEqual(2, len(metadata["text_segments"]))

    def test_four_text_and_image_sequences_create_four_independent_candidates(self) -> None:
        listings = [
            ("Escavadeira Caterpillar 320 ano 2019", b"excavator"),
            ("Trator John Deere 6110J ano 2018", b"tractor"),
            ("Ônibus Volkswagen 15.190 ano 2012", b"bus"),
            ("Caminhão Volvo FH 540 ano 2020", b"truck"),
        ]
        candidates = []

        for index, (text, image_contents) in enumerate(listings, start=1):
            candidate = ingest_event(
                self.root,
                self.event(f"text-{index}", text=text),
            )
            image = self.image(f"image-{index}.jpg", image_contents)
            attached = ingest_event(
                self.root,
                self.event(
                    f"image-{index}",
                    media_paths=[str(image)],
                    media_types=["image/jpeg"],
                ),
            )
            self.assertEqual(candidate["import_id"], attached["import_id"])
            candidates.append(candidate)

        self.assertEqual(4, len({item["import_id"] for item in candidates}))
        for index, ((text, image_contents), candidate) in enumerate(
            zip(listings, candidates), start=1
        ):
            package = self.package(str(candidate["import_id"]))
            self.assertEqual(text, (package / "mensagem-original.txt").read_text())
            self.assertEqual(
                image_contents,
                (package / "fotos" / "001.jpg").read_bytes(),
            )
            metadata = json.loads((package / "metadata.json").read_text())
            self.assertEqual([f"text-{index}", f"image-{index}"], metadata["message_ids"])
            self.assertEqual(1, metadata["media_count"])

    def test_orphan_image_is_preserved(self) -> None:
        image = self.image("orphan.jpg", b"orphan")
        result = ingest_event(
            self.root,
            self.event(
                "orphan",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )
        self.assertEqual("orphan_media_stored", result["action"])
        orphan = self.root / "anuncios" / "recebendo" / "orfaos" / str(result["orphan_id"])
        self.assertTrue((orphan / "001.jpg").is_file())

    def test_media_remains_attached_while_clarification_is_pending(self) -> None:
        created = ingest_event(
            self.root,
            self.event("text", text="Escavadeira Caterpillar 320 ano 2017"),
        )
        import_id = str(created["import_id"])
        package = self.package(import_id)
        status = json.loads((package / "status.json").read_text())
        status["status"] = "awaiting_clarification"
        (package / "status.json").write_text(json.dumps(status))
        state_paths = list(
            (self.root / "anuncios" / "recebendo" / "state").glob("*.json")
        )
        state = json.loads(state_paths[0].read_text())
        state["state"] = "awaiting_clarification"
        state_paths[0].write_text(json.dumps(state))
        image = self.image("extra.jpg", b"extra")

        result = ingest_event(
            self.root,
            self.event(
                "extra-photo",
                media_paths=[str(image)],
                media_types=["image/jpeg"],
            ),
        )

        self.assertEqual("clarification_media_attached", result["action"])
        self.assertEqual(import_id, result["import_id"])
        self.assertEqual(
            "awaiting_clarification",
            json.loads((package / "status.json").read_text())["status"],
        )
        self.assertEqual(
            1, json.loads((package / "metadata.json").read_text())["media_count"]
        )

    def test_non_personal_chat_and_group_are_rejected(self) -> None:
        with self.assertRaises(IngestError):
            ingest_event(
                self.root,
                self.event(
                    "other",
                    chat_id="5511999999999",
                    text="Trator Valtra ano 2019",
                ),
            )
        with self.assertRaises(IngestError):
            ingest_event(
                self.root,
                self.event("group", is_group=True, text="Trator Valtra ano 2019"),
            )

    def test_media_outside_openclaw_root_is_rejected(self) -> None:
        ingest_event(self.root, self.event("text", text="Trator Valtra ano 2019"))
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        with self.assertRaises(IngestError):
            ingest_event(
                self.root,
                self.event(
                    "outside",
                    media_paths=[str(outside)],
                    media_types=["image/jpeg"],
                ),
            )


if __name__ == "__main__":
    unittest.main()
