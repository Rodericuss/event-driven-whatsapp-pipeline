from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_importer.marketplace import (
    MarketplaceAPIError,
    MarketplaceContractError,
    build_marketplace_payload,
    execute_marketplace_live,
    prepare_marketplace_request,
    submit_marketplace_dry_run,
)


class MarketplaceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "settings.json").write_text(
            json.dumps(
                {
                    "dry_run": True,
                    "marketplace_api": {
                        "enabled": False,
                        "base_url": "http://127.0.0.1:4000",
                        "path": "/api/internal/imported-products",
                        "dry_run_only": True,
                        "visible": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.import_id = "b5fd61c0-9b11-4393-8e62-8e62c4e6b965"
        self.package = self.root / "anuncios" / "pendentes" / self.import_id
        (self.package / "fotos").mkdir(parents=True)
        (self.package / "fotos" / "001.jpg").write_bytes(b"first")
        (self.package / "fotos" / "002.jpg").write_bytes(b"second")
        (self.package / "status.json").write_text(
            json.dumps(
                {
                    "status": "ready_for_review",
                    "validated": True,
                    "registered": False,
                    "images_uploaded": False,
                    "published": False,
                    "dry_run": True,
                }
            ),
            encoding="utf-8",
        )
        (self.package / "anuncio-extraido.json").write_text(
            json.dumps(
                {
                    "title": "Pá Carregadeira Volvo L70F 2013",
                    "year": 2013,
                    "price_in_cents": 30000000,
                    "description": "Pintura nova, toda operacional.",
                    "category": "maquinas",
                    "type": "Pá Carregadeira",
                    "seller_confirmation_required": False,
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
                            "sha256": "a" * 64,
                            "media_type": "image/jpeg",
                        },
                        {
                            "sequence": 2,
                            "filename": "002.jpg",
                            "sha256": "b" * 64,
                            "media_type": "image/jpeg",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_dry_run_payload_without_credentials_or_personal_metadata(self) -> None:
        payload = build_marketplace_payload(self.root, self.import_id)
        encoded = json.dumps(payload)
        self.assertEqual(self.import_id, payload["import_id"])
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["visible"])
        self.assertEqual([1, 2], [item["sequence"] for item in payload["images"]])
        self.assertNotIn("token", encoded.lower())
        self.assertNotIn("chat_id", encoded)
        self.assertNotIn("sender", encoded)

    def test_prepares_request_without_network_or_product_creation(self) -> None:
        result = prepare_marketplace_request(self.root, self.import_id)
        self.assertTrue(result["prepared"])
        self.assertFalse(result["network_called"])
        self.assertFalse(result["product_created"])
        self.assertEqual(self.import_id, result["idempotency_key"])
        self.assertTrue((self.package / "marketplace-request.json").is_file())

    def test_rejects_package_that_requires_review(self) -> None:
        status_path = self.package / "status.json"
        status = json.loads(status_path.read_text())
        status["status"] = "review_required"
        status["validated"] = False
        status_path.write_text(json.dumps(status))

        with self.assertRaises(MarketplaceContractError):
            build_marketplace_payload(self.root, self.import_id)

    def test_seller_confirmation_is_advisory_but_images_remain_required(self) -> None:
        listing_path = self.package / "anuncio-extraido.json"
        listing = json.loads(listing_path.read_text())
        listing["seller_confirmation_required"] = True
        listing_path.write_text(json.dumps(listing))
        payload = build_marketplace_payload(self.root, self.import_id)
        self.assertTrue(payload["listing"]["seller_confirmation_required"])
        for image in (self.package / "fotos").iterdir():
            image.unlink()
        with self.assertRaises(MarketplaceContractError):
            build_marketplace_payload(self.root, self.import_id)

    def test_submits_and_audits_a_safe_dry_run_response(self) -> None:
        self._enable_api()
        captured: dict[str, object] = {}

        def transport(url, headers, body, timeout):
            captured.update(
                url=url,
                headers=headers,
                body=json.loads(body),
                timeout=timeout,
            )
            return self._api_response(json.loads(body), replayed=False)

        result = submit_marketplace_dry_run(
            self.root,
            self.import_id,
            token="temporary-secret",
            transport=transport,
            now=datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(result["network_called"])
        self.assertTrue(result["marketplace_validated"])
        self.assertFalse(result["product_created"])
        self.assertFalse(result["images_uploaded"])
        self.assertFalse(result["published"])
        self.assertEqual(
            "http://127.0.0.1:4000/api/internal/imported-products",
            captured["url"],
        )
        self.assertEqual(
            self.import_id,
            captured["headers"]["Idempotency-Key"],  # type: ignore[index]
        )
        response_path = self.package / "marketplace-response.json"
        saved = response_path.read_text(encoding="utf-8")
        self.assertNotIn("temporary-secret", saved)
        self.assertTrue(json.loads(saved)["dry_run"])

        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual(1, status["attempts"])
        self.assertTrue(status["marketplace_validated"])
        self.assertFalse(status["registered"])
        self.assertFalse(status["images_uploaded"])
        self.assertFalse(status["published"])

    def test_repeated_submission_uses_the_same_key_and_records_replay(self) -> None:
        self._enable_api()
        calls = 0

        def transport(_url, _headers, body, _timeout):
            nonlocal calls
            calls += 1
            return self._api_response(json.loads(body), replayed=calls > 1)

        first = submit_marketplace_dry_run(
            self.root, self.import_id, token="secret", transport=transport
        )
        second = submit_marketplace_dry_run(
            self.root, self.import_id, token="secret", transport=transport
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual(2, status["attempts"])
        self.assertEqual(1, len(list(self.package.glob("marketplace-response.json"))))

    def test_rejects_an_unsafe_api_response_and_preserves_write_locks(self) -> None:
        self._enable_api()

        def transport(_url, _headers, body, _timeout):
            response = self._api_response(json.loads(body), replayed=False)
            response["data"]["writes"]["images"] = "enabled"
            return response

        with self.assertRaises(MarketplaceAPIError):
            submit_marketplace_dry_run(
                self.root, self.import_id, token="secret", transport=transport
            )

        self.assertFalse((self.package / "marketplace-response.json").exists())
        status = json.loads((self.package / "status.json").read_text())
        self.assertEqual(1, status["attempts"])
        self.assertFalse(status["registered"])
        self.assertFalse(status["images_uploaded"])
        self.assertFalse(status["published"])
        self.assertIn("travas de escrita", status["errors"][-1])

    def test_api_call_is_disabled_by_default(self) -> None:
        with self.assertRaises(MarketplaceContractError):
            submit_marketplace_dry_run(
                self.root,
                self.import_id,
                token="secret",
                transport=lambda *_args: self.fail("transport must not be called"),
            )

    def test_executes_one_visible_product_after_ordered_uploads(self) -> None:
        self._enable_live_api()
        calls: list[tuple[str, str]] = []

        def transport(method, url, headers, body, _timeout):
            calls.append((method, url))
            self.assertIn("Bearer temporary-live-secret", headers["Authorization"])
            if method == "POST" and url.endswith("/imported-products"):
                payload = json.loads(body)
                self.assertFalse(payload["dry_run"])
                return self._live_create_response(payload, product_id=99)
            if method == "PUT":
                sequence = int(url.rsplit("/", 1)[-1])
                return self._live_upload_response(99, sequence)
            if method == "POST" and url.endswith("/finalize"):
                return self._live_finalize_response(99)
            self.fail(f"unexpected request: {method} {url}")

        result = execute_marketplace_live(
            self.root,
            self.import_id,
            approval=f"CREATE_VISIBLE:{self.import_id}",
            token="temporary-live-secret",
            transport=transport,
            now=datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(99, result["product_id"])
        self.assertTrue(result["registered"])
        self.assertTrue(result["images_uploaded"])
        self.assertFalse(result["published"])
        self.assertTrue(result["visible"])
        self.assertEqual(
            ["POST", "PUT", "PUT", "POST"], [method for method, _url in calls]
        )

        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["registered"])
        self.assertTrue(status["images_uploaded"])
        self.assertFalse(status["published"])
        self.assertEqual(99, status["product_id"])
        checkpoint = (self.package / "marketplace-live-response.json").read_text()
        self.assertNotIn("temporary-live-secret", checkpoint)

    def test_live_execution_requires_exact_approval(self) -> None:
        self._enable_live_api()

        with self.assertRaises(MarketplaceContractError):
            execute_marketplace_live(
                self.root,
                self.import_id,
                approval="yes",
                token="secret",
                transport=lambda *_args: self.fail("transport must not be called"),
            )

    def test_live_retry_resumes_after_a_partial_upload(self) -> None:
        self._enable_live_api()
        first_attempt_uploads: list[int] = []

        def failing_transport(method, url, _headers, body, _timeout):
            if method == "POST" and url.endswith("/imported-products"):
                return self._live_create_response(json.loads(body), product_id=101)
            if method == "PUT":
                sequence = int(url.rsplit("/", 1)[-1])
                first_attempt_uploads.append(sequence)
                if sequence == 2:
                    raise MarketplaceAPIError("simulated partial upload")
                return self._live_upload_response(101, sequence)
            self.fail("finalize must not run after partial failure")

        with self.assertRaises(MarketplaceAPIError):
            execute_marketplace_live(
                self.root,
                self.import_id,
                approval=f"CREATE_VISIBLE:{self.import_id}",
                token="secret",
                transport=failing_transport,
            )

        status = json.loads((self.package / "status.json").read_text())
        self.assertTrue(status["registered"])
        self.assertFalse(status["images_uploaded"])
        self.assertEqual([1, 2], first_attempt_uploads)

        resumed_uploads: list[int] = []

        def resumed_transport(method, url, _headers, body, _timeout):
            if method == "POST" and url.endswith("/imported-products"):
                response = self._live_create_response(json.loads(body), product_id=101)
                response["data"]["image_uploads"][0]["status"] = "uploaded"
                return response
            if method == "PUT":
                sequence = int(url.rsplit("/", 1)[-1])
                resumed_uploads.append(sequence)
                return self._live_upload_response(101, sequence)
            if method == "POST" and url.endswith("/finalize"):
                return self._live_finalize_response(101)
            self.fail(f"unexpected request: {method} {url}")

        result = execute_marketplace_live(
            self.root,
            self.import_id,
            approval=f"CREATE_VISIBLE:{self.import_id}",
            token="secret",
            transport=resumed_transport,
        )

        self.assertEqual([2], resumed_uploads)
        self.assertTrue(result["images_uploaded"])

    def _enable_api(self) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["marketplace_api"]["enabled"] = True
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

    def _enable_live_api(self) -> None:
        self._enable_api()
        settings_path = self.root / "config" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["marketplace_api"]["dry_run_only"] = False
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        status_path = self.package / "status.json"
        status = json.loads(status_path.read_text())
        status["marketplace_validated"] = True
        status_path.write_text(json.dumps(status), encoding="utf-8")

    def _api_response(
        self, payload: dict[str, object], *, replayed: bool
    ) -> dict[str, object]:
        uploads = []
        for image in payload["images"]:  # type: ignore[union-attr]
            uploads.append(
                {
                    **image,
                    "method": "PUT",
                    "path": (
                        f"/api/internal/imported-products/{self.import_id}"
                        f"/images/{image['sequence']}"
                    ),
                    "status": "blocked_in_dry_run",
                }
            )
        return {
            "data": {
                "status": "validated",
                "dry_run": True,
                "replayed": replayed,
                "import_id": self.import_id,
                "product_id": None,
                "image_uploads": uploads,
                "writes": {
                    "product": "blocked_in_dry_run",
                    "images": "blocked_in_dry_run",
                    "publication": "blocked_in_dry_run",
                },
            }
        }

    def _live_create_response(
        self, payload: dict[str, object], *, product_id: int
    ) -> dict[str, object]:
        uploads = []
        for image in payload["images"]:  # type: ignore[union-attr]
            uploads.append(
                {
                    **image,
                    "method": "PUT",
                    "path": (
                        f"/api/internal/imported-products/{self.import_id}"
                        f"/images/{image['sequence']}"
                    ),
                    "status": "pending",
                }
            )
        return {
            "data": {
                "status": "product_created",
                "dry_run": False,
                "replayed": False,
                "import_id": self.import_id,
                "product_id": product_id,
                "visible": False,
                "published": False,
                "image_uploads": uploads,
                "writes": {
                    "product": "completed",
                    "images": "pending",
                    "publication": "blocked_until_approval",
                },
            }
        }

    def _live_upload_response(
        self, product_id: int, sequence: int
    ) -> dict[str, object]:
        return {
            "data": {
                "status": "image_uploaded",
                "import_id": self.import_id,
                "product_id": product_id,
                "sequence": sequence,
                "visible": False,
                "published": False,
                "replayed": False,
                "all_images_uploaded": sequence == 2,
            }
        }

    def _live_finalize_response(self, product_id: int) -> dict[str, object]:
        return {
            "data": {
                "status": "images_uploaded",
                "import_id": self.import_id,
                "product_id": product_id,
                "visible": True,
                "publication": {
                    "text": "official publication",
                    "images": ["first", "second"],
                    "published": False,
                },
            }
        }

        listing["seller_confirmation_required"] = False
        listing_path.write_text(json.dumps(listing))
        (self.package / "fotos" / "002.jpg").unlink()
        with self.assertRaises(MarketplaceContractError):
            build_marketplace_payload(self.root, self.import_id)


if __name__ == "__main__":
    unittest.main()
