from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ConfigurationError, load_settings
from .marketplace import _atomic_json, _load_json


class PublicationError(RuntimeError):
    """The personal WhatsApp publication cannot continue safely."""


class PublicationDeliveryError(PublicationError):
    def __init__(self, message: str, *, uncertain: bool = True) -> None:
        super().__init__(message)
        self.uncertain = uncertain


Runner = Callable[[dict[str, Any]], dict[str, Any]]


def _run_album_http(params: dict[str, Any], route: str) -> dict[str, Any]:
    config_path = Path(
        os.environ.get("OPENCLAW_CONFIG_PATH")
        or Path.home() / ".openclaw" / "openclaw.json"
    )
    try:
        config = _load_json(config_path)
        gateway = config.get("gateway")
        auth = gateway.get("auth") if isinstance(gateway, dict) else None
        token = auth.get("token") if isinstance(auth, dict) else None
        mode = auth.get("mode") if isinstance(auth, dict) else None
        if mode != "token" or not isinstance(token, str) or not token:
            raise PublicationDeliveryError(
                "Gateway local sem autenticação por token.", uncertain=False
            )
        request = urllib.request.Request(
            f"http://127.0.0.1:18789{route}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except PublicationDeliveryError:
        raise
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()[-1000:]
        raise PublicationDeliveryError(
            f"OpenClaw recusou o álbum: {detail}",
            uncertain=error.code >= 500,
        ) from error
    except (TimeoutError, urllib.error.URLError) as error:
        raise PublicationDeliveryError("Timeout no envio pelo OpenClaw.") from error
    except OSError as error:
        raise PublicationDeliveryError(
            f"Falha ao acessar a configuração do OpenClaw: {error}", uncertain=False
        ) from error

    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        raise PublicationDeliveryError("OpenClaw retornou JSON inválido.") from error
    if not isinstance(result, dict):
        raise PublicationDeliveryError("OpenClaw retornou resultado inválido.")
    return result


def _run_personal_album_http(params: dict[str, Any]) -> dict[str, Any]:
    return _run_album_http(
        params, "/api/romildonegocios/whatsapp/personal-album"
    )


def _run_group_album_http(params: dict[str, Any]) -> dict[str, Any]:
    return _run_album_http(params, "/api/romildonegocios/whatsapp/group-album")


def _find_message_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in (
            "messageId",
            "message_id",
            "primaryPlatformMessageId",
            "primaryMessageId",
        ):
            candidate = value.get(key)
            if candidate is not None and str(candidate):
                return str(candidate)
        for child in value.values():
            found = _find_message_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_message_id(child)
            if found:
                return found
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_openclaw_media(import_id: str, operation: dict[str, Any]) -> Path:
    source = Path(operation["path"])
    staging_root = Path(
        os.environ.get("OPENCLAW_MEDIA_ROOT")
        or Path.home() / ".openclaw" / "media" / "outbound"
    )
    destination = staging_root / "romildonegocios" / import_id / source.name
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError as error:
        raise PublicationDeliveryError(
            f"Falha ao preparar a mídia {source.name}: {error}",
            uncertain=False,
        ) from error
    if _sha256(destination) != operation["sha256"]:
        raise PublicationDeliveryError(
            f"Falha ao preparar a mídia {source.name}.", uncertain=False
        )
    return destination


def _publication_album(package: Path, publication: dict[str, Any]) -> dict[str, Any]:
    metadata = _load_json(package / "metadata.json")
    media = metadata.get("media")
    remote_images = publication.get("images")
    if not isinstance(media, list) or not isinstance(remote_images, list):
        raise PublicationError("Publicação ou metadata sem imagens ordenadas.")
    status_path = package / "status.json"
    status = _load_json(status_path) if status_path.exists() else {}
    excluded = set(
        metadata.get("excluded_image_sequences")
        or status.get("excluded_image_sequences")
        or []
    )
    usable_media = [item for item in media if item.get("sequence") not in excluded]
    if len(usable_media) != len(remote_images) or not usable_media:
        raise PublicationError("Quantidade de imagens diverge da publicação oficial.")

    album = {
        "kind": "native_album",
        "status": "pending",
        "message": publication["text"],
        "media": [],
    }
    for expected_sequence, item in enumerate(usable_media, start=1):
        image_path = package / "fotos" / str(item.get("filename"))
        if not image_path.is_file() or _sha256(image_path) != item.get("sha256"):
            raise PublicationError(f"Hash local inválido para a imagem {expected_sequence}.")
        album["media"].append(
            {
                "sequence": expected_sequence,
                "path": str(image_path),
                "sha256": item["sha256"],
            }
        )
    return album


def _fallback_group_publication(package: Path, status: dict[str, Any]) -> dict[str, Any]:
    if (
        status.get("validated") is not True
        or status.get("extracted") is not True
        or status.get("product_id") is not None
        or status.get("registered") is not False
        or status.get("images_uploaded") is not False
        or status.get("published") is not False
    ):
        raise PublicationError(
            "O pacote não está seguro para publicação sem cadastro no site."
        )

    extracted = _load_json(package / "anuncio-extraido.json")
    title = extracted.get("title")
    description = extracted.get("description")
    price_in_cents = extracted.get("price_in_cents")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(description, str)
        or not description.strip()
        or not isinstance(price_in_cents, int)
        or isinstance(price_in_cents, bool)
        or price_in_cents <= 0
    ):
        raise PublicationError("Anúncio extraído inválido para publicação sem site.")

    metadata = _load_json(package / "metadata.json")
    media = metadata.get("media")
    if not isinstance(media, list):
        raise PublicationError("Metadata sem imagens ordenadas.")
    excluded = set(
        metadata.get("excluded_image_sequences")
        or status.get("excluded_image_sequences")
        or []
    )
    usable_count = len(
        [item for item in media if item.get("sequence") not in excluded]
    )
    if usable_count == 0:
        raise PublicationError("Publicação sem site exige pelo menos uma imagem.")

    reais, centavos = divmod(price_in_cents, 100)
    formatted_price = f"R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"
    text = (
        f"🏗️ *{title.strip()}*\n\n"
        f"💰 *Preço:* {formatted_price}\n"
        "_(Valor negociável)_\n\n"
        "📝 *Descrição:*\n"
        f"{description.strip()}\n\n"
        "📸 *Fotos anexadas*"
    )
    return {
        "text": text,
        "images": [f"local:{sequence}" for sequence in range(1, usable_count + 1)],
        "published": False,
    }


def publish_to_personal_chat(
    root: Path,
    import_id: str,
    *,
    approval: str,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise PublicationError(str(error)) from error
    publication_settings = settings.get("personal_publication")
    if (
        not isinstance(publication_settings, dict)
        or publication_settings.get("enabled") is not True
    ):
        raise PublicationError("A publicação pessoal está desativada.")

    allowed = settings.get("allowed_chat_ids")
    if not isinstance(allowed, list) or len(allowed) != 1:
        raise PublicationError("A fase exige exatamente um chat pessoal autorizado.")
    chat_id = str(allowed[0])
    if approval != f"PUBLISH_PERSONAL:{import_id}:{chat_id}":
        raise PublicationError("A aprovação literal do chat pessoal é obrigatória.")

    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path)
    if (
        status.get("product_id") is None
        or status.get("registered") is not True
        or status.get("images_uploaded") is not True
        or status.get("published") is not False
    ):
        raise PublicationError("O pacote não está pronto para publicação pessoal.")

    live_response = _load_json(package / "marketplace-live-response.json")
    finalize = live_response.get("finalize")
    data = finalize.get("data") if isinstance(finalize, dict) else None
    publication = data.get("publication") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("product_id") != status["product_id"]
        or data.get("visible") is not False
        or not isinstance(publication, dict)
        or publication.get("published") is not False
        or not isinstance(publication.get("text"), str)
    ):
        raise PublicationError("Artefato oficial de publicação inválido.")

    audit_path = package / "whatsapp-personal-album-publication.json"
    if audit_path.exists():
        audit = _load_json(audit_path)
        if (
            audit.get("import_id") != import_id
            or audit.get("chat_id") != chat_id
            or audit.get("product_id") != status["product_id"]
        ):
            raise PublicationError("Checkpoint de publicação pertence a outro destino.")
        if audit.get("status") == "complete":
            return _result(audit, audit_path, replayed=True)
        if audit.get("operation", {}).get("status") == "uncertain":
            raise PublicationError("Existe entrega incerta; revisão manual é obrigatória.")
    else:
        operation = _publication_album(package, publication)
        audit = {
            "import_id": import_id,
            "product_id": status.get("product_id"),
            "channel": "whatsapp",
            "chat_id": chat_id,
            "delivery_mode": (
                "native_album" if len(operation["media"]) > 1 else "single_media"
            ),
            "idempotency_key": (
                f"romildonegocios:personal-native-album:{import_id}:{chat_id}"
            ),
            "status": "pending",
            "operation": operation,
        }
        _atomic_json(audit_path, audit)

    call = runner or _run_personal_album_http
    operation = audit["operation"]
    audit["status"] = "in_progress"
    operation["status"] = "sending"
    _atomic_json(audit_path, audit)

    try:
        media_paths = [
            str(
                _stage_openclaw_media(import_id, item)
                if runner is None
                else Path(item["path"])
            )
            for item in operation["media"]
        ]
        params = {
            "importId": import_id,
            "chatId": chat_id,
            "approval": approval,
            "message": operation["message"],
            "mediaUrls": media_paths,
        }
        response = call(params)
        message_id = _find_message_id(response)
        if not message_id:
            raise PublicationDeliveryError(
                "OpenClaw não retornou messageId para o álbum.", uncertain=True
            )
    except PublicationDeliveryError as error:
        operation["status"] = "uncertain" if error.uncertain else "failed"
        operation["error"] = str(error)
        audit["status"] = operation["status"]
        _atomic_json(audit_path, audit)
        raise

    operation["status"] = "sent"
    operation.pop("error", None)
    operation["message_id"] = message_id
    operation["sent_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    operation["response"] = response
    _atomic_json(audit_path, audit)

    completed_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    audit["status"] = "complete"
    audit["completed_at"] = completed_at
    _atomic_json(audit_path, audit)

    status["personal_test_published"] = True
    status["personal_test_delivery_mode"] = audit["delivery_mode"]
    status["personal_test_published_at"] = completed_at
    status["personal_test_message_ids"] = [operation["message_id"]]
    _atomic_json(status_path, status)
    return _result(audit, audit_path, replayed=False)


def publish_to_group(
    root: Path,
    import_id: str,
    *,
    approval: str,
    without_site: bool = False,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise PublicationError(str(error)) from error
    publication_settings = settings.get("group_publication")
    if (
        not isinstance(publication_settings, dict)
        or publication_settings.get("enabled") is not True
    ):
        raise PublicationError("A publicação no grupo está desativada.")

    group_name = str(publication_settings.get("group_name") or "").strip()
    group_jid = str(publication_settings.get("group_jid") or "").strip()
    if not group_name or not group_jid.endswith("@g.us"):
        raise PublicationError("O destino do grupo não está configurado corretamente.")
    if approval != f"PUBLISH_GROUP:{import_id}:{group_jid}":
        raise PublicationError("A aprovação literal do grupo é obrigatória.")

    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path)
    audit_path = package / "whatsapp-group-album-publication.json"
    if audit_path.exists():
        completed_audit = _load_json(audit_path)
        if (
            completed_audit.get("import_id") != import_id
            or completed_audit.get("group_jid") != group_jid
            or completed_audit.get("group_name") != group_name
        ):
            raise PublicationError("Checkpoint de publicação pertence a outro destino.")
        if completed_audit.get("status") == "complete":
            return _group_result(completed_audit, audit_path, replayed=True)

    if without_site:
        publication = _fallback_group_publication(package, status)
    else:
        live_response = _load_json(package / "marketplace-live-response.json")
        finalize = live_response.get("finalize")
        data = finalize.get("data") if isinstance(finalize, dict) else None
        publication = data.get("publication") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("product_id") != status.get("product_id")
            or data.get("visible") is not True
            or not isinstance(publication, dict)
            or publication.get("published") is not False
            or not isinstance(publication.get("text"), str)
        ):
            raise PublicationError("Artefato oficial de publicação inválido.")

    if audit_path.exists():
        audit = _load_json(audit_path)
        if (
            audit.get("import_id") != import_id
            or audit.get("group_jid") != group_jid
            or audit.get("group_name") != group_name
        ):
            raise PublicationError("Checkpoint de publicação pertence a outro destino.")
        if (
            audit.get("product_id") != status.get("product_id")
            or audit.get("without_site") is not without_site
        ):
            raise PublicationError("Checkpoint de publicação pertence a outro produto.")
        if audit.get("operation", {}).get("status") == "uncertain":
            raise PublicationError("Existe entrega incerta; revisão manual é obrigatória.")
    else:
        if not without_site and (
            status.get("product_id") is None
            or status.get("registered") is not True
            or status.get("images_uploaded") is not True
            or status.get("published") is not False
        ):
            raise PublicationError("O pacote não está pronto para publicação no grupo.")
        operation = _publication_album(package, publication)
        audit = {
            "import_id": import_id,
            "product_id": status.get("product_id"),
            "channel": "whatsapp",
            "group_jid": group_jid,
            "group_name": group_name,
            "without_site": without_site,
            "delivery_mode": (
                "native_album" if len(operation["media"]) > 1 else "single_media"
            ),
            "idempotency_key": (
                f"romildonegocios:group-native-album:{import_id}:{group_jid}"
            ),
            "status": "pending",
            "operation": operation,
        }
        _atomic_json(audit_path, audit)

    call = runner or _run_group_album_http
    operation = audit["operation"]
    audit["status"] = "in_progress"
    operation["status"] = "sending"
    _atomic_json(audit_path, audit)

    try:
        media_paths = [
            str(
                _stage_openclaw_media(import_id, item)
                if runner is None
                else Path(item["path"])
            )
            for item in operation["media"]
        ]
        params = {
            "importId": import_id,
            "groupJid": group_jid,
            "groupName": group_name,
            "approval": approval,
            "message": operation["message"],
            "mediaUrls": media_paths,
        }
        response = call(params)
        message_id = _find_message_id(response)
        if not message_id:
            raise PublicationDeliveryError(
                "OpenClaw não retornou messageId para o álbum.", uncertain=True
            )
    except PublicationDeliveryError as error:
        operation["status"] = "uncertain" if error.uncertain else "failed"
        operation["error"] = str(error)
        audit["status"] = operation["status"]
        _atomic_json(audit_path, audit)
        raise

    operation["status"] = "sent"
    operation.pop("error", None)
    operation["message_id"] = message_id
    operation["sent_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    operation["response"] = response
    _atomic_json(audit_path, audit)

    completed_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    audit["status"] = "complete"
    audit["completed_at"] = completed_at
    _atomic_json(audit_path, audit)

    status["published"] = True
    status["published_to_group"] = True
    status["group_publication_name"] = group_name
    status["group_publication_jid"] = group_jid
    status["group_publication_delivery_mode"] = audit["delivery_mode"]
    status["group_published_at"] = completed_at
    status["group_message_ids"] = [operation["message_id"]]
    status["site_registration_pending"] = without_site
    status["publication_without_site"] = without_site
    _atomic_json(status_path, status)
    return _group_result(audit, audit_path, replayed=False)


def _result(audit: dict[str, Any], audit_path: Path, *, replayed: bool) -> dict[str, Any]:
    return {
        "import_id": audit["import_id"],
        "product_id": audit["product_id"],
        "chat_id": audit["chat_id"],
        "message_ids": (
            [audit["operation"]["message_id"]]
            if audit.get("operation", {}).get("status") == "sent"
            else []
        ),
        "delivery_mode": audit.get("delivery_mode"),
        "personal_test_published": audit.get("status") == "complete",
        "published_to_group": False,
        "replayed": replayed,
        "audit_path": str(audit_path),
    }


def _group_result(
    audit: dict[str, Any], audit_path: Path, *, replayed: bool
) -> dict[str, Any]:
    return {
        "import_id": audit["import_id"],
        "product_id": audit["product_id"],
        "group_jid": audit["group_jid"],
        "group_name": audit["group_name"],
        "message_ids": (
            [audit["operation"]["message_id"]]
            if audit.get("operation", {}).get("status") == "sent"
            else []
        ),
        "delivery_mode": audit.get("delivery_mode"),
        "personal_test_published": False,
        "published_to_group": audit.get("status") == "complete",
        "without_site": audit.get("without_site") is True,
        "replayed": replayed,
        "audit_path": str(audit_path),
    }
