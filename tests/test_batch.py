from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.batch import flush_stream, stage_event


CHAT = "5500000000000"
GROUP = "100000000000000001"
GROUP_JID = f"{GROUP}@g.us"
GROUP_SENDER = "5500000000001"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_22_ads.json"


class BufferedBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "allowed_chat_ids": [CHAT],
                    "allowed_media_roots": [str(self.media_root)],
                    "item_keywords": [
                        "retro escavadeira",
                        "trator",
                        "hilux",
                        "escavadeira",
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def event(
        self,
        message_id: str,
        received_at: str,
        *,
        text: str = "",
        image: Path | None = None,
        media_type: str = "image/jpeg",
    ) -> dict[str, object]:
        event: dict[str, object] = {
            "source": "whatsapp",
            "chat_id": CHAT,
            "sender_id": CHAT,
            "message_id": message_id,
            "received_at": received_at,
            "text": text,
        }
        if image is not None:
            event["media_paths"] = [str(image)]
            event["media_types"] = [media_type]
        return event

    def image(self, name: str, content: bytes) -> Path:
        path = self.media_root / name
        path.write_bytes(content)
        return path

    def enable_group_intake(self) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["group_intake"] = {
            "enabled": True,
            "shadow_mode": True,
            "group_name": "GRUPO DE ORIGEM EXEMPLO",
            "group_jid": GROUP_JID,
            "approval_chat_id": CHAT,
        }
        settings_path.write_text(json.dumps(settings))

    def group_event(
        self,
        message_id: str,
        received_at: str,
        *,
        text: str = "",
        image: Path | None = None,
    ) -> dict[str, object]:
        event: dict[str, object] = {
            "source": "whatsapp",
            "chat_id": GROUP,
            "chat_jid": GROUP_JID,
            "sender_id": GROUP_SENDER,
            "approval_chat_id": CHAT,
            "message_id": message_id,
            "received_at": received_at,
            "text": text,
            "is_group": True,
            "intake_shadow_mode": True,
        }
        if image is not None:
            event["media_paths"] = [str(image)]
            event["media_types"] = ["image/jpeg"]
        return event

    def test_delayed_older_media_is_sorted_back_into_the_correct_candidate(self) -> None:
        texts = [
            ("retro", "2026-08-02T01:24:31Z", "Retro escavadeira Caterpillar 416E ano 2008"),
            ("valtra", "2026-08-02T01:24:33Z", "Trator Valtra BM110 ano 2017"),
            ("hilux", "2026-08-02T01:24:34Z", "Toyota Hilux SRX ano 2024"),
            ("cat", "2026-08-02T01:24:36Z", "Escavadeira Caterpillar 320NG ano 2018"),
        ]
        for message_id, received_at, text in texts:
            stage_event(self.root, self.event(message_id, received_at, text=text))
            if message_id == "valtra":
                for index in range(5):
                    image = self.image(f"valtra-{index}.jpg", f"v{index}".encode())
                    stage_event(
                        self.root,
                        self.event(
                            f"valtra-image-{index}",
                            "2026-08-02T01:24:33Z",
                            image=image,
                        ),
                    )
            if message_id == "hilux":
                for index in range(5):
                    image = self.image(f"hilux-{index}.jpg", f"h{index}".encode())
                    stage_event(
                        self.root,
                        self.event(
                            f"hilux-image-{index}",
                            "2026-08-02T01:24:35Z",
                            image=image,
                        ),
                    )
            if message_id == "cat":
                for index in range(5):
                    image = self.image(f"cat-{index}.jpg", f"c{index}".encode())
                    stage_event(
                        self.root,
                        self.event(
                            f"cat-image-{index}",
                            "2026-08-02T01:24:37Z",
                            image=image,
                        ),
                    )

        # These callbacks were observed last, but WhatsApp timestamps place them
        # between the first and second texts.
        for index in range(11):
            image = self.image(f"retro-{index}.jpg", f"r{index}".encode())
            stage_event(
                self.root,
                self.event(
                    f"retro-image-{index}",
                    "2026-08-02T01:24:32Z",
                    image=image,
                ),
            )

        result = flush_stream(self.root, CHAT, CHAT)

        self.assertEqual("batch_flushed", result["action"])
        packages = {}
        for package in (self.root / "anuncios" / "pendentes").iterdir():
            text = (package / "mensagem-original.txt").read_text()
            metadata = json.loads((package / "metadata.json").read_text())
            packages[text] = metadata["media_count"]
        self.assertEqual(
            {
                texts[0][2]: 11,
                texts[1][2]: 5,
                texts[2][2]: 5,
                texts[3][2]: 5,
            },
            packages,
        )

    def test_video_is_skipped_without_aborting_the_candidate(self) -> None:
        stage_event(
            self.root,
            self.event("text", "2026-08-02T01:00:00Z", text="Trator Valtra ano 2017"),
        )
        video = self.image("clip.mp4", b"video")
        stage_event(
            self.root,
            self.event(
                "video",
                "2026-08-02T01:00:01Z",
                image=video,
                media_type="video/mp4",
            ),
        )
        image = self.image("photo.jpg", b"photo")
        stage_event(
            self.root,
            self.event("image", "2026-08-02T01:00:02Z", image=image),
        )

        result = flush_stream(self.root, CHAT, CHAT)

        self.assertTrue(
            any(item.get("media_skipped") == 1 for item in result["results"])
        )
        package = next((self.root / "anuncios" / "pendentes").iterdir())
        metadata = json.loads((package / "metadata.json").read_text())
        self.assertEqual(1, metadata["media_count"])

    def test_synthetic_replay_separates_22_unique_ads(self) -> None:
        self.enable_group_intake()
        fixture = json.loads(FIXTURE.read_text())

        for index, entry in enumerate(fixture["events"]):
            received_at = entry["at"]
            stage_event(
                self.root,
                self.group_event(
                    f"synthetic-text-{index}", received_at, text=entry["text"]
                ),
            )
            image_time = (
                datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            for image_index in range(entry.get("images", 0)):
                image = self.image(
                    f"synthetic-{index}-{image_index}.jpg",
                    f"synthetic-image-{index}-{image_index}".encode(),
                )
                stage_event(
                    self.root,
                    self.group_event(
                        f"synthetic-image-{index}-{image_index}",
                        image_time,
                        image=image,
                    ),
                )

        result = flush_stream(self.root, GROUP, GROUP_SENDER)

        self.assertEqual("batch_flushed", result["action"])
        self.assertEqual(CHAT, result["approval_chat_id"])
        actions = [item["action"] for item in result["results"]]
        self.assertEqual(22, actions.count("candidate_created"))
        self.assertEqual(0, actions.count("duplicate_listing_text_ignored"))
        self.assertEqual(0, actions.count("duplicate_listing_media_ignored"))
        self.assertEqual(0, actions.count("candidate_text_appended"))
        self.assertEqual(0, actions.count("invalid_text_ignored"))

        packages = list((self.root / "anuncios" / "pendentes").iterdir())
        self.assertEqual(fixture["expected_unique_candidates"], len(packages))
        for package in packages:
            metadata = json.loads((package / "metadata.json").read_text())
            self.assertEqual(GROUP, metadata["chat_id"])
            self.assertEqual(GROUP_JID, metadata["chat_jid"])
            self.assertEqual(CHAT, metadata["approval_chat_id"])
            self.assertTrue(metadata["is_group"])
            self.assertTrue(metadata["intake_shadow_mode"])
            self.assertEqual(1, metadata["media_count"])
