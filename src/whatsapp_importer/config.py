from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """The importer configuration is missing or invalid."""


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _load_dotenv(root: Path) -> None:
    """Load a small, dependency-free .env file without overriding the process."""
    path = root / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f".env inválido na linha {line_number}.")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or not key[0].isalpha():
            raise ConfigurationError(f"Nome de variável inválido na linha {line_number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _settings_path(root: Path) -> Path:
    configured = os.environ.get("IMPORTER_SETTINGS_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else root / path
    candidates = (
        root / "config" / "settings.local.json",
        root / "config" / "settings.json",
        root / "config" / "settings.example.json",
    )
    return next((path for path in candidates if path.is_file()), candidates[-1])


def _boolean(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} deve ser true ou false.")


def _integer(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} deve ser inteiro.") from error


def _string(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _section(settings: dict[str, Any], name: str) -> dict[str, Any]:
    current = settings.get(name)
    if current is None:
        current = {}
        settings[name] = current
    if not isinstance(current, dict):
        raise ConfigurationError(f"A seção {name} deve ser um objeto JSON.")
    return current


def _set_if(section: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        section[key] = value


def _apply_environment(settings: dict[str, Any]) -> None:
    dry_run = _boolean("DRY_RUN")
    if dry_run is not None:
        settings["dry_run"] = dry_run

    personal_chat = _string("OPENCLAW_PERSONAL_CHAT_ID")
    allowed_chats = _string("OPENCLAW_ALLOWED_CHAT_IDS")
    if allowed_chats:
        settings["allowed_chat_ids"] = [
            value.strip() for value in allowed_chats.split(",") if value.strip()
        ]
    elif personal_chat:
        settings["allowed_chat_ids"] = [personal_chat]

    media_root = _string("OPENCLAW_MEDIA_ROOT")
    if media_root:
        settings["allowed_media_roots"] = [media_root]

    redacted_terms = _string("REDACTED_TERMS")
    if redacted_terms:
        settings["redacted_terms"] = [
            value.strip() for value in redacted_terms.split(",") if value.strip()
        ]

    intake = _section(settings, "group_intake")
    _set_if(intake, "enabled", _boolean("SOURCE_GROUP_ENABLED"))
    _set_if(intake, "shadow_mode", _boolean("SOURCE_GROUP_SHADOW_MODE"))
    _set_if(intake, "group_name", _string("SOURCE_GROUP_NAME"))
    _set_if(intake, "group_jid", _string("SOURCE_GROUP_JID"))
    _set_if(
        intake,
        "approval_chat_id",
        _string("APPROVAL_CHAT_ID") or personal_chat,
    )

    extraction = _section(settings, "extraction_model")
    _set_if(extraction, "provider", _string("OLLAMA_PROVIDER"))
    _set_if(extraction, "name", _string("OLLAMA_EXTRACTION_MODEL"))
    _set_if(extraction, "endpoint", _string("OLLAMA_ENDPOINT"))
    _set_if(extraction, "timeout_seconds", _integer("OLLAMA_TIMEOUT_SECONDS"))

    marketplace = _section(settings, "marketplace_api")
    _set_if(marketplace, "enabled", _boolean("MARKETPLACE_ENABLED"))
    _set_if(marketplace, "base_url", _string("MARKETPLACE_INTERNAL_URL"))
    _set_if(marketplace, "path", _string("MARKETPLACE_API_PATH"))
    _set_if(marketplace, "dry_run_only", _boolean("MARKETPLACE_DRY_RUN_ONLY"))
    _set_if(marketplace, "visible", _boolean("MARKETPLACE_VISIBLE"))

    personal = _section(settings, "personal_publication")
    _set_if(personal, "enabled", _boolean("PERSONAL_PUBLICATION_ENABLED"))
    _set_if(personal, "channel", _string("PERSONAL_PUBLICATION_CHANNEL"))

    publication = _section(settings, "group_publication")
    _set_if(publication, "enabled", _boolean("GROUP_PUBLICATION_ENABLED"))
    _set_if(publication, "channel", _string("GROUP_PUBLICATION_CHANNEL"))
    _set_if(publication, "group_name", _string("PUBLICATION_GROUP_NAME"))
    _set_if(publication, "group_jid", _string("PUBLICATION_GROUP_JID"))


def load_settings(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _load_dotenv(root)
    path = _settings_path(root)
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(
            "Configuração ausente. Copie config/settings.example.json para "
            "config/settings.local.json ou defina IMPORTER_SETTINGS_PATH."
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"JSON de configuração inválido: {path}") from error
    if not isinstance(settings, dict):
        raise ConfigurationError(f"Configuração inválida: {path}")
    _apply_environment(settings)
    return settings
