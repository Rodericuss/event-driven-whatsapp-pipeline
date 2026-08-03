from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ConfigurationError, load_settings


class MarketplaceContractError(RuntimeError):
    """A package is not eligible for the marketplace contract."""


class MarketplaceAPIError(RuntimeError):
    """The marketplace rejected or could not process a dry-run request."""


Transport = Callable[[str, dict[str, str], bytes, float], dict[str, Any]]
LiveTransport = Callable[
    [str, str, dict[str, str], bytes, float], dict[str, Any]
]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise MarketplaceContractError(f"JSON ausente ou inválido: {path}") from error


def _atomic_json(path: Path, value: Any) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_marketplace_payload(
    root: Path, import_id: str, *, allow_registered: bool = False
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    package = (root / "anuncios" / "pendentes" / import_id).resolve()
    pending_root = (root / "anuncios" / "pendentes").resolve()
    if package.parent != pending_root or not package.is_dir():
        raise MarketplaceContractError("import_id não corresponde a um pacote pendente.")

    status = _load_json(package / "status.json")
    listing = _load_json(package / "anuncio-extraido.json")
    metadata = _load_json(package / "metadata.json")

    if status.get("status") != "ready_for_review" or status.get("validated") is not True:
        raise MarketplaceContractError("O pacote não está validado e pronto para revisão.")
    forbidden_fields = (
        ("images_uploaded", "published")
        if allow_registered
        else ("registered", "images_uploaded", "published")
    )
    for forbidden in forbidden_fields:
        if status.get(forbidden) is not False:
            raise MarketplaceContractError(f"A trava {forbidden}=false não está preservada.")
    images = []
    excluded = {
        int(value)
        for value in status.get("excluded_image_sequences", [])
        if isinstance(value, int)
    }
    for item in metadata.get("media", []):
        source_sequence = item.get("sequence")
        if source_sequence in excluded:
            continue
        if not isinstance(source_sequence, int):
            raise MarketplaceContractError("A sequência das imagens não é contínua.")
        filename = item.get("filename")
        image_path = package / "fotos" / str(filename)
        if not image_path.is_file():
            raise MarketplaceContractError(f"Imagem ausente: {filename}")
        images.append(
            {
                "sequence": len(images) + 1,
                "source_sequence": source_sequence,
                "filename": filename,
                "sha256": item.get("sha256"),
                "media_type": item.get("media_type"),
            }
        )
    if not images:
        raise MarketplaceContractError("O contrato exige ao menos uma imagem.")

    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise MarketplaceContractError(str(error)) from error
    api_settings = settings.get("marketplace_api")
    visible = api_settings.get("visible") if isinstance(api_settings, dict) else None
    if type(visible) is not bool:
        raise MarketplaceContractError(
            "marketplace_api.visible deve ser booleano de forma explícita."
        )

    return {
        "import_id": import_id,
        "dry_run": True,
        "visible": visible,
        "listing": {
            "title": listing.get("title"),
            "year": listing.get("year"),
            "price_in_cents": listing.get("price_in_cents"),
            "description": listing.get("description"),
            "category": listing.get("category"),
            "type": listing.get("type"),
            "seller_confirmation_required": listing.get(
                "seller_confirmation_required"
            ),
        },
        "images": images,
    }


def prepare_marketplace_request(root: Path, import_id: str) -> dict[str, Any]:
    payload = build_marketplace_payload(root, import_id)
    package = root.expanduser().resolve() / "anuncios" / "pendentes" / import_id
    target = package / "marketplace-request.json"
    _atomic_json(target, payload)
    return {
        "import_id": import_id,
        "prepared": True,
        "request_path": str(target),
        "method": "POST",
        "path": "/api/internal/imported-products",
        "idempotency_key": import_id,
        "network_called": False,
        "product_created": False,
        "dry_run": True,
    }


def _http_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> dict[str, Any]:
    request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        response_body = error.read()
        detail = response_body.decode("utf-8", errors="replace")[:500]
        raise MarketplaceAPIError(
            f"API respondeu HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise MarketplaceAPIError(f"Falha de conexão com a API: {error.reason}") from error

    if status != 200:
        raise MarketplaceAPIError(f"API respondeu HTTP inesperado {status}.")
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise MarketplaceAPIError("A API retornou JSON inválido.") from error
    if not isinstance(decoded, dict):
        raise MarketplaceAPIError("A API retornou um envelope inválido.")
    return decoded


def _validate_dry_run_response(
    response: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise MarketplaceAPIError("Resposta sem o objeto data.")
    if data.get("import_id") != payload["import_id"]:
        raise MarketplaceAPIError("A API respondeu com import_id diferente.")
    if data.get("status") != "validated" or data.get("dry_run") is not True:
        raise MarketplaceAPIError("A API não confirmou a validação em DRY_RUN.")
    if data.get("product_id") is not None:
        raise MarketplaceAPIError("A API informou product_id durante o DRY_RUN.")

    writes = data.get("writes")
    blocked = "blocked_in_dry_run"
    if not isinstance(writes, dict) or any(
        writes.get(operation) != blocked
        for operation in ("product", "images", "publication")
    ):
        raise MarketplaceAPIError("A API não confirmou todas as travas de escrita.")

    uploads = data.get("image_uploads")
    expected_images = payload["images"]
    if not isinstance(uploads, list) or len(uploads) != len(expected_images):
        raise MarketplaceAPIError("O plano de uploads não corresponde às imagens.")

    for expected, upload in zip(expected_images, uploads, strict=True):
        if not isinstance(upload, dict):
            raise MarketplaceAPIError("O plano de uploads contém entrada inválida.")
        expected_path = (
            f"/api/internal/imported-products/{payload['import_id']}"
            f"/images/{expected['sequence']}"
        )
        expected_fields = {
            "sequence": expected["sequence"],
            "filename": expected["filename"],
            "sha256": expected["sha256"],
            "media_type": expected["media_type"],
            "method": "PUT",
            "path": expected_path,
            "status": blocked,
        }
        if any(upload.get(key) != value for key, value in expected_fields.items()):
            raise MarketplaceAPIError("O plano de uploads diverge do pacote local.")

    return data


def submit_marketplace_dry_run(
    root: Path,
    import_id: str,
    *,
    token: str | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    payload = build_marketplace_payload(root, import_id)
    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise MarketplaceContractError(str(error)) from error
    api = settings.get("marketplace_api")
    if not isinstance(api, dict) or api.get("enabled") is not True:
        raise MarketplaceContractError("A chamada da API do marketplace está desativada.")
    base_url = str(api.get("base_url") or "").rstrip("/")
    path = str(api.get("path") or "")
    if not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise MarketplaceContractError("Nesta fase, a API deve apontar para localhost.")
    if path != "/api/internal/imported-products":
        raise MarketplaceContractError("Caminho inesperado para a API interna.")

    token = token or os.environ.get("MARKETPLACE_API_TOKEN")
    if not token:
        raise MarketplaceContractError("MARKETPLACE_API_TOKEN não está definido.")

    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path)
    status["attempts"] = int(status.get("attempts") or 0) + 1
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Idempotency-Key": import_id,
    }

    try:
        response = (transport or _http_transport)(
            f"{base_url}{path}",
            headers,
            body,
            float(api.get("timeout_seconds") or 30),
        )
        data = _validate_dry_run_response(response, payload)
    except MarketplaceAPIError as error:
        errors = status.setdefault("errors", [])
        errors.append(f"Marketplace DRY_RUN: {error}")
        _atomic_json(status_path, status)
        raise

    received_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    response_path = package / "marketplace-response.json"
    _atomic_json(
        response_path,
        {
            "import_id": import_id,
            "received_at": received_at,
            "dry_run": True,
            "response": response,
        },
    )
    status["marketplace_validated"] = True
    status["marketplace_validated_at"] = received_at
    _atomic_json(status_path, status)
    return {
        "import_id": import_id,
        "network_called": True,
        "marketplace_validated": True,
        "replayed": data.get("replayed") is True,
        "product_created": False,
        "images_uploaded": False,
        "published": False,
        "response_path": str(response_path),
        "dry_run": True,
    }


def _live_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        response_body = error.read()
        detail = response_body.decode("utf-8", errors="replace")[:500]
        raise MarketplaceAPIError(
            f"API respondeu HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise MarketplaceAPIError(f"Falha de conexão com a API: {error.reason}") from error

    if status != 200:
        raise MarketplaceAPIError(f"API respondeu HTTP inesperado {status}.")
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise MarketplaceAPIError("A API retornou JSON inválido.") from error
    if not isinstance(decoded, dict):
        raise MarketplaceAPIError("A API retornou um envelope inválido.")
    return decoded


def _multipart_file(filename: str, media_type: str, contents: bytes) -> tuple[str, bytes]:
    boundary = f"romildonegocios-{os.urandom(16).hex()}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            contents,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return boundary, body


def _live_settings(root: Path) -> tuple[str, str, float]:
    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise MarketplaceContractError(str(error)) from error
    api = settings.get("marketplace_api")
    if not isinstance(api, dict) or api.get("enabled") is not True:
        raise MarketplaceContractError("A chamada da API do marketplace está desativada.")
    if api.get("dry_run_only") is not False:
        raise MarketplaceContractError(
            "A execução real exige dry_run_only=false de forma explícita."
        )
    base_url = str(api.get("base_url") or "").rstrip("/")
    path = str(api.get("path") or "")
    if not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise MarketplaceContractError("Nesta fase, a API deve apontar para localhost.")
    if path != "/api/internal/imported-products":
        raise MarketplaceContractError("Caminho inesperado para a API interna.")
    return base_url, path, float(api.get("timeout_seconds") or 60)


def _validate_live_create(
    response: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise MarketplaceAPIError("Resposta real sem o objeto data.")
    if data.get("import_id") != payload["import_id"] or data.get("dry_run") is not False:
        raise MarketplaceAPIError("A API respondeu com identidade ou modo divergente.")
    if not isinstance(data.get("product_id"), int):
        raise MarketplaceAPIError("A API não retornou product_id inteiro.")
    if data.get("visible") is not False or data.get("published") is not False:
        raise MarketplaceAPIError("O produto real não permaneceu invisível e não publicado.")
    writes = data.get("writes")
    if (
        not isinstance(writes, dict)
        or writes.get("product") != "completed"
        or writes.get("publication") != "blocked_until_approval"
    ):
        raise MarketplaceAPIError("A API não confirmou as travas da execução real.")
    uploads = data.get("image_uploads")
    if not isinstance(uploads, list) or len(uploads) != len(payload["images"]):
        raise MarketplaceAPIError("A API retornou plano real de uploads inválido.")
    return data


def _validate_live_upload(
    response: dict[str, Any], product_id: int, sequence: int
) -> dict[str, Any]:
    data = response.get("data")
    if (
        not isinstance(data, dict)
        or data.get("product_id") != product_id
        or data.get("sequence") != sequence
        or data.get("visible") is not False
        or data.get("published") is not False
    ):
        raise MarketplaceAPIError(f"Resposta inválida para o upload {sequence}.")
    return data


def _validate_live_finalize(
    response: dict[str, Any], product_id: int, visible: bool
) -> dict[str, Any]:
    data = response.get("data")
    publication = data.get("publication") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("status") != "images_uploaded"
        or data.get("product_id") != product_id
        or data.get("visible") is not visible
        or not isinstance(publication, dict)
        or publication.get("published") is not False
    ):
        raise MarketplaceAPIError("A API não confirmou a finalização segura.")
    return data


def execute_marketplace_live(
    root: Path,
    import_id: str,
    *,
    approval: str,
    token: str | None = None,
    transport: LiveTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    payload = build_marketplace_payload(root, import_id, allow_registered=True)
    expected_approval = (
        f"CREATE_VISIBLE:{import_id}"
        if payload["visible"] is True
        else f"CREATE_INVISIBLE:{import_id}"
    )
    if approval != expected_approval:
        raise MarketplaceContractError("A aprovação explícita do import_id é obrigatória.")

    payload["dry_run"] = False
    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path)
    if status.get("marketplace_validated") is not True:
        raise MarketplaceContractError("O pacote ainda não foi validado pela API em DRY_RUN.")
    if status.get("images_uploaded") is not False or status.get("published") is not False:
        raise MarketplaceContractError("O pacote já foi finalizado ou publicado.")

    base_url, path, timeout = _live_settings(root)
    token = token or os.environ.get("MARKETPLACE_API_TOKEN")
    if not token:
        raise MarketplaceContractError("MARKETPLACE_API_TOKEN não está definido.")
    call = transport or _live_http_transport
    common_headers = {"Authorization": f"Bearer {token}"}
    status["attempts"] = int(status.get("attempts") or 0) + 1
    checkpoint: dict[str, Any] = {
        "import_id": import_id,
        "dry_run": False,
        "create": None,
        "uploads": [],
        "finalize": None,
    }
    checkpoint_path = package / "marketplace-live-response.json"

    try:
        create_response = call(
            "POST",
            f"{base_url}{path}",
            {
                **common_headers,
                "Content-Type": "application/json",
                "Idempotency-Key": import_id,
            },
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            timeout,
        )
        create_data = _validate_live_create(create_response, payload)
        checkpoint["create"] = create_response
        product_id = create_data["product_id"]
        status["registered"] = True
        status["product_id"] = product_id
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(status_path, status)

        local_images = {image["sequence"]: image for image in payload["images"]}
        for upload_plan in create_data["image_uploads"]:
            sequence = upload_plan["sequence"]
            if upload_plan.get("status") == "uploaded":
                continue
            image = local_images[sequence]
            image_path = package / "fotos" / image["filename"]
            contents = image_path.read_bytes()
            boundary, multipart = _multipart_file(
                image["filename"], image["media_type"], contents
            )
            upload_response = call(
                "PUT",
                f"{base_url}{upload_plan['path']}",
                {
                    **common_headers,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Idempotency-Key": f"{import_id}:{sequence}",
                },
                multipart,
                timeout,
            )
            _validate_live_upload(upload_response, product_id, sequence)
            checkpoint["uploads"].append(upload_response)
            _atomic_json(checkpoint_path, checkpoint)

        finalize_response = call(
            "POST",
            f"{base_url}{path}/{import_id}/finalize",
            {**common_headers, "Content-Type": "application/json"},
            b"{}",
            timeout,
        )
        finalize_data = _validate_live_finalize(
            finalize_response, product_id, payload["visible"]
        )
        checkpoint["finalize"] = finalize_response
        completed_at = (now or datetime.now(timezone.utc)).isoformat().replace(
            "+00:00", "Z"
        )
        checkpoint["completed_at"] = completed_at
        _atomic_json(checkpoint_path, checkpoint)
        status["images_uploaded"] = True
        status["marketplace_completed_at"] = completed_at
        _atomic_json(status_path, status)
    except (MarketplaceAPIError, OSError, KeyError, TypeError) as error:
        errors = status.setdefault("errors", [])
        errors.append(f"Marketplace LIVE: {error}")
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(status_path, status)
        if isinstance(error, MarketplaceAPIError):
            raise
        raise MarketplaceAPIError(f"Falha durante a execução real: {error}") from error

    return {
        "import_id": import_id,
        "product_id": product_id,
        "registered": True,
        "images_uploaded": True,
        "published": False,
        "visible": payload["visible"],
        "publication": finalize_data["publication"],
        "response_path": str(checkpoint_path),
        "dry_run": False,
    }
