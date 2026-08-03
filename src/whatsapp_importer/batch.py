from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingest import (
    IngestError,
    _assert_allowed_chat,
    _digits,
    _load_settings,
    ingest_event,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _database(root: Path) -> Path:
    path = root / "anuncios" / "inbound.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inbound_events (
            observed_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            batch_id TEXT,
            staged_at TEXT NOT NULL,
            processed_at TEXT,
            UNIQUE(chat_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS inbound_events_stream_status "
        "ON inbound_events(chat_id, sender_id, status, observed_sequence)"
    )


def stage_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not isinstance(event, dict):
        raise IngestError("evento deve ser um objeto JSON")
    if str(event.get("source") or "whatsapp").lower() != "whatsapp":
        raise IngestError("source inválido")
    message_id = str(event.get("message_id") or "").strip()
    if not message_id:
        raise IngestError("message_id é obrigatório")
    settings = _load_settings(root)
    chat_id, sender_id = _assert_allowed_chat(event, settings)
    received_at = str(event.get("received_at") or _now())
    payload = dict(event)
    payload["chat_id"] = chat_id
    payload["sender_id"] = sender_id
    payload["message_id"] = message_id
    payload["received_at"] = received_at

    with closing(sqlite3.connect(_database(root))) as conn:
        _initialize(conn)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO inbound_events (
                chat_id, sender_id, message_id, received_at, payload_json,
                status, staged_at
            ) VALUES (?, ?, ?, ?, ?, 'staged', ?)
            """,
            (
                chat_id,
                sender_id,
                message_id,
                received_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                _now(),
            ),
        )
        inserted = cursor.rowcount == 1
        row = conn.execute(
            "SELECT observed_sequence, status FROM inbound_events "
            "WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        conn.commit()
    return {
        "action": "event_staged" if inserted else "staged_duplicate_ignored",
        "chat_id": chat_id,
        "sender_id": sender_id,
        "message_id": message_id,
        "observed_sequence": row[0] if row else None,
        "status": row[1] if row else None,
    }


def _sort_key(row: sqlite3.Row) -> tuple[float, int]:
    raw = str(row["received_at"])
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        timestamp = 0.0
    return timestamp, int(row["observed_sequence"])


def flush_stream(root: Path, chat_id: str, sender_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    chat_id = _digits(chat_id)
    sender_id = _digits(sender_id)
    batch_id = str(uuid.uuid4())
    database = _database(root)

    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        _initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM inbound_events WHERE chat_id=? AND sender_id=? "
            "AND status='staged'",
            (chat_id, sender_id),
        ).fetchall()
        if not rows:
            conn.commit()
            return {
                "action": "batch_empty",
                "chat_id": chat_id,
                "sender_id": sender_id,
                "results": [],
            }
        sequences = [int(row["observed_sequence"]) for row in rows]
        placeholders = ",".join("?" for _ in sequences)
        conn.execute(
            f"UPDATE inbound_events SET status='processing', batch_id=? "
            f"WHERE observed_sequence IN ({placeholders})",
            (batch_id, *sequences),
        )
        conn.commit()

    results: list[dict[str, Any]] = []
    approval_chat_ids: set[str] = set()
    try:
        for row in sorted(rows, key=_sort_key):
            event = json.loads(str(row["payload_json"]))
            approval_chat_id = _digits(event.get("approval_chat_id"))
            if approval_chat_id:
                approval_chat_ids.add(approval_chat_id)
            try:
                result = ingest_event(root, event)
            except (IngestError, OSError, json.JSONDecodeError) as error:
                result = {
                    "action": "event_rejected",
                    "message_id": str(row["message_id"]),
                    "reason": str(error),
                }
            results.append(
                {
                    **result,
                    "received_at": str(row["received_at"]),
                    "observed_sequence": int(row["observed_sequence"]),
                }
            )
    except Exception:
        with closing(sqlite3.connect(database)) as conn:
            _initialize(conn)
            conn.execute(
                "UPDATE inbound_events SET status='staged', batch_id=NULL "
                "WHERE batch_id=? AND status='processing'",
                (batch_id,),
            )
            conn.commit()
        raise

    with closing(sqlite3.connect(database)) as conn:
        _initialize(conn)
        conn.execute(
            "UPDATE inbound_events SET status='processed', processed_at=? "
            "WHERE batch_id=? AND status='processing'",
            (_now(), batch_id),
        )
        conn.commit()
    return {
        "action": "batch_flushed",
        "batch_id": batch_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "approval_chat_id": (
            next(iter(approval_chat_ids))
            if len(approval_chat_ids) == 1
            else chat_id
        ),
        "event_count": len(results),
        "results": results,
    }


def flush_all(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    database = _database(root)
    with closing(sqlite3.connect(database)) as conn:
        _initialize(conn)
        conn.execute(
            "UPDATE inbound_events SET status='staged', batch_id=NULL "
            "WHERE status='processing'"
        )
        conn.commit()
        streams = conn.execute(
            "SELECT DISTINCT chat_id, sender_id FROM inbound_events "
            "WHERE status='staged' ORDER BY chat_id, sender_id"
        ).fetchall()
    batches = [flush_stream(root, chat_id, sender_id) for chat_id, sender_id in streams]
    return {"action": "all_batches_flushed", "batches": batches}
