from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ConfigurationError, load_settings


class IngestError(ValueError):
    """An event failed deterministic validation."""


YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
SHORT_YEAR_RE = re.compile(r"(?i)\bano\s+\d{2}\b")
SHORT_YEAR_VALUE_RE = re.compile(r"(?i)\bano\s+(\d{2})\b")
MEDIA_PLACEHOLDER_RE = re.compile(r"^<media:[^>]+>$")
PRICE_SIGNAL_RE = re.compile(
    r"(?i)\b(?:valor|pre[cç]o)\b[^0-9]{0,20}"
    r"(?:r\$\s*)?(?:\d{1,3}(?:[.\s]\d{3})+|\d{4,9})(?:,\d{2})?\b"
)
CONTACT_SIGNAL_RE = re.compile(
    r"(?i)\b(?:fone|telefone|whats(?:app)?|contato)\b[^0-9]{0,30}\d"
)
DETAIL_HINTS = {
    "cabine",
    "cilindros",
    "conservacao",
    "conservação",
    "contato",
    "dono",
    "fone",
    "horas",
    "motor",
    "nota fiscal",
    "operacional",
    "pneus",
    "preco",
    "preço",
    "rodante",
    "valor",
}
TOKEN_STOPWORDS = {
    "000",
    "antonio",
    "apenas",
    "ano",
    "com",
    "conservacao",
    "das",
    "de",
    "do",
    "dos",
    "dono",
    "em",
    "estado",
    "excelente",
    "fiscal",
    "fone",
    "horas",
    "maquinas",
    "maquina",
    "mecanica",
    "nota",
    "operacional",
    "para",
    "por",
    "preco",
    "pronta",
    "rodante",
    "toda",
    "todo",
    "trabalhar",
    "unico",
    "valor",
    "vendedor",
    "empresa",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _json_load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_jsonl(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    lock_path = root / "anuncios" / "recebendo" / ".ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_settings(root: Path) -> dict[str, Any]:
    try:
        return load_settings(root)
    except ConfigurationError as error:
        raise IngestError(str(error)) from error


def _state_key(chat_id: str, sender_id: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{sender_id}".encode()).hexdigest()


def _message_key(chat_id: str, message_id: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{message_id}".encode()).hexdigest()


def _listing_text_key(chat_id: str, text: str, received_at: str) -> str:
    day = received_at[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", received_at) else "unknown"
    normalized = re.sub(r"[^a-z0-9]+", " ", _plain(text)).strip()
    return hashlib.sha256(f"{chat_id}\0{day}\0{normalized}".encode()).hexdigest()


def _candidate_text(text: str, keywords: list[str]) -> tuple[bool, str]:
    if not text or MEDIA_PLACEHOLDER_RE.match(text.strip()):
        return False, "mensagem sem texto de anúncio"
    if not YEAR_RE.search(text) and not SHORT_YEAR_RE.search(text):
        return False, "texto não contém ano"
    searchable = _plain(text)
    if any(_plain(keyword) in searchable for keyword in keywords):
        return True, ""
    if PRICE_SIGNAL_RE.search(text) and CONTACT_SIGNAL_RE.search(text):
        return True, ""
    return False, "texto não contém tipo reconhecível nem sinais fortes de anúncio"


def _year_values(text: str) -> set[int]:
    years = {int(value) for value in YEAR_RE.findall(text)}
    for value in SHORT_YEAR_VALUE_RE.findall(text):
        short = int(value)
        years.add(2000 + short if short <= 30 else 1900 + short)
    return years


def _model_tokens(text: str) -> set[str]:
    prefix = re.split(
        r"\bano\b|(?<!\d)(?:19|20)\d{2}(?!\d)",
        _plain(text),
        maxsplit=1,
    )[0]
    return {
        token
        for token in re.findall(r"[a-z0-9]+", prefix)
        if len(token) >= 2 and any(char.isdigit() for char in token)
    }


def _meaningful_tokens(
    text: str, extra_stopwords: set[str] | None = None
) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", _plain(text)))
    stopwords = TOKEN_STOPWORDS | (extra_stopwords or set())
    return {
        token
        for token in tokens
        if len(token) >= 3
        and token not in stopwords
        and not (token.isdigit() and len(token) >= 7)
    }


def _text_relation(
    existing_text: str,
    incoming_text: str,
    keywords: list[str],
    extra_stopwords: set[str] | None = None,
) -> str:
    """Classify a text received while a candidate is collecting media."""
    incoming_valid, _reason = _candidate_text(incoming_text, keywords)
    incoming_plain = _plain(incoming_text)
    existing_tokens = _meaningful_tokens(existing_text, extra_stopwords)
    incoming_tokens = _meaningful_tokens(incoming_text, extra_stopwords)
    overlap = existing_tokens & incoming_tokens
    existing_years = _year_values(existing_text)
    incoming_years = _year_values(incoming_text)
    if not incoming_valid:
        if incoming_years and existing_years & incoming_years and overlap:
            return "supplement"
        has_detail = any(_plain(hint) in incoming_plain for hint in DETAIL_HINTS)
        return "supplement" if has_detail and len(incoming_text.strip()) >= 12 else "noise"

    existing_plain = _plain(existing_text)
    if incoming_plain == existing_plain:
        return "supplement"
    if existing_years and incoming_years and existing_years.isdisjoint(incoming_years):
        return "new"
    existing_models = _model_tokens(existing_text)
    incoming_models = _model_tokens(incoming_text)
    if existing_models and incoming_models and existing_models.isdisjoint(incoming_models):
        return "new"
    union = existing_tokens | incoming_tokens
    similarity = len(overlap) / len(union) if union else 0.0
    same_year = bool(existing_years and existing_years & incoming_years)
    return "supplement" if similarity >= 0.45 or (same_year and len(overlap) >= 3) else "new"


def _assert_allowed_chat(event: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    chat_id = _digits(
        event.get("chat_id") or event.get("chat_jid") or event.get("from")
    )
    sender_id = _digits(event.get("sender_id") or event.get("sender_e164") or chat_id)
    allowed = {_digits(value) for value in settings.get("allowed_chat_ids", [])}
    is_group = event.get("is_group") is True or str(
        event.get("chat_type", "")
    ).lower() == "group"
    if is_group:
        intake = settings.get("group_intake") or {}
        expected_group = _digits(intake.get("group_jid"))
        approval_chat = _digits(intake.get("approval_chat_id"))
        if (
            intake.get("enabled") is not True
            or not expected_group
            or chat_id != expected_group
            or approval_chat not in allowed
        ):
            raise IngestError("grupo fora do allowlist de entrada")
        if not sender_id:
            raise IngestError("remetente do grupo não identificado")
        return chat_id, sender_id
    if not chat_id or chat_id not in allowed:
        raise IngestError("chat fora do allowlist pessoal")
    return chat_id, sender_id


def _media_sources(event: dict[str, Any]) -> list[tuple[str, str | None]]:
    paths = event.get("media_paths")
    if not isinstance(paths, list):
        path = event.get("media_path")
        paths = [path] if isinstance(path, str) and path else []
    types = event.get("media_types")
    if not isinstance(types, list):
        media_type = event.get("media_type")
        types = [media_type] if isinstance(media_type, str) and media_type else []
    return [
        (str(path), str(types[index]) if index < len(types) and types[index] else None)
        for index, path in enumerate(paths)
        if isinstance(path, str) and path
    ]


def _allowed_media(path_value: str, settings: dict[str, Any]) -> Path:
    source = Path(path_value).expanduser().resolve(strict=True)
    roots = [
        Path(value).expanduser().resolve(strict=True)
        for value in settings.get("allowed_media_roots", [])
    ]
    if not roots or not any(source == root or root in source.parents for root in roots):
        raise IngestError("mídia fora dos diretórios permitidos")
    if not source.is_file():
        raise IngestError("mídia não é um arquivo regular")
    return source


def _extension(source: Path, media_type: str | None) -> str:
    mime = (media_type or "").split(";", 1)[0].strip().lower()
    preferred = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }.get(mime)
    suffix = preferred or source.suffix.lower() or mimetypes.guess_extension(mime) or ".bin"
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_package(
    root: Path,
    settings: dict[str, Any],
    event: dict[str, Any],
    chat_id: str,
    sender_id: str,
    text: str,
    received_at: str,
    dry_run: bool,
) -> tuple[str, Path]:
    import_id = str(uuid.uuid4())
    package = root / "anuncios" / "pendentes" / import_id
    (package / "fotos").mkdir(parents=True, exist_ok=False)
    (package / "mensagem-original.txt").write_text(text, encoding="utf-8")
    is_group = event.get("is_group") is True or str(
        event.get("chat_type", "")
    ).lower() == "group"
    intake = settings.get("group_intake") or {}
    approval_chat_id = (
        _digits(intake.get("approval_chat_id")) if is_group else chat_id
    )
    intake_shadow_mode = bool(
        is_group
        and (
            intake.get("shadow_mode") is True
            or event.get("intake_shadow_mode") is True
        )
    )
    metadata = {
        "import_id": import_id,
        "source": "whatsapp",
        "chat_id": chat_id,
        "chat_jid": str(event.get("chat_jid") or ""),
        "chat_name": str(event.get("chat_name") or ""),
        "sender_id": sender_id,
        "sender_name": str(event.get("sender_name") or ""),
        "is_group": is_group,
        "approval_chat_id": approval_chat_id,
        "approval_sender_id": approval_chat_id,
        "intake_shadow_mode": intake_shadow_mode,
        "message_ids": [str(event["message_id"])],
        "received_at": received_at,
        "text_received_at": received_at,
        "media_received_at": None,
        "media_count": 0,
        "is_forwarded": bool(event.get("is_forwarded", False)),
        "text_segments": [
            {
                "message_id": str(event["message_id"]),
                "text": text,
                "received_at": received_at,
            }
        ],
        "media": [],
    }
    status = {
        "status": "awaiting_media",
        "captured_at": received_at,
        "validated": False,
        "extracted": False,
        "registered": False,
        "images_uploaded": False,
        "published": False,
        "attempts": 0,
        "errors": [],
        "warnings": [],
        "dry_run": dry_run,
        "intake_shadow_mode": intake_shadow_mode,
    }
    _atomic_json(package / "metadata.json", metadata)
    _atomic_json(package / "status.json", status)
    return import_id, package


def _append_candidate_text(
    root: Path,
    import_id: str,
    event: dict[str, Any],
    text: str,
    received_at: str,
) -> None:
    package = root / "anuncios" / "pendentes" / import_id
    metadata_path = package / "metadata.json"
    metadata = _json_load(metadata_path, {})
    message_id = str(event["message_id"])
    segments = metadata.setdefault("text_segments", [])
    if not segments:
        original = (package / "mensagem-original.txt").read_text(encoding="utf-8")
        segments.append(
            {
                "message_id": str((metadata.get("message_ids") or [""])[0]),
                "text": original,
                "received_at": metadata.get("text_received_at"),
            }
        )
    segments.append(
        {"message_id": message_id, "text": text, "received_at": received_at}
    )
    if message_id not in metadata.setdefault("message_ids", []):
        metadata["message_ids"].append(message_id)
    metadata["text_received_at"] = received_at
    (package / "mensagem-combinada.txt").write_text(
        "\n".join(str(segment.get("text") or "").strip() for segment in segments),
        encoding="utf-8",
    )
    _atomic_json(metadata_path, metadata)


def _mark_incomplete(root: Path, import_id: str, warning: str) -> None:
    status_path = root / "anuncios" / "pendentes" / import_id / "status.json"
    status = _json_load(status_path, {})
    warnings = list(status.get("warnings") or [])
    if warning not in warnings:
        warnings.append(warning)
    status["status"] = "captured_incomplete"
    status["warnings"] = warnings
    _atomic_json(status_path, status)


def _attach_media(
    root: Path,
    settings: dict[str, Any],
    import_id: str,
    event: dict[str, Any],
    received_at: str,
) -> tuple[int, int, int]:
    package = root / "anuncios" / "pendentes" / import_id
    metadata_path = package / "metadata.json"
    status_path = package / "status.json"
    metadata = _json_load(metadata_path, {})
    status = _json_load(status_path, {})
    known_hashes = {item.get("sha256") for item in metadata.get("media", [])}
    copied = 0
    duplicates = 0
    skipped = 0

    for path_value, media_type in _media_sources(event):
        source = _allowed_media(path_value, settings)
        if not (media_type or "").lower().startswith("image/"):
            skipped += 1
            continue
        digest = _file_sha256(source)
        if digest in known_hashes:
            duplicates += 1
            continue
        sequence = len(metadata.get("media", [])) + 1
        target = package / "fotos" / f"{sequence:03d}{_extension(source, media_type)}"
        shutil.copyfile(source, target)
        item = {
            "sequence": sequence,
            "filename": target.name,
            "sha256": digest,
            "media_type": media_type,
            "message_id": str(event["message_id"]),
            "received_at": received_at,
        }
        metadata.setdefault("media", []).append(item)
        known_hashes.add(digest)
        copied += 1

    message_id = str(event["message_id"])
    if message_id not in metadata.setdefault("message_ids", []):
        metadata["message_ids"].append(message_id)
    metadata["media_count"] = len(metadata.get("media", []))
    metadata["media_received_at"] = received_at
    if status.get("status") != "awaiting_clarification":
        status["status"] = "captured" if metadata["media_count"] else "awaiting_media"
    _atomic_json(metadata_path, metadata)
    _atomic_json(status_path, status)
    return copied, duplicates, skipped


def _record_dedupe(
    root: Path, chat_id: str, message_id: str, result: dict[str, Any]
) -> None:
    path = (
        root
        / "anuncios"
        / "recebendo"
        / "dedup"
        / f"{_message_key(chat_id, message_id)}.json"
    )
    _atomic_json(
        path,
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "recorded_at": _utc_now(),
            "result": result,
        },
    )


def ingest_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    root = root.expanduser().resolve()
    settings = _load_settings(root)
    dry_run = settings.get("dry_run") is True
    if not isinstance(event, dict):
        raise IngestError("evento deve ser um objeto JSON")
    if str(event.get("source") or "whatsapp").lower() != "whatsapp":
        raise IngestError("source inválido")
    message_id = str(event.get("message_id") or "").strip()
    if not message_id:
        raise IngestError("message_id é obrigatório")
    received_at = str(event.get("received_at") or _utc_now())
    chat_id, sender_id = _assert_allowed_chat(event, settings)

    state_path = (
        root
        / "anuncios"
        / "recebendo"
        / "state"
        / f"{_state_key(chat_id, sender_id)}.json"
    )
    dedupe_path = (
        root
        / "anuncios"
        / "recebendo"
        / "dedup"
        / f"{_message_key(chat_id, message_id)}.json"
    )

    with _exclusive_lock(root):
        duplicate = _json_load(dedupe_path, None)
        if duplicate:
            previous = duplicate.get("result") or {}
            return {
                "action": "duplicate_ignored",
                "import_id": previous.get("import_id"),
                "message_id": message_id,
                "dry_run": dry_run,
            }

        state = _json_load(state_path, {"state": "idle"})
        text = event.get("text")
        text = text if isinstance(text, str) else ""
        media = _media_sources(event)

        if media:
            if state.get("state") == "ignoring_duplicate_media":
                result = {
                    "action": "duplicate_listing_media_ignored",
                    "import_id": state.get("duplicate_of_import_id"),
                    "message_id": message_id,
                    "media_skipped": len(media),
                    "dry_run": dry_run,
                }
                _record_dedupe(root, chat_id, message_id, result)
                return result
            clarification_media = state.get("state") == "awaiting_clarification"
            import_id = (
                state.get("import_id")
                if state.get("state") in {"awaiting_media", "awaiting_clarification"}
                else None
            )
            if not import_id:
                image_media = [
                    (path_value, media_type)
                    for path_value, media_type in media
                    if (media_type or "").lower().startswith("image/")
                ]
                skipped_media = len(media) - len(image_media)
                if not image_media:
                    result = {
                        "action": "unsupported_media_ignored",
                        "message_id": message_id,
                        "media_skipped": skipped_media,
                        "dry_run": dry_run,
                    }
                    _record_dedupe(root, chat_id, message_id, result)
                    return result
                orphan_id = str(uuid.uuid4())
                orphan_dir = root / "anuncios" / "recebendo" / "orfaos" / orphan_id
                orphan_dir.mkdir(parents=True, exist_ok=False)
                files = []
                for index, (path_value, media_type) in enumerate(image_media, start=1):
                    source = _allowed_media(path_value, settings)
                    target = orphan_dir / f"{index:03d}{_extension(source, media_type)}"
                    shutil.copyfile(source, target)
                    files.append(
                        {
                            "filename": target.name,
                            "sha256": _file_sha256(target),
                            "media_type": media_type,
                        }
                    )
                result = {
                    "action": "orphan_media_stored",
                    "orphan_id": orphan_id,
                    "message_id": message_id,
                    "media_count": len(files),
                    "media_skipped": skipped_media,
                    "dry_run": dry_run,
                }
                _atomic_json(
                    orphan_dir / "metadata.json",
                    {
                        "orphan_id": orphan_id,
                        "chat_id": chat_id,
                        "sender_id": sender_id,
                        "message_id": message_id,
                        "received_at": received_at,
                        "files": files,
                    },
                )
                _record_dedupe(root, chat_id, message_id, result)
                return result

            copied, duplicate_media, skipped_media = _attach_media(
                root, settings, str(import_id), event, received_at
            )
            state["media_count"] = int(state.get("media_count") or 0) + copied
            state["updated_at"] = received_at
            _atomic_json(state_path, state)
            result = {
                "action": (
                    "unsupported_media_ignored"
                    if copied == 0 and skipped_media > 0
                    else (
                        "clarification_media_attached"
                        if clarification_media
                        else "media_attached"
                    )
                ),
                "import_id": import_id,
                "message_id": message_id,
                "media_copied": copied,
                "duplicate_media": duplicate_media,
                "media_skipped": skipped_media,
                "dry_run": dry_run,
            }
            _record_dedupe(root, chat_id, message_id, result)
            return result

        keywords = list(settings.get("item_keywords") or [])
        redacted_terms = list(settings.get("redacted_terms") or [])
        group_name = str(
            (settings.get("group_intake") or {}).get("group_name") or ""
        ).strip()
        if group_name:
            redacted_terms.append(group_name)
        extra_stopwords = {
            token
            for value in redacted_terms
            for token in re.findall(r"[a-z0-9]+", _plain(str(value)))
            if len(token) >= 3
        }
        valid, reason = _candidate_text(text, keywords)
        if valid:
            listing_text_path = (
                root
                / "anuncios"
                / "recebendo"
                / "listing-text-dedup"
                / f"{_listing_text_key(chat_id, text, received_at)}.json"
            )
            previous_listing = _json_load(listing_text_path, None)
            previous_listing_import_id = str(
                (previous_listing or {}).get("import_id") or ""
            )
            previous_listing_package = (
                root / "anuncios" / "pendentes" / previous_listing_import_id
            )
            previous_listing_metadata = _json_load(
                previous_listing_package / "metadata.json", {}
            )
            active_without_media = (
                state.get("state") == "awaiting_media"
                and str(state.get("import_id") or "") == previous_listing_import_id
                and int(previous_listing_metadata.get("media_count") or 0) == 0
            )
            if (
                previous_listing_import_id
                and previous_listing_package.is_dir()
                and not active_without_media
            ):
                _atomic_json(
                    state_path,
                    {
                        "state": "ignoring_duplicate_media",
                        "duplicate_of_import_id": previous_listing_import_id,
                        "chat_id": chat_id,
                        "sender_id": sender_id,
                        "updated_at": received_at,
                    },
                )
                result = {
                    "action": "duplicate_listing_text_ignored",
                    "import_id": previous_listing_import_id,
                    "message_id": message_id,
                    "dry_run": dry_run,
                }
                _record_dedupe(root, chat_id, message_id, result)
                return result
        active_import_id = (
            str(state["import_id"])
            if state.get("state") == "awaiting_media" and state.get("import_id")
            else None
        )
        if active_import_id:
            package = root / "anuncios" / "pendentes" / active_import_id
            existing_text_path = package / "mensagem-combinada.txt"
            if not existing_text_path.is_file():
                existing_text_path = package / "mensagem-original.txt"
            relation = _text_relation(
                existing_text_path.read_text(encoding="utf-8"),
                text,
                keywords,
                extra_stopwords,
            )
            if relation == "supplement":
                _append_candidate_text(
                    root, active_import_id, event, text, received_at
                )
                state["updated_at"] = received_at
                _atomic_json(state_path, state)
                result = {
                    "action": "candidate_text_appended",
                    "import_id": active_import_id,
                    "message_id": message_id,
                    "dry_run": dry_run,
                }
                _record_dedupe(root, chat_id, message_id, result)
                return result

        if not valid:
            result = {
                "action": "invalid_text_ignored",
                "message_id": message_id,
                "reason": reason,
                "dry_run": dry_run,
            }
            _append_jsonl(
                root / "anuncios" / "recebendo" / "diagnostico.jsonl",
                {**result, "chat_id": chat_id, "sender_id": sender_id, "at": received_at},
            )
            _record_dedupe(root, chat_id, message_id, result)
            return result

        previous_import_id = active_import_id
        previous_completed_import_id = None
        if previous_import_id:
            previous_metadata = _json_load(
                root
                / "anuncios"
                / "pendentes"
                / previous_import_id
                / "metadata.json",
                {},
            )
            if int(previous_metadata.get("media_count") or 0) > 0:
                previous_completed_import_id = previous_import_id
                previous_import_id = None
            else:
                _mark_incomplete(
                    root,
                    previous_import_id,
                    "Novo texto candidato chegou antes das imagens deste anúncio.",
                )

        import_id, _package = _new_package(
            root, settings, event, chat_id, sender_id, text, received_at, dry_run
        )
        _atomic_json(
            root
            / "anuncios"
            / "recebendo"
            / "listing-text-dedup"
            / f"{_listing_text_key(chat_id, text, received_at)}.json",
            {
                "chat_id": chat_id,
                "sender_id": sender_id,
                "message_id": message_id,
                "import_id": import_id,
                "received_at": received_at,
            },
        )
        _atomic_json(
            state_path,
            {
                "state": "awaiting_media",
                "import_id": import_id,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "text_message_id": message_id,
                "media_count": 0,
                "updated_at": received_at,
            },
        )
        result = {
            "action": "candidate_created",
            "import_id": import_id,
            "message_id": message_id,
            "previous_incomplete_import_id": previous_import_id,
            "previous_completed_import_id": previous_completed_import_id,
            "dry_run": dry_run,
        }
        _record_dedupe(root, chat_id, message_id, result)
        return result
