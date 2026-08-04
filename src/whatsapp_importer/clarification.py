from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import ConfigurationError, load_settings


class ClarificationError(ValueError):
    """A clarification answer could not be associated or validated safely."""


SUPPORTED_FIELDS = (
    "price_in_cents",
    "year",
    "category",
    "type",
    "description",
)
FIELD_COMMANDS = {
    "price_in_cents": "PREÇO",
    "year": "ANO",
    "category": "CATEGORIA",
    "type": "TIPO",
    "description": "DESCRIÇÃO",
}
FIELD_LABELS = {
    "price_in_cents": "preço",
    "year": "ano",
    "category": "categoria",
    "type": "tipo",
    "description": "descrição",
}
CODE_RE = re.compile(r"(?i)(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])")
ANSWER_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:sim|cancelar|publicar|pode\s+publicar|pre[çc]o|ano|categoria|tipo|descri[çc][aã]o)\b"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).casefold()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _message_key(chat_id: str, message_id: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{message_id}".encode()).hexdigest()


def _state_key(chat_id: str, sender_id: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{sender_id}".encode()).hexdigest()


def _dedupe_path(root: Path, chat_id: str, message_id: str) -> Path:
    return (
        root
        / "anuncios"
        / "recebendo"
        / "dedup"
        / f"{_message_key(chat_id, message_id)}.json"
    )


def _record_dedupe(
    root: Path, chat_id: str, message_id: str, result: dict[str, Any]
) -> None:
    _atomic_json(
        _dedupe_path(root, chat_id, message_id),
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "recorded_at": _now(),
            "result": result,
        },
    )


def _set_ingest_state(
    root: Path, import_id: str, chat_id: str, sender_id: str, state: str
) -> None:
    _atomic_json(
        root
        / "anuncios"
        / "recebendo"
        / "state"
        / f"{_state_key(chat_id, sender_id)}.json",
        {
            "state": state,
            "import_id": import_id,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "updated_at": _now(),
        },
    )


def _format_price(price_in_cents: int) -> str:
    reais, centavos = divmod(price_in_cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"


def parse_price_answer(value: str) -> int:
    cleaned = _plain(value)
    cleaned = re.sub(r"\b(?:preco|valor)\b", " ", cleaned)
    cleaned = cleaned.replace("r$", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    match = re.search(r"(\d[\d.,\s]*)(?:\s*(mil)\b)?", cleaned)
    if not match:
        raise ClarificationError("não encontrei um preço na resposta")
    number = re.sub(r"\s+", "", match.group(1)).rstrip(".,")
    if not number:
        raise ClarificationError("não encontrei um preço na resposta")

    multiplier = Decimal(1000) if match.group(2) else Decimal(1)
    if multiplier == 1000:
        normalized = number.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+,\d{2}", number):
        parts = number.split(",")
        normalized = "".join(parts[:-1]) + "." + parts[-1]
    elif "." in number and "," in number:
        normalized = number.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number):
        normalized = number.replace(".", "")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", number):
        normalized = number.replace(",", "")
    elif "," in number:
        integer, decimal = number.rsplit(",", 1)
        if len(decimal) not in {1, 2}:
            raise ClarificationError("use um preço como 240.000,00 ou 240 mil")
        normalized = integer + "." + decimal
    else:
        normalized = number

    try:
        cents = int((Decimal(normalized) * multiplier * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        raise ClarificationError("o preço informado não é válido") from None
    if cents <= 0:
        raise ClarificationError("o preço deve ser maior que zero")
    return cents


def _parse_year_answer(value: str) -> int:
    matches = re.findall(r"(?<!\d)(\d{2}|\d{4})(?!\d)", value)
    if len(matches) != 1:
        raise ClarificationError("informe um único ano, como 2018")
    year = int(matches[0])
    if year < 100:
        pivot = (datetime.now().year + 1) % 100
        year = 2000 + year if year <= pivot else 1900 + year
    if year < 1900 or year > datetime.now().year + 1:
        raise ClarificationError("o ano informado não é plausível")
    return year


def _strip_answer_prefix(text: str, code: str, command: str) -> str:
    value = re.sub(rf"(?i)\b{re.escape(code)}\b", " ", text)
    command_pattern = _plain(command)
    words = value.strip().split(maxsplit=1)
    if words and _plain(words[0]) == command_pattern:
        value = words[1] if len(words) == 2 else ""
    return value.strip(" \t:-=")


def _catalog_value(
    root: Path, field: str, raw_value: str, listing: dict[str, Any]
) -> str:
    catalog = _load_json(root / "config" / "marketplace-catalog.json", {})
    if field == "category":
        values = [
            value for value in catalog.get("categories", []) if isinstance(value, str)
        ]
    else:
        values = [
            item.get("name")
            for item in catalog.get("types", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    matches = list(dict.fromkeys(value for value in values if _plain(value) == _plain(raw_value)))
    if not matches:
        raise ClarificationError(
            f"{FIELD_LABELS[field]} não existe no catálogo atual"
        )
    if field == "type":
        current_category = listing.get("category")
        allowed_categories = {
            item.get("category")
            for item in catalog.get("types", [])
            if isinstance(item, dict) and item.get("name") == matches[0]
        }
        if current_category not in allowed_categories and len(allowed_categories) == 1:
            listing["category"] = next(iter(allowed_categories))
    return matches[0]


def _field_from_errors(
    status: dict[str, Any], excluded: set[str] | None = None
) -> str | None:
    searchable = _plain(
        " ".join(
            str(item)
            for item in [*(status.get("errors") or []), *(status.get("warnings") or [])]
        )
    )
    checks = (
        ("description", ("description", "descricao")),
        ("price_in_cents", ("price_in_cents", "preco")),
        ("year", ("year", "ano")),
        ("category", ("category", "categoria")),
        ("type", ("type", "tipo")),
    )
    for field, markers in checks:
        if field not in (excluded or set()) and any(
            marker in searchable for marker in markers
        ):
            return field
    return None


def _field_for_clarification(
    status: dict[str, Any],
    listing: dict[str, Any],
    excluded: set[str] | None = None,
) -> str | None:
    declared_missing = listing.get("missing_fields")
    if isinstance(declared_missing, list):
        for field in SUPPORTED_FIELDS:
            if field in declared_missing and field not in (excluded or set()):
                return field
    return _field_from_errors(status, excluded)


def _question_for(
    import_id: str, field: str, suggestion: Any, listing: dict[str, Any]
) -> str:
    code = import_id[:8]
    command = FIELD_COMMANDS[field]
    if field == "price_in_cents":
        if isinstance(suggestion, int) and suggestion > 0:
            rendered = _format_price(suggestion)
            return (
                f"Fiquei em dúvida no preço do anúncio {code}. Entendi {rendered}. "
                f"Responda SIM {code} para confirmar ou PREÇO {code} 240.000,00 "
                f"para corrigir. Para desistir, responda CANCELAR {code}."
            )
        example = "240.000,00"
    elif field == "year":
        if isinstance(suggestion, int):
            return (
                f"Fiquei em dúvida no ano do anúncio {code}. Entendi {suggestion}. "
                f"Responda SIM {code} para confirmar ou ANO {code} 2018 para corrigir. "
                f"Para desistir, responda CANCELAR {code}."
            )
        example = "2018"
    elif field == "category":
        example = str(listing.get("category") or "maquinas")
    elif field == "type":
        example = str(listing.get("type") or "Pá Carregadeira")
    else:
        example = "máquina revisada e operacional"
    return (
        f"Preciso confirmar a {FIELD_LABELS[field]} do anúncio {code}. "
        f"Responda {command} {code} {example}. "
        f"Para desistir, responda CANCELAR {code}."
    )


def prepare_clarification(root: Path, import_id: str) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path, {})
    if status.get("status") != "review_required":
        return None
    overrides = _load_json(package / "review-overrides.json", {})
    confirmed_fields = set(
        (overrides.get("field_answers") or {}).keys()
        if isinstance(overrides, dict)
        else []
    )
    listing = _load_json(package / "anuncio-extraido.json", {})
    field = _field_for_clarification(status, listing, confirmed_fields)
    if field not in SUPPORTED_FIELDS:
        return None

    metadata = _load_json(package / "metadata.json", {})
    approval_chat_id = _digits(
        metadata.get("approval_chat_id") or metadata.get("chat_id")
    )
    approval_sender_id = _digits(
        metadata.get("approval_sender_id") or approval_chat_id
    )
    suggestion = listing.get(field)
    clarification_path = package / "clarification.json"
    previous = _load_json(clarification_path, {})
    clarification = {
        "version": 1,
        "kind": "field",
        "status": "pending",
        "import_id": import_id,
        "code": import_id[:8],
        "field": field,
        "suggested_value": suggestion,
        "question": _question_for(import_id, field, suggestion, listing),
        "chat_id": approval_chat_id,
        "sender_id": approval_sender_id,
        "source_chat_id": _digits(metadata.get("chat_id")),
        "source_sender_id": _digits(metadata.get("sender_id")),
        "created_at": previous.get("created_at") or _now(),
        "updated_at": _now(),
        "attempts": int(previous.get("attempts") or 0),
        "history": list(previous.get("history") or []),
    }
    _atomic_json(clarification_path, clarification)
    status["status"] = "awaiting_clarification"
    status["clarification_field"] = field
    _atomic_json(status_path, status)
    _set_ingest_state(
        root,
        import_id,
        clarification["source_chat_id"],
        clarification["source_sender_id"],
        "awaiting_clarification",
    )
    return clarification


def prepare_publication_confirmation(
    root: Path, import_id: str
) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    package = root / "anuncios" / "pendentes" / import_id
    status_path = package / "status.json"
    status = _load_json(status_path, {})
    if (
        status.get("status") != "ready_for_review"
        or status.get("validated") is not True
        or status.get("publication_confirmed") is True
    ):
        return None
    listing = _load_json(package / "anuncio-extraido.json", {})
    metadata = _load_json(package / "metadata.json", {})
    approval_chat_id = _digits(
        metadata.get("approval_chat_id") or metadata.get("chat_id")
    )
    approval_sender_id = _digits(
        metadata.get("approval_sender_id") or approval_chat_id
    )
    shadow_mode = metadata.get("intake_shadow_mode") is True
    code = import_id[:8]
    price = listing.get("price_in_cents")
    rendered_price = (
        _format_price(price) if isinstance(price, int) and price > 0 else "não informado"
    )
    question = (
        ("🧪 MODO SOMBRA — nenhuma publicação será feita.\n\n" if shadow_mode else "")
        + f"Anúncio {code} validado:\n"
        f"{listing.get('title')}\n"
        f"Preço: {rendered_price}\n"
        f"Tipo: {listing.get('type')}\n"
        f"Imagens vinculadas: {metadata.get('media_count', 0)}\n\n"
        + (
            "Reaja com 👍 para registrar que o card está correto.\n"
            "Reaja com 👎 para cancelar este teste."
            if shadow_mode
            else (
                "Para autorizar a publicação, reaja a esta mensagem com 👍.\n"
                "Para cancelar, reaja com 👎.\n\n"
                f"Se preferir digitar, responda PUBLICAR {code} ou CANCELAR {code}."
            )
        )
    )
    clarification = {
        "version": 1,
        "kind": "publication_confirmation",
        "status": "pending",
        "import_id": import_id,
        "code": code,
        "question": question,
        "chat_id": approval_chat_id,
        "sender_id": approval_sender_id,
        "source_chat_id": _digits(metadata.get("chat_id")),
        "source_sender_id": _digits(metadata.get("sender_id")),
        "shadow_mode": shadow_mode,
        "created_at": _now(),
        "updated_at": _now(),
        "attempts": 0,
        "history": list(
            (
                _load_json(package / "clarification.json", {})
                if (package / "clarification.json").is_file()
                else {}
            ).get("history")
            or []
        ),
    }
    _atomic_json(package / "clarification.json", clarification)
    status["status"] = "awaiting_publication_confirmation"
    _atomic_json(status_path, status)
    return clarification


def mark_question_sent(
    root: Path, import_id: str, question_message_id: str | None = None
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not re.fullmatch(r"[0-9a-f-]{36}", import_id, re.IGNORECASE):
        raise ClarificationError("import_id inválido")
    package = root / "anuncios" / "pendentes" / import_id
    clarification_path = package / "clarification.json"
    clarification = _load_json(clarification_path, {})
    if clarification.get("status") != "pending":
        raise ClarificationError("não existe pergunta pendente para esse anúncio")
    clarification.setdefault("question_sent_at", _now())
    if question_message_id:
        clarification["question_message_id"] = question_message_id
    clarification["updated_at"] = _now()
    _atomic_json(clarification_path, clarification)
    return {
        "handled": True,
        "action": "clarification_question_marked_sent",
        "import_id": import_id,
    }


def _pending_clarifications(
    root: Path, chat_id: str, sender_id: str
) -> list[tuple[Path, dict[str, Any]]]:
    pending: list[tuple[Path, dict[str, Any]]] = []
    pending_root = root / "anuncios" / "pendentes"
    if not pending_root.is_dir():
        return pending
    for path in pending_root.glob("*/clarification.json"):
        value = _load_json(path, {})
        if (
            value.get("status") == "pending"
            and _digits(value.get("chat_id")) == chat_id
            and _digits(value.get("sender_id")) == sender_id
        ):
            pending.append((path.parent, value))
    return sorted(pending, key=lambda item: str(item[1].get("created_at") or ""))


def _select_pending(
    pending: list[tuple[Path, dict[str, Any]]], text: str
) -> tuple[Path, dict[str, Any]] | None:
    code_match = CODE_RE.search(text)
    if code_match:
        code = code_match.group(1).lower()
        matches = [
            item
            for item in pending
            if str(item[1].get("import_id") or "").lower().startswith(code)
        ]
        if len(matches) == 1:
            return matches[0]
        return None
    if len(pending) == 1:
        return pending[0]
    return None


def _invalid_result(
    root: Path,
    package: Path,
    clarification: dict[str, Any],
    chat_id: str,
    message_id: str,
    raw_answer: str,
    reason: str,
) -> dict[str, Any]:
    clarification["attempts"] = int(clarification.get("attempts") or 0) + 1
    clarification.setdefault("history", []).append(
        {
            "message_id": message_id,
            "received_at": _now(),
            "answer": raw_answer[:500],
            "accepted": False,
            "reason": reason,
        }
    )
    clarification["updated_at"] = _now()
    _atomic_json(package / "clarification.json", clarification)
    result = {
        "handled": True,
        "action": "clarification_invalid",
        "import_id": clarification["import_id"],
        "message_id": message_id,
        "reply": f"Não consegui usar essa resposta: {reason}. {clarification['question']}",
    }
    _record_dedupe(root, chat_id, message_id, result)
    return result


def handle_clarification_event(
    root: Path, event: dict[str, Any]
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if event.get("_internal_action") == "mark_question_sent":
        return mark_question_sent(
            root,
            str(event.get("import_id") or ""),
            str(event.get("question_message_id") or "") or None,
        )
    try:
        settings = load_settings(root)
    except ConfigurationError as error:
        raise ClarificationError(str(error)) from error
    chat_id = _digits(event.get("chat_id") or event.get("from"))
    sender_id = _digits(event.get("sender_id") or chat_id)
    message_id = str(event.get("message_id") or "").strip()
    text = str(event.get("text") or "").strip()
    reaction_target_message_id = str(
        event.get("reaction_target_message_id") or ""
    ).strip()
    reaction_action = str(event.get("reaction_action") or "").strip().upper()
    is_publication_reaction = bool(reaction_target_message_id or reaction_action)
    allowed = {_digits(value) for value in settings.get("allowed_chat_ids", [])}
    if (
        not message_id
        or (not text and not is_publication_reaction)
        or chat_id not in allowed
        or event.get("is_group") is True
        or event.get("media_paths")
    ):
        return {"handled": False}

    previous = _load_json(_dedupe_path(root, chat_id, message_id), None)
    if previous:
        result = previous.get("result") or {}
        if result.get("handled") is True:
            return {
                "handled": True,
                "action": "clarification_duplicate_ignored",
                "import_id": result.get("import_id"),
                "message_id": message_id,
            }

    pending = _pending_clarifications(root, chat_id, sender_id)
    if not pending:
        return {"handled": is_publication_reaction, "action": "reaction_ignored"}
    if not is_publication_reaction and not ANSWER_PREFIX_RE.search(text):
        return {"handled": False, "action": "not_a_clarification_answer"}
    if is_publication_reaction:
        if reaction_action not in {"APPROVE", "CANCEL"}:
            return {"handled": True, "action": "reaction_ignored"}
        matches = [
            item
            for item in pending
            if item[1].get("kind") == "publication_confirmation"
            and str(item[1].get("question_message_id") or "")
            == reaction_target_message_id
        ]
        selected = matches[0] if len(matches) == 1 else None
    else:
        selected = _select_pending(pending, text)
    if selected is None:
        if is_publication_reaction:
            return {"handled": True, "action": "reaction_ignored"}
        codes = ", ".join(item[1]["code"] for item in pending)
        result = {
            "handled": True,
            "action": "clarification_ambiguous",
            "message_id": message_id,
            "reply": f"Há mais de um anúncio aguardando resposta. Inclua o código: {codes}.",
        }
        _record_dedupe(root, chat_id, message_id, result)
        return result

    package, clarification = selected
    import_id = clarification["import_id"]
    code = clarification["code"]
    reaction_emoji = "👍" if reaction_action == "APPROVE" else "👎"
    audit_answer = reaction_emoji if is_publication_reaction else text
    if is_publication_reaction:
        text = (
            f"PUBLICAR {code}"
            if reaction_action == "APPROVE"
            else f"CANCELAR {code}"
        )
    normalized = _plain(text)
    if normalized.startswith("cancelar"):
        clarification["status"] = "cancelled"
        clarification["updated_at"] = _now()
        clarification.setdefault("history", []).append(
            {
                "message_id": message_id,
                "received_at": _now(),
                "answer": audit_answer[:500],
                "accepted": True,
                "action": "cancel",
                **({"method": "reaction"} if is_publication_reaction else {}),
            }
        )
        _atomic_json(package / "clarification.json", clarification)
        status = _load_json(package / "status.json", {})
        status["status"] = "cancelled_by_user"
        status["published"] = False
        _atomic_json(package / "status.json", status)
        result = {
            "handled": True,
            "action": "clarification_cancelled",
            "import_id": import_id,
            "message_id": message_id,
            "reply": f"Anúncio {code} cancelado. Nada foi cadastrado ou publicado.",
        }
        _record_dedupe(root, chat_id, message_id, result)
        return result

    if clarification.get("kind") == "publication_confirmation":
        if not re.fullmatch(rf"(?i)\s*PUBLICAR\s+{re.escape(code)}\s*", text):
            return _invalid_result(
                root,
                package,
                clarification,
                chat_id,
                message_id,
                text,
                f"para autorizar, responda exatamente PUBLICAR {code}",
            )
        clarification["status"] = "answered"
        clarification["updated_at"] = _now()
        clarification["answer_message_id"] = message_id
        clarification["answer"] = audit_answer
        if is_publication_reaction:
            clarification["answer_method"] = "reaction"
            clarification["reaction_target_message_id"] = reaction_target_message_id
        _atomic_json(package / "clarification.json", clarification)
        status = _load_json(package / "status.json", {})
        shadow_mode = clarification.get("shadow_mode") is True
        status["status"] = (
            "shadow_approval_recorded" if shadow_mode else "ready_for_review"
        )
        status["publication_confirmed"] = not shadow_mode
        if shadow_mode:
            status["shadow_approval_recorded_at"] = _now()
            status["published"] = False
        else:
            status["publication_confirmed_at"] = _now()
        status["publication_confirmation_message_id"] = message_id
        if is_publication_reaction:
            status["publication_confirmation_method"] = "reaction"
        _atomic_json(package / "status.json", status)
        result = {
            "handled": True,
            "action": (
                "shadow_approval_recorded" if shadow_mode else "publication_confirmed"
            ),
            "import_id": import_id,
            "message_id": message_id,
            "reply": (
                f"Teste do anúncio {code} registrado. O card foi aprovado, mas nada foi cadastrado ou publicado."
                if shadow_mode
                else f"Autorização do anúncio {code} registrada. Vou cadastrar e publicar."
            ),
        }
        _record_dedupe(root, chat_id, message_id, result)
        return result

    field = clarification.get("field")
    if field not in SUPPORTED_FIELDS:
        raise ClarificationError("campo de esclarecimento inválido")
    command = FIELD_COMMANDS[field]
    try:
        if re.match(r"(?i)^\s*sim\b", text):
            if clarification.get("suggested_value") is None:
                raise ClarificationError("não existe uma sugestão para confirmar")
            value = clarification["suggested_value"]
        else:
            raw_value = _strip_answer_prefix(text, code, command)
            if field == "price_in_cents":
                value = parse_price_answer(raw_value)
            elif field == "year":
                value = _parse_year_answer(raw_value)
            elif field in {"category", "type"}:
                listing = _load_json(package / "anuncio-extraido.json", {})
                value = _catalog_value(root, field, raw_value, listing)
            else:
                value = raw_value
                if not value or len(value) > 5000:
                    raise ClarificationError(
                        "a descrição deve ter entre 1 e 5000 caracteres"
                    )
    except ClarificationError as error:
        return _invalid_result(
            root,
            package,
            clarification,
            chat_id,
            message_id,
            text,
            str(error),
        )

    overrides_path = package / "review-overrides.json"
    overrides = _load_json(overrides_path, {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides["confirmed_by"] = "user"
    overrides["confirmed_at"] = _now()
    answers = overrides.get("field_answers")
    if not isinstance(answers, dict):
        answers = {}
    answers[field] = value
    if field == "type" and value == "Confirmar com o vendedor":
        answers["seller_confirmation_required"] = True
    overrides["field_answers"] = answers
    _atomic_json(overrides_path, overrides)

    clarification["status"] = "answered"
    clarification["updated_at"] = _now()
    clarification["answer_message_id"] = message_id
    clarification["answer"] = text
    clarification["normalized_value"] = value
    clarification.setdefault("history", []).append(
        {
            "message_id": message_id,
            "received_at": _now(),
            "answer": text[:500],
            "accepted": True,
            "normalized_value": value,
        }
    )
    _atomic_json(package / "clarification.json", clarification)
    status = _load_json(package / "status.json", {})
    status["status"] = "review_required"
    _atomic_json(package / "status.json", status)
    result = {
        "handled": True,
        "action": "clarification_recorded",
        "import_id": import_id,
        "message_id": message_id,
        "reply": (
            f"Resposta do anúncio {code} registrada: "
            f"{FIELD_LABELS[field]} = "
            f"{_format_price(value) if field == 'price_in_cents' else value}. "
            "Vou validar novamente."
        ),
    }
    _record_dedupe(root, chat_id, message_id, result)
    return result
